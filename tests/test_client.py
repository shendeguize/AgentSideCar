import io
import json
import socket
import threading
import time
import unittest
from unittest import mock

from sidecar.client import (
    HttpPingInfo,
    MAX_RESPONSE_BYTES,
    PingInfo,
    SidecarClient,
    SidecarClientError,
)


class StubClient(SidecarClient):
    def __init__(self, response):
        super().__init__(socket_path="/tmp/unused-agent-sidecar.sock")
        self.response = response

    def _request(self, operation):
        self.asserted_operation = operation
        return self.response


class SidecarClientTests(unittest.TestCase):
    def test_ping_info_supports_old_and_http_enabled_daemons(self):
        client = StubClient(
            {
                "ok": True,
                "op": "ping",
                "pid": 41,
                "version": "0.3",
            }
        )

        self.assertEqual(
            PingInfo(
                pid=41,
                version="0.3",
                http=HttpPingInfo(enabled=False),
            ),
            client.ping_info(),
        )

        client.response = {
            "ok": True,
            "op": "ping",
            "pid": 42,
            "version": "0.4",
            "http": {
                "enabled": True,
                "host": "127.0.0.1",
                "port": 43210,
            },
        }
        self.assertEqual(
            PingInfo(
                pid=42,
                version="0.4",
                http=HttpPingInfo(
                    enabled=True,
                    host="127.0.0.1",
                    port=43210,
                ),
            ),
            client.ping_info(),
        )

    def test_ping_remains_raw_and_ping_info_rejects_malformed_http(self):
        response = {
            "ok": True,
            "op": "ping",
            "pid": 42,
            "http": {"enabled": True, "host": "127.0.0.1", "port": 0},
        }
        client = StubClient(response)

        self.assertIs(response, client.ping())
        with self.assertRaises(SidecarClientError) as raised:
            client.ping_info()
        self.assertEqual("invalid_response", raised.exception.code)

    def test_status_preserves_list_result_and_exposes_diagnostics(self):
        sessions = [{"agent": "claude", "session_id": "one"}]
        scan_errors = [
            {
                "adapter": "broken",
                "stage": "discover",
                "message": "unreadable",
                "exception_type": "OSError",
                "session_id": None,
            }
        ]
        tail_errors = [
            {
                "agent": "cursor-cli",
                "session_id": "two",
                "code": "CursorChatSourceError",
            }
        ]
        client = StubClient(
            {
                "ok": True,
                "op": "status",
                "sessions": sessions,
                "scan_errors": scan_errors,
                "tail_errors": tail_errors,
            }
        )

        self.assertEqual(sessions, client.status())
        self.assertEqual("status", client.asserted_operation)
        self.assertEqual(scan_errors, client.scan_errors)
        self.assertEqual(tail_errors, client.tail_errors)

        exposed = client.scan_errors
        exposed[0]["message"] = "changed"
        self.assertEqual("unreadable", client.scan_errors[0]["message"])
        exposed_tail = client.tail_errors
        exposed_tail[0]["code"] = "changed"
        self.assertEqual(
            "CursorChatSourceError",
            client.tail_errors[0]["code"],
        )
        with self.assertRaises(AttributeError):
            client.scan_errors = []
        with self.assertRaises(AttributeError):
            client.tail_errors = []

    def test_status_without_diagnostics_supports_older_daemon_and_clears_latest(self):
        client = StubClient(
            {
                "ok": True,
                "op": "status",
                "sessions": [],
                "scan_errors": [{"message": "old"}],
                "tail_errors": [{"code": "old"}],
            }
        )
        client.status()
        client.response = {"ok": True, "op": "status", "sessions": []}

        self.assertEqual([], client.status())
        self.assertEqual([], client.scan_errors)
        self.assertEqual([], client.tail_errors)

    def test_default_capacity_accepts_valid_status_frame_above_2mib(self):
        payload = {
            "ok": True,
            "op": "status",
            "sessions": [{"title": "x" * (2 * 1024 * 1024)}],
            "scan_errors": [],
            "tail_errors": [],
        }
        frame = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        self.assertGreater(len(frame), 2 * 1024 * 1024)
        self.assertLessEqual(len(frame), MAX_RESPONSE_BYTES)
        client = SidecarClient(socket_path="/tmp/unused-agent-sidecar.sock")

        self.assertEqual(
            payload,
            client._read_response(io.BytesIO(frame)),
        )

    def test_response_capacity_is_bounded_and_rejects_oversize_frame(self):
        with self.assertRaises(ValueError):
            SidecarClient(
                socket_path="/tmp/unused-agent-sidecar.sock",
                max_response_bytes=MAX_RESPONSE_BYTES + 1,
            )

        client = SidecarClient(
            socket_path="/tmp/unused-agent-sidecar.sock",
            max_response_bytes=32,
        )
        stream = io.BytesIO(b"x" * 33 + b"\n")
        with self.assertRaises(SidecarClientError) as raised:
            client._read_response(stream)

        self.assertEqual("response_too_large", raised.exception.code)
        self.assertEqual(33, stream.tell())

    def test_cancelable_subscribe_stops_idle_socket_within_one_second(self):
        client_socket, daemon_socket = socket.socketpair()
        client = SidecarClient(
            socket_path="/tmp/unused-agent-sidecar.sock",
            timeout=0.05,
        )
        cancel = threading.Event()
        finished = threading.Event()
        failures = []

        def consume():
            try:
                list(client.subscribe(cancel_event=cancel))
            except BaseException as error:
                failures.append(error)
            finally:
                finished.set()

        with mock.patch.object(client, "_connect", return_value=client_socket):
            worker = threading.Thread(target=consume)
            worker.start()
            try:
                self.assertEqual(
                    b'{"op":"subscribe"}\n',
                    daemon_socket.recv(1024),
                )
                daemon_socket.sendall(b'{"ok":true,"op":"subscribe"}\n')
                time.sleep(0.06)
                started = time.monotonic()
                cancel.set()
                worker.join(timeout=1.0)
                elapsed = time.monotonic() - started
            finally:
                daemon_socket.close()
                worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(finished.is_set())
        self.assertLess(elapsed, 1.0)
        self.assertEqual([], failures)

    def test_pre_cancelled_subscribe_does_not_connect(self):
        client = SidecarClient(socket_path="/tmp/unused-agent-sidecar.sock")
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(client, "_connect") as connect:
            self.assertEqual([], list(client.subscribe(cancel_event=cancel)))
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
