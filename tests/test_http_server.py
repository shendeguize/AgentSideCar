import base64
import fcntl
import http.client
import json
import os
import re
import secrets
import socket
import stat
import tempfile
import threading
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

import sidecar.http_server as http_server
from sidecar.http_server import (
    MAX_CLIENTS,
    MAX_EVENT_STREAMS,
    MAX_HEADER_BYTES,
    MAX_REQUEST_LINE_BYTES,
    HttpRuntimeError,
    HttpServerCloseError,
    SidecarHttpServer,
)


class FakeClient:
    def __init__(self, sessions=(), scan_errors=(), tail_errors=(), events=()):
        self.sessions = list(sessions)
        self.scan_errors = list(scan_errors)
        self.tail_errors = list(tail_errors)
        self.events = list(events)
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return list(self.sessions)

    def subscribe(self, cancel_event=None):
        del cancel_event
        yield from self.events


class BlockingClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.started = 0
        self.cancelled = 0
        self.lock = threading.Lock()

    def subscribe(self, cancel_event=None):
        with self.lock:
            self.started += 1
        try:
            while cancel_event is not None and not cancel_event.wait(0.01):
                pass
        finally:
            with self.lock:
                self.cancelled += 1
        if False:
            yield {}


class PanelParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def by_id(self, identity):
        return next(attrs for _, attrs in self.elements if attrs.get("id") == identity)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def recv_all(connection):
    chunks = bytearray()
    while True:
        try:
            chunk = connection.recv(65536)
        except (ConnectionResetError, socket.timeout):
            break
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def recv_until(connection, marker):
    chunks = bytearray()
    deadline = time.monotonic() + 2.0
    while marker not in chunks and time.monotonic() < deadline:
        chunk = connection.recv(65536)
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


class HttpServerTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.socket_path = self.root / "daemon.sock"
        self.servers = []

    def tearDown(self):
        for server in reversed(self.servers):
            server.close()
        self.temporary.cleanup()

    def start(self, client=None, *, runtime=None, port=0):
        source = FakeClient() if client is None else client
        server = SidecarHttpServer(
            self.runtime if runtime is None else runtime,
            self.socket_path,
            port=port,
            client_factory=lambda **unused: source,
        )
        self.servers.append(server)
        server.start()
        return server

    def token(self, server):
        return server.token_path.read_text(encoding="ascii").strip()

    def request(self, server, method, path, *, headers=None, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            server.port,
            timeout=2.0,
        )
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def authorized_headers(self, server):
        return {"Authorization": "Bearer " + self.token(server)}

    def raw(self, server, request):
        connection = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
        try:
            connection.sendall(request)
            return recv_all(connection)
        finally:
            connection.close()

    def raw_request(self, server, first_line, headers=(), body=b""):
        lines = [first_line, "Host: 127.0.0.1:{}".format(server.port)]
        lines.extend(headers)
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body
        return self.raw(server, payload)


class RuntimeSecurityTests(HttpServerTestCase):
    def test_creates_private_token_and_port_and_retains_only_token(self):
        server = self.start()
        token = self.token(server)
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))

        self.assertGreaterEqual(len(decoded), 32)
        self.assertEqual(0o700, mode(self.runtime))
        self.assertEqual(0o600, mode(server.token_path))
        self.assertEqual(0o600, mode(server.port_path))
        port_record = json.loads(server.port_path.read_text(encoding="ascii"))
        self.assertEqual(server.port, port_record["port"])
        self.assertGreaterEqual(
            len(
                base64.urlsafe_b64decode(
                    port_record["nonce"] + "=" * (-len(port_record["nonce"]) % 4)
                )
            ),
            16,
        )
        first_token = token

        server.close()
        self.assertTrue(server.token_path.exists())
        self.assertFalse(server.port_path.exists())
        server.start()
        self.assertEqual(first_token, self.token(server))

    def test_new_runtime_mode_is_0700_even_under_restrictive_umask(self):
        old_umask = os.umask(0o777)
        try:
            server = self.start()
        finally:
            os.umask(old_umask)
        self.assertEqual(0o700, mode(self.runtime))
        self.assertEqual(0o600, mode(server.token_path))

    def test_runtime_symlink_and_wrong_mode_fail(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        self.runtime.symlink_to(target, target_is_directory=True)
        with self.assertRaises(HttpRuntimeError):
            self.start()

        self.runtime.unlink()
        self.runtime.mkdir(mode=0o755)
        with self.assertRaises(HttpRuntimeError):
            self.start()

    def test_token_symlink_wrong_mode_and_malformed_fail(self):
        self.runtime.mkdir(mode=0o700)
        target = self.root / "token-target"
        target.write_text("not-a-token\n", encoding="ascii")
        target.chmod(0o600)
        (self.runtime / "http.token").symlink_to(target)
        with self.assertRaises(HttpRuntimeError):
            self.start()

        (self.runtime / "http.token").unlink()
        (self.runtime / "http.token").write_text("bad\n", encoding="ascii")
        (self.runtime / "http.token").chmod(0o600)
        with self.assertRaises(HttpRuntimeError):
            self.start()

        (self.runtime / "http.token").write_text(
            base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode() + "\n",
            encoding="ascii",
        )
        (self.runtime / "http.token").chmod(0o644)
        with self.assertRaises(HttpRuntimeError):
            self.start()

    def test_port_symlink_wrong_mode_and_malformed_fail_before_socket_creation(self):
        self.runtime.mkdir(mode=0o700)
        target = self.root / "port-target"
        target.write_text("1234\n", encoding="ascii")
        target.chmod(0o600)
        port_path = self.runtime / "http.port"
        port_path.symlink_to(target)

        with mock.patch("sidecar.http_server.socket.socket") as socket_factory:
            with self.assertRaises(HttpRuntimeError):
                self.start()
        socket_factory.assert_not_called()

        port_path.unlink()
        port_path.write_text("1234 \n", encoding="ascii")
        port_path.chmod(0o600)
        with mock.patch("sidecar.http_server.socket.socket") as socket_factory:
            with self.assertRaises(HttpRuntimeError):
                self.start()
        socket_factory.assert_not_called()

        port_path.write_text("1234\n", encoding="ascii")
        port_path.chmod(0o644)
        with mock.patch("sidecar.http_server.socket.socket") as socket_factory:
            with self.assertRaises(HttpRuntimeError):
                self.start()
        socket_factory.assert_not_called()

    def test_concurrent_first_start_creates_one_valid_token(self):
        servers = [
            SidecarHttpServer(
                self.runtime,
                self.socket_path,
                client_factory=lambda **unused: FakeClient(),
            )
            for _ in range(2)
        ]
        self.servers.extend(servers)
        barrier = threading.Barrier(3)
        results = []

        def launch(server):
            barrier.wait()
            try:
                server.start()
            except BaseException as error:
                results.append(error)
            else:
                results.append(server)

        workers = [threading.Thread(target=launch, args=(server,)) for server in servers]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(3.0)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(1, sum(isinstance(item, SidecarHttpServer) for item in results))
        token = (self.runtime / "http.token").read_text(encoding="ascii").strip()
        self.assertGreaterEqual(
            len(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))),
            32,
        )

    def test_close_does_not_remove_replaced_port_metadata(self):
        server = self.start()
        replacement = self.runtime / "replacement"
        replacement_record = {
            "port": 12345,
            "nonce": secrets.token_urlsafe(24),
        }
        replacement.write_text(
            json.dumps(replacement_record, separators=(",", ":"), sort_keys=True)
            + "\n",
            encoding="ascii",
        )
        replacement.chmod(0o600)
        os.replace(str(replacement), str(server.port_path))

        server.close()

        self.assertEqual(
            replacement_record,
            json.loads(server.port_path.read_text(encoding="ascii")),
        )

    def test_lifetime_flock_excludes_second_owner_and_releases_on_close(self):
        server = self.start()
        descriptor = os.open(str(self.runtime), os.O_RDONLY)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            server.close()
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def test_stale_record_pointing_to_unrelated_listener_is_replaced(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        self.runtime.mkdir(mode=0o700)
        stale = {
            "port": blocker.getsockname()[1],
            "nonce": secrets.token_urlsafe(24),
        }
        port_path = self.runtime / "http.port"
        port_path.write_text(
            json.dumps(stale, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="ascii",
        )
        port_path.chmod(0o600)
        try:
            server = self.start()
        finally:
            blocker.close()

        current = json.loads(server.port_path.read_text(encoding="ascii"))
        self.assertEqual(server.port, current["port"])
        self.assertNotEqual(stale["nonce"], current["nonce"])


class HttpPolicyTests(HttpServerTestCase):
    def test_health_is_minimal_unauthenticated_and_security_headers_apply(self):
        server = self.start()
        status, headers, body = self.request(server, "GET", "/api/v1/health")

        self.assertEqual(200, status)
        self.assertEqual(b'{"ok":true}\n', body)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertEqual("DENY", headers["X-Frame-Options"])
        self.assertEqual("no-referrer", headers["Referrer-Policy"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_head_has_metadata_without_body(self):
        server = self.start()
        status, headers, body = self.request(server, "HEAD", "/api/v1/health")
        self.assertEqual(200, status)
        self.assertEqual("12", headers["Content-Length"])
        self.assertEqual(b"", body)

    def test_host_origin_auth_and_query_are_strict(self):
        server = self.start()
        token = self.token(server)
        cases = [
            (
                self.raw_request(
                    server,
                    "GET /api/v1/status HTTP/1.1",
                    ("Host: localhost:{}".format(server.port),),
                ),
                b"400 ",
            ),
            (
                self.raw_request(
                    server,
                    "GET /api/v1/status HTTP/1.1",
                    ("Origin: http://example.test",),
                ),
                b"403 ",
            ),
            (
                self.raw_request(server, "GET /api/v1/status HTTP/1.1"),
                b"401 ",
            ),
            (
                self.raw_request(
                    server,
                    "GET /api/v1/status HTTP/1.1",
                    ("Authorization: bearer " + token,),
                ),
                b"401 ",
            ),
            (
                self.raw_request(
                    server,
                    "GET /api/v1/status?token=" + token + " HTTP/1.1",
                    ("Authorization: Bearer " + token,),
                ),
                b"400 ",
            ),
        ]
        for response, expected in cases:
            self.assertIn(expected, response.split(b"\r\n", 1)[0])
            self.assertNotIn(token.encode("ascii"), response)

        status, _, _ = self.request(
            server,
            "GET",
            "/api/v1/status",
            headers={
                "Authorization": "Bearer " + token,
                "Origin": server.url,
            },
        )
        self.assertEqual(200, status)

    def test_absolute_uri_duplicate_security_headers_and_smuggling_are_rejected(self):
        server = self.start()
        token = self.token(server)
        host = "127.0.0.1:{}".format(server.port)
        requests = [
            (
                "GET http://{}/api/v1/health HTTP/1.1\r\nHost: {}\r\n\r\n".format(
                    host, host
                ).encode("ascii"),
                b"400 ",
            ),
            (
                (
                    "GET /api/v1/status HTTP/1.1\r\nHost: {0}\r\nHost: {0}\r\n"
                    "Authorization: Bearer {1}\r\n\r\n"
                ).format(host, token).encode("ascii"),
                b"400 ",
            ),
            (
                (
                    "GET /api/v1/status HTTP/1.1\r\nHost: {0}\r\n"
                    "Authorization: Bearer {1}\r\nAuthorization: Bearer {1}\r\n\r\n"
                ).format(host, token).encode("ascii"),
                b"400 ",
            ),
            (
                (
                    "GET /api/v1/health HTTP/1.1\r\nHost: {0}\r\n"
                    "Content-Length: 0\r\nContent-Length: 0\r\n\r\n"
                ).format(host).encode("ascii"),
                b"400 ",
            ),
            (
                (
                    "GET /api/v1/health HTTP/1.1\r\nHost: {0}\r\n"
                    "Transfer-Encoding: chunked\r\n\r\n"
                ).format(host).encode("ascii"),
                b"400 ",
            ),
            (
                (
                    "GET /api/v1/health HTTP/1.1\r\nHost: {0}\r\n"
                    "Content-Length: 1\r\n\r\nx"
                ).format(host).encode("ascii"),
                b"413 ",
            ),
            (
                (
                    "GET /api/v1/health HTTP/1.1\r\nHost: {0}\r\n\r\nbody"
                ).format(host).encode("ascii"),
                b"413 ",
            ),
        ]
        for request, expected in requests:
            response = self.raw(server, request)
            self.assertIn(expected, response.split(b"\r\n", 1)[0])

    def test_methods_unknown_api_and_oversize_headers_are_rejected(self):
        server = self.start()
        method = self.raw_request(server, "POST /api/v1/health HTTP/1.1")
        unknown = self.raw_request(server, "GET /api/v1/send HTTP/1.1")
        oversized = self.raw_request(
            server,
            "GET /api/v1/health HTTP/1.1",
            ("X-Fill: " + "x" * MAX_HEADER_BYTES,),
        )

        self.assertIn(b"405 ", method.split(b"\r\n", 1)[0])
        self.assertIn(b"404 ", unknown.split(b"\r\n", 1)[0])
        self.assertIn(b"431 ", oversized.split(b"\r\n", 1)[0])

    def test_request_line_and_header_reads_stop_at_one_byte_sentinel(self):
        class TrackingSocket:
            def __init__(self, payload):
                self.payload = payload
                self.offset = 0
                self.requests = []

            def settimeout(self, timeout):
                self.timeout = timeout

            def recv(self, maximum):
                self.requests.append(maximum)
                chunk = self.payload[self.offset : self.offset + maximum]
                self.offset += len(chunk)
                return chunk

        line_socket = TrackingSocket(b"G" * (MAX_REQUEST_LINE_BYTES + 100))
        with self.assertRaises(http_server._RequestError) as raised:
            SidecarHttpServer._read_request(line_socket)
        self.assertEqual(414, raised.exception.status)
        self.assertEqual(MAX_REQUEST_LINE_BYTES + 1, line_socket.offset)
        self.assertTrue(all(size <= 4096 for size in line_socket.requests))

        header_socket = TrackingSocket(
            b"GET / HTTP/1.1\r\nX-Fill: " + b"x" * MAX_HEADER_BYTES
        )
        with self.assertRaises(http_server._RequestError) as raised:
            SidecarHttpServer._read_request(header_socket)
        self.assertEqual(431, raised.exception.status)
        self.assertEqual(MAX_HEADER_BYTES + 1, header_socket.offset)
        self.assertTrue(all(size <= 4096 for size in header_socket.requests))

    def test_raw_oversize_request_line_is_414_not_header_error(self):
        server = self.start()
        request = (
            b"GET /"
            + b"x" * MAX_REQUEST_LINE_BYTES
            + b" HTTP/1.1\r\nHost: 127.0.0.1:"
            + str(server.port).encode("ascii")
            + b"\r\n\r\n"
        )
        response = self.raw(server, request)
        self.assertIn(b"414 ", response.split(b"\r\n", 1)[0])

    def test_total_header_deadline_stops_slowloris(self):
        with mock.patch("sidecar.http_server.REQUEST_TIMEOUT_SECONDS", 0.1):
            server = self.start()
            connection = socket.create_connection(
                ("127.0.0.1", server.port),
                timeout=1.0,
            )
            try:
                connection.sendall(b"GET /")
                response = recv_all(connection)
            finally:
                connection.close()
        self.assertIn(b"408 ", response.split(b"\r\n", 1)[0])


class ProtocolTests(HttpServerTestCase):
    def test_status_reconstructs_daemon_jsonl_fields_exactly(self):
        sessions = [
            {
                "agent": "cursor-cli",
                "session_id": "one",
                "project": "/tmp/project",
                "status": "working",
            }
        ]
        scan_errors = [{"adapter": "broken", "stage": "discover"}]
        tail_errors = [{"agent": "cursor-cli", "code": "read_failed"}]
        client = FakeClient(sessions, scan_errors, tail_errors)
        server = self.start(client)

        status, headers, body = self.request(
            server,
            "GET",
            "/api/v1/status",
            headers=self.authorized_headers(server),
        )

        self.assertEqual(200, status)
        self.assertEqual("application/json; charset=utf-8", headers["Content-Type"])
        self.assertTrue(body.endswith(b"\n"))
        self.assertEqual(
            {
                "ok": True,
                "op": "status",
                "sessions": sessions,
                "scan_errors": scan_errors,
                "tail_errors": tail_errors,
            },
            json.loads(body),
        )
        self.assertEqual(1, client.status_calls)

    def test_status_encoding_is_bounded(self):
        client = FakeClient([{"title": "x" * 256}])
        server = self.start(client)
        with mock.patch("sidecar.http_server.MAX_RESPONSE_BYTES", 128):
            status, _, body = self.request(
                server,
                "GET",
                "/api/v1/status",
                headers=self.authorized_headers(server),
            )
        self.assertEqual(502, status)
        self.assertEqual("upstream_error", json.loads(body)["error"])

    def test_event_stream_writes_ack_and_events(self):
        event = {
            "ts": "2026-08-23T12:00:00Z",
            "agent": "cursor-cli",
            "session_id": "one",
            "kind": "assistant",
            "text": "done",
        }
        server = self.start(FakeClient(events=[event]))
        response = self.raw_request(
            server,
            "GET /api/v1/events HTTP/1.1",
            ("Authorization: Bearer " + self.token(server),),
        )
        headers, body = response.split(b"\r\n\r\n", 1)
        lines = [json.loads(line) for line in body.splitlines()]

        self.assertIn(b"Content-Type: application/x-ndjson", headers)
        self.assertNotIn(b"Transfer-Encoding", headers)
        self.assertNotIn(b"Content-Length", headers)
        self.assertEqual({"ok": True, "op": "subscribe"}, lines[0])
        self.assertEqual(event, lines[1])

    def test_event_disconnect_cancels_subscription(self):
        client = BlockingClient()
        server = self.start(client)
        connection = socket.create_connection(
            ("127.0.0.1", server.port),
            timeout=2.0,
        )
        request = (
            "GET /api/v1/events HTTP/1.1\r\n"
            "Host: 127.0.0.1:{0}\r\n"
            "Authorization: Bearer {1}\r\n\r\n"
        ).format(server.port, self.token(server))
        connection.sendall(request.encode("ascii"))
        response = recv_until(connection, b'{"ok":true,"op":"subscribe"}\n')
        self.assertIn(b'{"ok":true,"op":"subscribe"}\n', response)

        connection.close()
        deadline = time.monotonic() + 2.0
        while client.cancelled < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(1, client.cancelled)


class PanelAndLifecycleTests(HttpServerTestCase):
    def test_panel_uses_nonce_memory_only_auth_and_safe_dom_updates(self):
        server = self.start()
        status, headers, body = self.request(server, "GET", "/")
        text = body.decode("utf-8")
        policy = headers["Content-Security-Policy"]
        nonce = re.search(r"script-src 'nonce-([^']+)'", policy).group(1)
        parser = PanelParser()
        parser.feed(text)
        form = parser.by_id("auth")
        token_input = parser.by_id("token")
        connect_button = parser.by_id("connect")

        self.assertEqual(200, status)
        self.assertIn('nonce="{}"'.format(nonce), text)
        self.assertIn('fetch("/api/v1/status"', text)
        self.assertIn('fetch("/api/v1/events"', text)
        self.assertIn(".textContent", text)
        self.assertIn("childElementCount>200", text)
        self.assertNotIn(".innerHTML", text)
        self.assertNotIn("localStorage", text)
        self.assertNotIn("sessionStorage", text)
        self.assertNotIn("document.cookie", text)
        self.assertNotIn("/api/v1/send", text)
        self.assertNotIn(self.token(server), text)
        self.assertEqual("post", form["method"])
        self.assertEqual("/", form["action"])
        self.assertEqual("off", form["autocomplete"])
        self.assertNotIn("name", token_input)
        self.assertEqual("off", token_input["autocomplete"])
        self.assertEqual("button", connect_button["type"])

    def test_accept_returning_after_close_never_starts_worker(self):
        accepted, peer = socket.socketpair()

        class BarrierListener:
            def __init__(self):
                self.accepting = threading.Event()
                self.closed = threading.Event()
                self.release = threading.Event()

            def accept(self):
                self.accepting.set()
                self.release.wait(2.0)
                return accepted, ("127.0.0.1", 1)

            def shutdown(self, how):
                del how
                self.closed.set()

            def close(self):
                self.closed.set()

        listener = BarrierListener()
        server = SidecarHttpServer(
            self.runtime,
            self.socket_path,
            client_factory=lambda **unused: FakeClient(),
        )
        self.servers.append(server)
        server._listener = listener
        server._running = True
        accept_thread = threading.Thread(target=server._accept_loop)
        server._accept_thread = accept_thread
        accept_thread.start()
        self.assertTrue(listener.accepting.wait(1.0))
        failures = []
        closer = threading.Thread(
            target=lambda: self._capture_close(server, failures)
        )
        closer.start()
        try:
            self.assertTrue(listener.closed.wait(1.0))
            self.assertTrue(closer.is_alive())
            listener.release.set()
            closer.join(2.0)
            accept_thread.join(2.0)
            peer.settimeout(1.0)
            self.assertEqual(b"", peer.recv(1))
        finally:
            listener.release.set()
            peer.close()
            closer.join(2.0)
            accept_thread.join(2.0)

        self.assertEqual([], failures)
        self.assertFalse(closer.is_alive())
        self.assertFalse(accept_thread.is_alive())
        self.assertEqual(set(), server._client_threads)
        self.assertEqual(set(), server._connections)

    @staticmethod
    def _capture_close(server, failures):
        try:
            server.close()
        except BaseException as error:
            failures.append(error)

    def test_fifth_event_stream_gets_503_and_close_cancels_all(self):
        client = BlockingClient()
        server = self.start(client)
        connections = []
        try:
            for _ in range(MAX_EVENT_STREAMS):
                connection = socket.create_connection(
                    ("127.0.0.1", server.port),
                    timeout=2.0,
                )
                request = (
                    "GET /api/v1/events HTTP/1.1\r\n"
                    "Host: 127.0.0.1:{0}\r\n"
                    "Authorization: Bearer {1}\r\n\r\n"
                ).format(server.port, self.token(server))
                connection.sendall(request.encode("ascii"))
                response = recv_until(
                    connection,
                    b'{"ok":true,"op":"subscribe"}\n',
                )
                self.assertIn(b'{"ok":true,"op":"subscribe"}\n', response)
                connections.append(connection)

            response = self.raw_request(
                server,
                "GET /api/v1/events HTTP/1.1",
                ("Authorization: Bearer " + self.token(server),),
            )
            self.assertIn(b"503 ", response.split(b"\r\n", 1)[0])
        finally:
            server.close()
            for connection in connections:
                connection.close()

        self.assertEqual(MAX_EVENT_STREAMS, client.started)
        self.assertEqual(MAX_EVENT_STREAMS, client.cancelled)
        self.assertFalse(server.port_path.exists())
        self.assertEqual(set(), server._connections)
        self.assertEqual(set(), server._client_threads)
        self.assertFalse(
            any(
                thread.is_alive()
                and thread.name == "agent-sidecar-http-disconnect"
                for thread in threading.enumerate()
            )
        )

    def test_close_timeout_is_explicit_and_retains_ownership_for_retry(self):
        server = self.start()
        release = threading.Event()

        def stuck_worker():
            release.wait(2.0)
            with server._clients_lock:
                server._client_threads.discard(threading.current_thread())

        worker = threading.Thread(target=stuck_worker)
        with server._clients_lock:
            server._client_threads.add(worker)
        worker.start()
        with mock.patch("sidecar.http_server.CLOSE_TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(HttpServerCloseError):
                server.close()

        self.assertTrue(server._closing)
        self.assertTrue(server.port_path.exists())
        descriptor = os.open(str(self.runtime), os.O_RDONLY)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)

        release.set()
        worker.join(1.0)
        server.close()
        self.assertFalse(server.port_path.exists())
        self.assertFalse(server._closing)

    def test_seventeenth_client_gets_503(self):
        server = self.start()
        connections = []
        try:
            for _ in range(MAX_CLIENTS):
                connection = socket.create_connection(
                    ("127.0.0.1", server.port),
                    timeout=2.0,
                )
                connection.sendall(b"GET / HTTP/1.1\r\n")
                connections.append(connection)
            deadline = time.monotonic() + 2.0
            while (
                len(server._client_threads) < MAX_CLIENTS
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

            excess = socket.create_connection(
                ("127.0.0.1", server.port),
                timeout=2.0,
            )
            try:
                response = recv_all(excess)
            finally:
                excess.close()
            self.assertIn(b"503 ", response.split(b"\r\n", 1)[0])
        finally:
            for connection in connections:
                connection.close()

    def test_start_failure_cleans_listener_metadata_and_can_restart(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        server = SidecarHttpServer(
            self.runtime,
            self.socket_path,
            port=port,
            client_factory=lambda **unused: FakeClient(),
        )
        self.servers.append(server)
        try:
            with self.assertRaises(OSError):
                server.start()
            self.assertFalse(server.port_path.exists())
        finally:
            blocker.close()

        server.start()
        self.assertEqual(port, server.port)
        self.assertTrue(server.port_path.exists())

    def test_only_ipv4_numeric_loopback_is_bound(self):
        server = self.start()
        self.assertEqual(socket.AF_INET, server._listener.family)
        self.assertEqual(
            ("127.0.0.1", server.port),
            server._listener.getsockname(),
        )
        self.assertEqual("http://127.0.0.1:{}".format(server.port), server.url)

        if socket.has_ipv6:
            probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                self.assertNotEqual(0, probe.connect_ex(("::1", server.port)))
            finally:
                probe.close()

    def test_second_server_cannot_replace_live_port_metadata(self):
        first = self.start()
        second = SidecarHttpServer(
            self.runtime,
            self.socket_path,
            client_factory=lambda **unused: FakeClient(),
        )
        self.servers.append(second)
        with self.assertRaises(HttpRuntimeError):
            second.start()
        record = json.loads(first.port_path.read_text(encoding="ascii"))
        self.assertEqual(first.port, record["port"])
        self.assertEqual(first._instance_nonce, record["nonce"])

    def test_close_lock_prevents_old_generation_removing_new_record(self):
        first = self.start()
        second = SidecarHttpServer(
            self.runtime,
            self.socket_path,
            client_factory=lambda **unused: FakeClient(),
        )
        self.servers.append(second)
        entered = threading.Event()
        release = threading.Event()
        original_remove = http_server._remove_owned_port

        def delayed_remove(*args):
            entered.set()
            release.wait(2.0)
            return original_remove(*args)

        failures = []
        with mock.patch(
            "sidecar.http_server._remove_owned_port",
            side_effect=delayed_remove,
        ):
            closer = threading.Thread(
                target=lambda: self._capture_close(first, failures)
            )
            closer.start()
            self.assertTrue(entered.wait(1.0))
            with self.assertRaises(HttpRuntimeError):
                second.start()
            release.set()
            closer.join(2.0)

        self.assertEqual([], failures)
        self.assertFalse(closer.is_alive())
        second.start()
        new_record = second.port_path.read_bytes()
        first.close()
        self.assertEqual(new_record, second.port_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
