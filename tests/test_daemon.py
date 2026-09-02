import errno
import hashlib
import http.client
import io
import json
import multiprocessing
import os
import socket
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import sidecar
import sidecar.daemon as daemon_module
from sidecar import bus
from sidecar.adapters.base import Adapter
from sidecar.adapters.cursor import CursorAdapter
from sidecar.adapters.dsh import ReplayPage
from sidecar.adapters.replay import ReplayUnsupported
from sidecar.client import SidecarClient, SidecarClientError
from sidecar.cursor_chat import (
    default_snapshot_broker,
    reset_default_snapshot_broker,
)
from sidecar.daemon import DaemonAlreadyRunning, SidecarDaemon
from sidecar.daemon_log import DaemonLog, DaemonLogError, LOG_NAME
from sidecar.model import Event, Session, Status
from sidecar.scan import Scanner
from sidecar.tail import SessionTailer


class FakeAdapter(Adapter):
    name = "fake"
    agent_names = ("fake",)

    def discover(self, home):
        del home
        return []

    def normalize(self, record, session):
        text = record.get("text")
        if not isinstance(text, str):
            return []
        return [
            Event(
                "2026-08-23T04:00:00+08:00",
                session.agent,
                session.session_id,
                str(record.get("type") or "message"),
                text,
            )
        ]


class FakeReplayAdapter(FakeAdapter):
    def __init__(self, records=(), failure=None):
        self.records = list(records)
        self.failure = failure
        self.calls = []

    def replay(self, session, after_seq=None, max_records=1024, **kwargs):
        del kwargs
        self.calls.append((session.session_id, after_seq, max_records))
        if self.failure is not None:
            raise self.failure
        floor = 0 if after_seq is None else after_seq
        matched = [
            record
            for record in self.records
            if not isinstance(record, dict)
            or (
                isinstance(record.get("seq"), int)
                and record["seq"] > floor
            )
        ]
        return matched[:max_records]

    def normalize(self, record, session):
        if record.get("type") == "mapping":
            return [
                {
                    "agent": session.agent,
                    "session_id": session.session_id,
                    "kind": "mapping",
                    "text": record.get("text"),
                }
            ]
        return super().normalize(record, session)


class BudgetStoppedReplayAdapter(FakeReplayAdapter):
    """Serve one record per page with an honest ``exhausted`` signal.

    Mimics the dsh adapter ending a page on its retained-byte or decode-time
    budget with fewer records than ``max_records``.
    """

    def replay(self, session, after_seq=None, max_records=1024, **kwargs):
        del kwargs
        self.calls.append((session.session_id, after_seq, max_records))
        floor = 0 if after_seq is None else after_seq
        matched = [
            record
            for record in self.records
            if isinstance(record.get("seq"), int) and record["seq"] > floor
        ]
        page = matched[: min(1, max_records)]
        return ReplayPage(page, exhausted=len(page) == len(matched))


class StalledReplayAdapter(FakeReplayAdapter):
    """Mimic a replay that budget-stops before finding any matching record."""

    def replay(self, session, after_seq=None, max_records=1024, **kwargs):
        del session, after_seq, max_records, kwargs
        return ReplayPage([], exhausted=False)


class FlakyAliasAdapter(FakeAdapter):
    name = "registry-name"
    agent_names = ("emitted-agent",)

    def __init__(self, sessions=()):
        self.sessions = list(sessions)
        self.failure = None

    def discover(self, home):
        del home
        if self.failure == "complete":
            raise RuntimeError("discover unavailable")
        if self.failure == "partial":
            sessions = list(self.sessions)

            def partial():
                if sessions:
                    yield sessions[0]
                raise RuntimeError("discover interrupted")

            return partial()
        return list(self.sessions)

    def infer_status(self, session, now=None):
        del now
        return session.status


class FakeScanner:
    def __init__(self, sessions=()):
        self.sessions = list(sessions)
        self.errors = []
        self.calls = 0
        self.lock = threading.Lock()

    def scan(self):
        with self.lock:
            self.calls += 1
            return list(self.sessions)


def run_daemon_process(
    runtime,
    stop_event,
    result_queue,
    stale_classified=None,
    resume_startup=None,
):
    if stale_classified is not None and resume_startup is not None:
        socket_is_live = daemon_module._socket_is_live

        def delayed_socket_is_live(path, timeout=0.1):
            live = socket_is_live(path, timeout)
            if not live:
                stale_classified.set()
                if not resume_startup.wait(5.0):
                    raise RuntimeError("startup test gate timed out")
            return live

        daemon_module._socket_is_live = delayed_socket_is_live

    daemon = SidecarDaemon(
        scanner=FakeScanner(),
        runtime_dir=Path(runtime),
        active_interval=0.02,
        idle_interval=0.02,
        max_idle_interval=0.03,
    )
    failure = []

    def serve():
        try:
            daemon.serve_forever(stop_event)
        except Exception as error:
            failure.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    deadline = time.monotonic() + 5.0
    while thread.is_alive() and not daemon.ready.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        daemon.wait_until_ready(min(0.05, remaining))

    if daemon.ready.is_set():
        result_queue.put(("ready", os.getpid()))
        stop_event.wait(10.0)
        daemon.stop(timeout=2.0)
    else:
        error_name = failure[0].__class__.__name__ if failure else "StartupTimeout"
        result_queue.put(("error", error_name))
    thread.join(2.0)


def create_cursor_chat_store(path):
    path.parent.mkdir(parents=True)
    message = json.dumps(
        {"role": "user", "content": "<user_query>Daemon generation</user_query>"},
        separators=(",", ":"),
    ).encode("utf-8")
    message_id = hashlib.sha256(message).hexdigest()
    root = b"\x0a\x20" + bytes.fromhex(message_id)
    root_id = hashlib.sha256(root).hexdigest()
    metadata = json.dumps(
        {
            "agentId": "daemon-generation",
            "latestRootBlobId": root_id,
            "name": "Daemon generation",
            "mode": "agent",
            "createdAt": 1_787_430_000_000,
        },
        separators=(",", ":"),
    ).encode("utf-8")

    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.executemany(
            "INSERT INTO blobs (id, data) VALUES (?, ?)",
            ((message_id, message), (root_id, root)),
        )
        connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("0", metadata.hex()),
        )
        connection.commit()
    finally:
        connection.close()


def make_session(
    transcript,
    *,
    session_id="one",
    updated_at=None,
    status=Status.WORKING,
    agent="fake"
):
    return Session(
        agent=agent,
        session_id=session_id,
        project=str(transcript.parent),
        transcript=str(transcript),
        updated_at=time.time() if updated_at is None else updated_at,
        title="fake session",
        status=status,
    )


class DaemonValidationTests(unittest.TestCase):
    def test_missing_fcntl_fails_only_when_runtime_lock_is_acquired(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            daemon = SidecarDaemon(scanner=FakeScanner(), runtime_dir=runtime)

            with mock.patch("sidecar.daemon._fcntl", None):
                with self.assertRaisesRegex(
                    daemon_module.RuntimePathError,
                    "unsupported.*fcntl required",
                ):
                    daemon.serve_forever()

            self.assertFalse((runtime / "daemon.sock").exists())
            self.assertFalse((runtime / "daemon.pid").exists())
            self.assertFalse((runtime / "daemon.lock").exists())

    def test_runtime_paths_bounds_and_http_ports_are_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict(
                os.environ,
                {daemon_module.RUNTIME_ENV: "$HOME/custom-sidecar"},
            ):
                self.assertEqual(
                    Path(os.path.expandvars("$HOME/custom-sidecar")).expanduser(),
                    daemon_module.default_runtime_dir(),
                )

            invalid = (
                {"active_interval": 0},
                {"idle_interval": 2, "max_idle_interval": 1},
                {"idle_backoff": 0.5},
                {"client_timeout": 0},
                {"request_bytes": 0},
                {"shutdown_timeout": "invalid"},
                {"shutdown_timeout": float("inf")},
                {"tail_recent_seconds": -1},
                {"max_event_polls": 0},
                {"http_port": True},
                {"http_port": -1},
                {"http_port": 65536},
            )
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        SidecarDaemon(runtime_dir=root, **arguments)

            socket_path = root / "nested" / "sidecar.sock"
            daemon = SidecarDaemon(socket_path=socket_path)
            self.assertEqual(socket_path.parent, daemon.runtime_dir)
            self.assertEqual(socket_path, daemon.socket_path)

    def test_lifecycle_helpers_report_bounded_errors_and_http_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=Path(temporary),
                shutdown_timeout=0.1,
            )
            self.assertGreater(daemon.current_interval, 0)
            self.assertEqual([], daemon.sessions)
            self.assertEqual([], daemon.scan_errors)
            self.assertEqual([], daemon.tail_errors)
            self.assertIsNone(daemon.shutdown_diagnostic)
            self.assertEqual({"enabled": False}, daemon._http_ping_payload())
            daemon._http_bound_port = 43123
            self.assertEqual(
                {"enabled": True, "host": "127.0.0.1", "port": 43123},
                daemon._http_ping_payload(),
            )
            self.assertEqual(
                {"ok": False, "error": {"code": "bad", "message": "request"}},
                daemon._error("bad", "request"),
            )
            self.assertEqual("safe.code-1", daemon._safe_log_error_code("safe.code-1"))
            for value in (None, "", "../unsafe", "x" * 129):
                self.assertEqual("log_error", daemon._safe_log_error_code(value))

            with mock.patch("sidecar.daemon.sys.stderr", new=object()):
                daemon._record_log_error("../unsafe")
                daemon._record_log_error("ignored-second-error")
            self.assertEqual("log_error", daemon.log_error)
            self.assertIn("daemon log unavailable", daemon.shutdown_diagnostic)
            self.assertEqual("log_error", daemon._status_response()["diagnostics"][0]["code"])

            with self.assertRaises(ValueError):
                daemon.stop(timeout="invalid")
            with self.assertRaises(ValueError):
                daemon.stop(timeout=float("nan"))
            self.assertFalse(daemon._wait_for_stop(0, None))
            external_stop = threading.Event()
            external_stop.set()
            self.assertTrue(daemon._wait_for_stop(1, external_stop))

            failing_logger = mock.Mock()
            failing_logger.append.side_effect = OSError("write failed")
            failing_logger.error_code = "disk_full"
            daemon._daemon_log = failing_logger
            daemon._logging_disabled = False
            with mock.patch.object(daemon, "_record_log_error") as record_error:
                daemon._log_event("event", durable=True)
            self.assertTrue(daemon._logging_disabled)
            record_error.assert_called_once_with("disk_full")

            failing_logger.close.side_effect = OSError("close failed")
            daemon._daemon_log = failing_logger
            with mock.patch.object(daemon, "_record_log_error") as record_error:
                daemon._close_daemon_log()
            record_error.assert_called_once_with("log_close_failed")

            self.assertTrue(daemon._claim_log_diagnostic(("kind", "one")))
            self.assertFalse(daemon._claim_log_diagnostic(("kind", "one")))
            with mock.patch.object(daemon, "_log_event") as log_event:
                daemon._log_scan_errors(
                    [{"adapter": "fake", "stage": "discover", "exception_type": "Boom"}]
                )
                daemon._log_tail_errors(
                    [{"agent": "fake", "session_id": "one", "code": "read"}]
                )
            self.assertEqual(2, log_event.call_count)

    def test_http_close_success_failure_and_json_wire_encoding(self):
        with tempfile.TemporaryDirectory() as temporary:
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=Path(temporary),
            )
            self.assertTrue(daemon._close_http_server())

            healthy = mock.Mock()
            daemon._http_server = healthy
            daemon._http_bound_port = 1
            self.assertTrue(daemon._close_http_server())
            healthy.close.assert_called_once_with()
            self.assertIsNone(daemon._http_server)

            broken = mock.Mock()
            broken.close.side_effect = RuntimeError("close failed")
            daemon._http_server = broken
            with mock.patch.object(daemon, "_log_event") as log_event:
                self.assertFalse(daemon._close_http_server())
            self.assertTrue(daemon.shutdown_timed_out)
            log_event.assert_called_once()

            connection = mock.Mock()
            daemon._write_json(connection, {"value": Path("/safe")})
            payload = connection.sendall.call_args.args[0]
            self.assertEqual({"value": "/safe"}, json.loads(payload))

            broken_client = mock.Mock()
            broken_client.shutdown.side_effect = OSError("shutdown failed")
            broken_client.close.side_effect = OSError("close failed")
            daemon._client_sockets.add(broken_client)
            daemon._close_clients()
            broken_client.shutdown.assert_called_once()
            broken_client.close.assert_called_once()
            daemon._join_scan_thread()
            daemon._remove_owned_paths()

    def test_client_protocol_read_size_type_and_disconnect_failures_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=Path(temporary),
                request_bytes=16,
            )

            def handle(reads, close_error=False):
                connection = mock.MagicMock()
                stream = mock.Mock()
                stream.readline.side_effect = reads
                if close_error:
                    stream.close.side_effect = OSError("close failed")
                connection.makefile.return_value = stream
                daemon._handle_client(connection)
                return connection

            handle([OSError("read failed")])
            oversized = handle([b"x" * 17])
            self.assertTrue(oversized.sendall.called)
            non_object = handle([b"[]\n", b""], close_error=True)
            response = json.loads(non_object.sendall.call_args_list[0].args[0])
            self.assertEqual("invalid_request", response["error"]["code"])

            subscription = mock.Mock()
            daemon.event_bus.subscribe = mock.Mock(return_value=subscription)
            disconnected = mock.Mock()
            disconnected.sendall.side_effect = OSError("disconnected")
            daemon._serve_subscription(disconnected)
            subscription.close.assert_called_once_with()


class DaemonIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.transcript = self.root / "events.jsonl"
        self.transcript.write_text(
            '{"type":"old","text":"history"}\n',
            encoding="utf-8",
        )
        self.adapter = FakeAdapter()
        self.scanner = FakeScanner([make_session(self.transcript)])
        self.stop_event = threading.Event()
        self.daemon = SidecarDaemon(
            scanner=self.scanner,
            runtime_dir=self.runtime,
            active_interval=0.02,
            idle_interval=0.03,
            max_idle_interval=0.05,
            client_timeout=0.5,
            subscriber_queue_size=2,
            tailer_factory=lambda session, from_start=False: SessionTailer(
                session,
                adapter=self.adapter,
                from_start=from_start,
            ),
        )
        self.thread = self.daemon.start_in_thread(self.stop_event)
        if not self.daemon.wait_until_ready(2.0):
            self.fail("daemon did not become ready")

    def tearDown(self):
        self.stop_event.set()
        self.daemon.stop()
        self.thread.join(2.0)
        self.assertFalse(self.thread.is_alive())
        self.temporary.cleanup()

    def test_socket_mode_ping_status_and_malformed_requests(self):
        mode = stat.S_IMODE(self.daemon.socket_path.stat().st_mode)
        self.assertEqual(0o600, mode)

        client = SidecarClient(runtime_dir=self.runtime, timeout=0.5)
        ping = client.ping()
        self.assertTrue(ping["ok"])
        self.assertEqual("ping", ping["op"])
        self.assertEqual([self.scanner.sessions[0].to_dict()], client.status())

        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(1.0)
        connection.connect(str(self.daemon.socket_path))
        with connection:
            stream = connection.makefile("rwb", buffering=0)
            stream.write(b"not-json\n")
            malformed = json.loads(stream.readline())
            stream.write(b'{"op":"not-supported"}\n')
            unknown = json.loads(stream.readline())
            stream.write(b'{"op":"ping"}\n')
            healthy = json.loads(stream.readline())

        self.assertFalse(malformed["ok"])
        self.assertEqual("malformed_json", malformed["error"]["code"])
        self.assertEqual("unknown_op", unknown["error"]["code"])
        self.assertTrue(healthy["ok"])

    def test_ping_reports_the_source_version_now_on_disk(self):
        # A long-lived daemon keeps serving whatever it imported at start, so
        # an upgrade in place leaves it answering with code the operator
        # believes they replaced. Reporting the version currently on disk
        # beside the loaded one is what lets any reader see that drift.
        client = SidecarClient(runtime_dir=self.runtime, timeout=0.5)

        ping = client.ping()

        self.assertEqual(sidecar.__version__, ping["version"])
        self.assertEqual(sidecar.__version__, ping["source_version"])

    def test_ping_omits_the_source_version_when_it_cannot_be_read(self):
        # Packaged forms (zipapp) have no readable source tree. Claiming a
        # version there would be a guess, and claiming drift would be a lie.
        client = SidecarClient(runtime_dir=self.runtime, timeout=0.5)

        with mock.patch.object(daemon_module, "installed_source_version", return_value=None):
            ping = client.ping()

        self.assertNotIn("source_version", ping)

    def test_ping_reports_source_changed_when_the_tree_no_longer_matches(self):
        # Version numbers only move on release, so between releases a daemon
        # can be several edits behind its own tree while both sides report
        # the same version — the exact case that reads as "no drift".
        client = SidecarClient(runtime_dir=self.runtime, timeout=0.5)

        with mock.patch.object(daemon_module, "SOURCE_DRIFT_TTL_SECONDS", 0.0):
            self.assertNotIn("source_changed", client.ping())
            with mock.patch.object(daemon_module, "source_digest", return_value="different"):
                changed = client.ping()

        self.assertTrue(changed["source_changed"])

    def test_ping_rereads_the_tree_on_an_interval_not_on_every_request(self):
        # A ping is the supervisor's health poll, so walking the package on
        # each one would make a diagnostic into a cost.
        client = SidecarClient(runtime_dir=self.runtime, timeout=0.5)
        real = daemon_module.source_digest

        with mock.patch.object(
            daemon_module,
            "source_digest",
            side_effect=lambda *args, **kwargs: real(*args, **kwargs),
        ) as digest:
            for _ in range(3):
                self.assertTrue(client.ping()["ok"])

        self.assertEqual(1, digest.call_count)

    def test_source_digest_follows_content_not_timestamps(self):
        package = self.root / "tree"
        package.mkdir()
        (package / "a.py").write_text("value = 1\n", encoding="utf-8")
        (package / "notes.txt").write_text("not code\n", encoding="utf-8")

        original = daemon_module.source_digest(package)

        # A rewrite with identical content must not read as a change: a
        # false stale warning teaches operators to ignore the real one.
        (package / "a.py").write_text("value = 1\n", encoding="utf-8")
        os.utime(package / "a.py", (0, 0))
        self.assertEqual(original, daemon_module.source_digest(package))

        (package / "a.py").write_text("value = 2\n", encoding="utf-8")
        self.assertNotEqual(original, daemon_module.source_digest(package))

        # A new module is a change; an unrelated file is not.
        (package / "a.py").write_text("value = 1\n", encoding="utf-8")
        (package / "notes.txt").write_text("still not code\n", encoding="utf-8")
        self.assertEqual(original, daemon_module.source_digest(package))
        (package / "b.py").write_text("value = 1\n", encoding="utf-8")
        self.assertNotEqual(original, daemon_module.source_digest(package))

        self.assertIsNone(daemon_module.source_digest(self.root / "absent"))

    def test_installed_source_version_parses_the_literal_without_importing(self):
        package = self.root / "pkg"
        package.mkdir()
        (package / "__init__.py").write_text(
            'raise RuntimeError("import me and the daemon dies")\n'
            '__version__ = "9.9.9"\n',
            encoding="utf-8",
        )

        self.assertEqual(
            "9.9.9",
            daemon_module.installed_source_version(package / "__init__.py"),
        )
        self.assertIsNone(
            daemon_module.installed_source_version(package / "missing.py")
        )

    def test_installed_source_version_declines_to_guess_a_non_literal(self):
        package = self.root / "odd"
        package.mkdir()
        (package / "computed.py").write_text(
            "other = 1\n__version__ = str(other)\n",
            encoding="utf-8",
        )
        (package / "silent.py").write_text("other = 1\n", encoding="utf-8")

        # A computed version cannot be read without running the file, and a
        # tree that declares none has nothing to report; both stay unknown
        # rather than becoming a fabricated drift signal.
        self.assertIsNone(daemon_module.installed_source_version(package / "computed.py"))
        self.assertIsNone(daemon_module.installed_source_version(package / "silent.py"))

    def test_source_digest_stays_unknown_when_a_module_cannot_be_read(self):
        package = self.root / "unreadable"
        (package / "shadow.py").mkdir(parents=True)

        self.assertIsNone(daemon_module.source_digest(package))

    def test_status_response_larger_than_2mib_roundtrips_as_jsonl(self):
        updated_at = time.time()
        sessions = [
            Session(
                agent="fake",
                session_id="bulk",
                project=str(self.root),
                transcript=str(self.root / "events.log"),
                updated_at=updated_at,
                title="large status payload " + ("x" * (2 * 1024 * 1024)),
                status=Status.IDLE,
            )
        ]
        expected = [session.to_dict() for session in sessions]
        response_frame = (
            json.dumps(
                {
                    "ok": True,
                    "op": "status",
                    "sessions": expected,
                    "scan_errors": [],
                    "tail_errors": [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            + b"\n"
        )
        self.assertGreater(len(response_frame), 2 * 1024 * 1024)
        self.assertLess(len(response_frame), 32 * 1024 * 1024)

        self.scanner.sessions = sessions
        self.daemon.scan_once()

        client = SidecarClient(runtime_dir=self.runtime, timeout=2.0)
        self.assertEqual(expected, client.status())

    def test_status_surfaces_safe_tail_errors_without_scan_error_regression(self):
        active = self.scanner.sessions[0]
        tailer = self.daemon._tailer_pool._tailers[
            (active.agent, active.session_id)
        ]
        tailer.errors.extend(
            [
                "CursorChatSourceError",
                "RuntimeError: /private/transcript secret content",
            ]
        )
        self.scanner.sessions = [
            make_session(
                self.transcript,
                updated_at=active.updated_at + 1.0,
            )
        ]

        self.daemon.scan_once()
        client = SidecarClient(runtime_dir=self.runtime, timeout=0.5)
        client.status()

        self.assertEqual([], client.scan_errors)
        self.assertEqual(
            [
                {
                    "agent": "fake",
                    "session_id": "one",
                    "code": "CursorChatSourceError",
                },
                {
                    "agent": "fake",
                    "session_id": "one",
                    "code": "RuntimeError",
                },
            ],
            client.tail_errors,
        )
        self.assertNotIn(
            "private",
            json.dumps(client.tail_errors),
        )

    def test_subscriber_receives_only_new_normalized_event(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2.0)
        connection.connect(str(self.daemon.socket_path))
        with connection:
            stream = connection.makefile("rwb", buffering=0)
            stream.write(b'{"op":"subscribe"}\n')
            acknowledgement = json.loads(stream.readline())
            self.assertEqual({"ok": True, "op": "subscribe"}, acknowledgement)

            with self.transcript.open("a", encoding="utf-8") as output:
                output.write('{"type":"assistant","text":"new event"}\n')
                output.flush()
                os.fsync(output.fileno())

            event = json.loads(stream.readline())

        self.assertEqual("fake", event["agent"])
        self.assertEqual("one", event["session_id"])
        self.assertEqual("assistant", event["kind"])
        self.assertEqual("new event", event["text"])
        self.assertNotEqual("history", event["text"])

    def test_ignored_poll_batch_does_not_strand_later_valid_event(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2.0)
        connection.connect(str(self.daemon.socket_path))
        with connection:
            stream = connection.makefile("rwb", buffering=0)
            stream.write(b'{"op":"subscribe"}\n')
            # Byte-exact legacy acknowledgement: old clients depend on this
            # exact serialization, not just on JSON-equivalent content.
            self.assertEqual(
                b'{"ok":true,"op":"subscribe"}\n',
                stream.readline(),
            )

            with self.transcript.open("a", encoding="utf-8") as output:
                for _ in range(256):
                    output.write('{"type":"ignored"}\n')
                output.write(
                    '{"type":"assistant","text":"after ignored rows"}\n'
                )
                output.flush()
                os.fsync(output.fileno())

            event = json.loads(stream.readline())

        self.assertEqual("assistant", event["kind"])
        self.assertEqual("after ignored rows", event["text"])

    def _open_protocol_stream(self, connection):
        connection.settimeout(2.0)
        connection.connect(str(self.daemon.socket_path))
        return connection.makefile("rwb", buffering=0)

    def _register_replay_adapter(self, adapter):
        import sidecar.adapters as adapters_package

        saved = dict(adapters_package.registry)

        def restore():
            adapters_package.registry.clear()
            adapters_package.registry.update(saved)

        self.addCleanup(restore)
        adapters_package.register_adapter(adapter)
        return adapter

    def test_subscribe_agents_filter_streams_only_selected_agents(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with connection:
            stream = self._open_protocol_stream(connection)
            stream.write(b'{"op":"subscribe","agents":["other"]}\n')
            acknowledgement = json.loads(stream.readline())
            self.assertEqual(
                {"ok": True, "op": "subscribe", "agents": ["other"]},
                acknowledgement,
            )

            self.daemon._publish_event(
                Event(
                    "2026-08-24T20:00:00+08:00",
                    "fake",
                    "one",
                    "assistant",
                    "unwanted noise",
                )
            )
            self.daemon._publish_event(
                Event(
                    "2026-08-24T20:00:01+08:00",
                    "other",
                    "two",
                    "assistant",
                    "wanted event",
                )
            )

            event = json.loads(stream.readline())

        self.assertEqual("other", event["agent"])
        self.assertEqual("two", event["session_id"])
        self.assertEqual("wanted event", event["text"])

    def test_subscribe_null_agents_keeps_full_stream_and_legacy_ack(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with connection:
            stream = self._open_protocol_stream(connection)
            stream.write(b'{"op":"subscribe","agents":null}\n')
            # An explicit null allowlist must keep the legacy acknowledgement
            # byte-for-byte identical to a request without the field.
            self.assertEqual(
                b'{"ok":true,"op":"subscribe"}\n',
                stream.readline(),
            )

            self.daemon._publish_event(
                Event(
                    "2026-08-24T20:00:00+08:00",
                    "fake",
                    "one",
                    "assistant",
                    "first",
                )
            )
            self.daemon._publish_event(
                Event(
                    "2026-08-24T20:00:01+08:00",
                    "other",
                    "two",
                    "assistant",
                    "second",
                )
            )

            agents = [
                json.loads(stream.readline())["agent"]
                for _ in range(2)
            ]

        self.assertEqual(["fake", "other"], agents)

    def test_subscribe_invalid_agents_reject_without_disconnecting(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with connection:
            stream = self._open_protocol_stream(connection)
            payloads = (
                b'{"op":"subscribe","agents":[]}\n',
                b'{"op":"subscribe","agents":"dsh"}\n',
                b'{"op":"subscribe","agents":[""]}\n',
                b'{"op":"subscribe","agents":[1]}\n',
                b'{"op":"subscribe","agents":{"dsh":true}}\n',
            )
            for payload in payloads:
                stream.write(payload)
                response = json.loads(stream.readline())
                self.assertFalse(response["ok"], payload)
                self.assertEqual(
                    "invalid_request",
                    response["error"]["code"],
                    payload,
                )
            stream.write(b'{"op":"ping"}\n')
            self.assertTrue(json.loads(stream.readline())["ok"])

    def test_replay_pages_seq_filtered_events_with_cursor_and_bounds(self):
        adapter = self._register_replay_adapter(
            FakeReplayAdapter(
                [
                    {"seq": index, "type": "assistant", "text": "event {}".format(index)}
                    for index in range(1, 6)
                ]
            )
        )
        client = SidecarClient(runtime_dir=self.runtime, timeout=2.0)

        full = client.replay("one")
        self.assertEqual("replay", full["op"])
        self.assertEqual("fake", full["agent"])
        self.assertEqual(0, full["after_seq"])
        self.assertEqual(
            ["event 1", "event 2", "event 3", "event 4", "event 5"],
            [event["text"] for event in full["events"]],
        )
        self.assertEqual(5, full["count"])
        self.assertEqual(5, full["last_seq"])
        self.assertFalse(full["truncated"])
        self.assertEqual(
            ("one", 0, daemon_module.REPLAY_DEFAULT_LIMIT),
            adapter.calls[0],
        )

        incremental = client.replay("one", 3)
        self.assertEqual(3, incremental["after_seq"])
        self.assertEqual(
            ["event 4", "event 5"],
            [event["text"] for event in incremental["events"]],
        )
        self.assertEqual(5, incremental["last_seq"])
        self.assertFalse(incremental["truncated"])

        first_page = client.replay("one", limit=2)
        self.assertEqual(
            ["event 1", "event 2"],
            [event["text"] for event in first_page["events"]],
        )
        self.assertEqual(2, first_page["last_seq"])
        self.assertTrue(first_page["truncated"])

        second_page = client.replay(
            "one",
            first_page["last_seq"],
            limit=2,
        )
        self.assertEqual(
            ["event 3", "event 4"],
            [event["text"] for event in second_page["events"]],
        )
        self.assertTrue(second_page["truncated"])

        empty = client.replay("one", 5, limit=2)
        self.assertEqual([], empty["events"])
        self.assertEqual(0, empty["count"])
        self.assertIsNone(empty["last_seq"])
        self.assertFalse(empty["truncated"])

    def test_replay_budget_stopped_pages_stay_truncated_until_true_end(self):
        # Regression for the truncated false negative: when the adapter's
        # byte/time budget ends a page with fewer records than `limit`, the
        # daemon must still report truncated:true so paging consumers keep
        # fetching the retained events instead of silently stopping early.
        self._register_replay_adapter(
            BudgetStoppedReplayAdapter(
                [
                    {
                        "seq": index,
                        "type": "assistant",
                        "text": "event {}".format(index),
                    }
                    for index in (1, 2, 3)
                ]
            )
        )
        client = SidecarClient(runtime_dir=self.runtime, timeout=2.0)

        pages = []
        cursor = 0
        for _ in range(4):
            page = client.replay("one", cursor, limit=8)
            pages.append(page)
            if not page["truncated"]:
                break
            cursor = page["last_seq"]

        self.assertEqual(
            [["event 1"], ["event 2"], ["event 3"]],
            [[event["text"] for event in page["events"]] for page in pages],
        )
        self.assertEqual([1, 2, 3], [page["last_seq"] for page in pages])
        self.assertEqual(
            [True, True, False],
            [page["truncated"] for page in pages],
        )

    def test_replay_early_stopped_page_without_cursor_is_not_truncated(self):
        # A budget stop before any matching record leaves no cursor to page
        # with, so reporting truncated:true would only send consumers into a
        # no-progress retry of the identical page.
        self._register_replay_adapter(StalledReplayAdapter())
        client = SidecarClient(runtime_dir=self.runtime, timeout=2.0)

        page = client.replay("one")

        self.assertEqual([], page["events"])
        self.assertEqual(0, page["count"])
        self.assertIsNone(page["last_seq"])
        self.assertFalse(page["truncated"])

    def test_replay_skips_non_record_values_and_accepts_mapping_events(self):
        self._register_replay_adapter(
            FakeReplayAdapter(
                [
                    {"seq": 1, "type": "assistant", "text": "typed event"},
                    "junk-not-a-record",
                    {"seq": 2, "type": "mapping", "text": "mapping event"},
                    {"seq": 3, "type": "assistant"},
                ]
            )
        )
        client = SidecarClient(runtime_dir=self.runtime, timeout=2.0)

        response = client.replay("one")

        self.assertEqual(
            ["typed event", "mapping event"],
            [event["text"] for event in response["events"]],
        )
        self.assertEqual("mapping", response["events"][1]["kind"])
        self.assertEqual(3, response["last_seq"])
        self.assertFalse(response["truncated"])

    def test_replay_unknown_session_unsupported_agent_and_failure(self):
        client = SidecarClient(runtime_dir=self.runtime, timeout=2.0)

        with self.assertRaises(SidecarClientError) as unsupported:
            client.replay("one")
        self.assertEqual("replay_unsupported", unsupported.exception.code)

        adapter = self._register_replay_adapter(
            FakeReplayAdapter(
                [{"seq": 1, "type": "assistant", "text": "kept"}]
            )
        )
        with self.assertRaises(SidecarClientError) as missing:
            client.replay("missing")
        self.assertEqual("unknown_session", missing.exception.code)

        adapter.failure = RuntimeError("/private/transcript boom")
        with self.assertRaises(SidecarClientError) as failed:
            client.replay("one")
        self.assertEqual("replay_failed", failed.exception.code)
        self.assertNotIn("private", str(failed.exception))

    def test_replay_unsupported_transcript_kind_is_a_capability_answer(self):
        # An adapter that replays other sessions but not this transcript
        # shape (Cursor's SQLite chat store) must not read as a failure:
        # the board treats replay_unsupported as "this source has nothing
        # to offer" and keeps the rest of the timeline healthy.
        adapter = self._register_replay_adapter(
            FakeReplayAdapter([{"seq": 1, "type": "assistant", "text": "kept"}])
        )
        adapter.failure = ReplayUnsupported("cursor-chat-sqlite")
        client = SidecarClient(runtime_dir=self.runtime, timeout=2.0)

        with self.assertRaises(SidecarClientError) as unsupported:
            client.replay("one")

        self.assertEqual("replay_unsupported", unsupported.exception.code)
        self.assertNotIn("sqlite", str(unsupported.exception))

    def test_replay_invalid_parameters_reject_without_disconnecting(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with connection:
            stream = self._open_protocol_stream(connection)
            payloads = (
                b'{"op":"replay"}\n',
                b'{"op":"replay","session_id":""}\n',
                b'{"op":"replay","session_id":5}\n',
                b'{"op":"replay","session_id":"one","after_seq":-1}\n',
                b'{"op":"replay","session_id":"one","after_seq":true}\n',
                b'{"op":"replay","session_id":"one","after_seq":"3"}\n',
                b'{"op":"replay","session_id":"one","limit":0}\n',
                b'{"op":"replay","session_id":"one","limit":1025}\n',
                b'{"op":"replay","session_id":"one","limit":true}\n',
            )
            for payload in payloads:
                stream.write(payload)
                response = json.loads(stream.readline())
                self.assertFalse(response["ok"], payload)
                self.assertEqual(
                    "invalid_request",
                    response["error"]["code"],
                    payload,
                )
            stream.write(b'{"op":"ping"}\n')
            self.assertTrue(json.loads(stream.readline())["ok"])


class DaemonHttpIntegrationTests(unittest.TestCase):
    def _start(self, root, *, scanner=None, http_port=0, **kwargs):
        daemon = SidecarDaemon(
            scanner=FakeScanner() if scanner is None else scanner,
            runtime_dir=root / "runtime",
            active_interval=0.02,
            idle_interval=0.02,
            max_idle_interval=0.03,
            http_port=http_port,
            **kwargs
        )
        thread = daemon.start_in_thread()
        self.assertTrue(daemon.wait_until_ready(2.0))
        return daemon, thread

    @staticmethod
    def _stop(daemon, thread):
        daemon.stop(timeout=2.0)
        thread.join(2.0)

    @staticmethod
    def _token(runtime):
        return (runtime / "http.token").read_text(encoding="ascii").strip()

    def test_default_off_has_disabled_ping_and_no_http_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            daemon, thread = self._start(root, http_port=None)
            runtime = root / "runtime"
            try:
                info = SidecarClient(runtime_dir=runtime).ping_info()
                self.assertFalse(info.http.enabled)
                self.assertIsNone(daemon._http_server)
                self.assertFalse((runtime / "http.token").exists())
                self.assertFalse((runtime / "http.port").exists())
            finally:
                self._stop(daemon, thread)

    def test_http_factory_starts_after_scan_and_closes_before_unix_cleanup(self):
        observations = []

        class FakeHttpServer:
            port = 43123

            def __init__(self, runtime_dir, socket_path, port):
                self.runtime_dir = runtime_dir
                self.socket_path = socket_path
                self.configured_port = port

            def start(self):
                observations.append(
                    (
                        "start",
                        self.socket_path.exists(),
                        (self.runtime_dir / "daemon.pid").exists(),
                        scanner.calls,
                    )
                )

            def close(self):
                observations.append(
                    (
                        "close",
                        self.socket_path.exists(),
                        (self.runtime_dir / "daemon.pid").exists(),
                    )
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scanner = FakeScanner()
            stop = threading.Event()
            stop.set()
            daemon = SidecarDaemon(
                scanner=scanner,
                runtime_dir=root / "runtime",
                http_port=0,
                http_server_factory=FakeHttpServer,
            )
            daemon.serve_forever(stop)

        self.assertEqual(("start", True, True, 1), observations[0])
        self.assertEqual(("close", True, True), observations[1])

    def test_real_http_status_and_events_reuse_integrated_unix_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "events.jsonl"
            transcript.write_text("", encoding="utf-8")
            scanner = FakeScanner([make_session(transcript)])
            adapter = FakeAdapter()
            daemon, thread = self._start(
                root,
                scanner=scanner,
                tailer_factory=lambda session, from_start=False: SessionTailer(
                    session,
                    adapter=adapter,
                    from_start=from_start,
                ),
            )
            runtime = root / "runtime"
            stream = None
            try:
                info = SidecarClient(runtime_dir=runtime).ping_info()
                self.assertTrue(info.http.enabled)
                self.assertEqual("127.0.0.1", info.http.host)
                self.assertGreater(info.http.port, 0)
                token = self._token(runtime)

                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    info.http.port,
                    timeout=2.0,
                )
                connection.request(
                    "GET",
                    "/api/v1/status",
                    headers={"Authorization": "Bearer " + token},
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(200, response.status)
                self.assertEqual(
                    [scanner.sessions[0].to_dict()],
                    payload["sessions"],
                )

                stream = socket.create_connection(
                    ("127.0.0.1", info.http.port),
                    timeout=2.0,
                )
                request = (
                    "GET /api/v1/events HTTP/1.1\r\n"
                    "Host: 127.0.0.1:{0}\r\n"
                    "Authorization: Bearer {1}\r\n\r\n"
                ).format(info.http.port, token)
                stream.sendall(request.encode("ascii"))
                received = bytearray()
                while b'{"ok":true,"op":"subscribe"}\n' not in received:
                    received.extend(stream.recv(65536))

                with transcript.open("a", encoding="utf-8") as output:
                    output.write('{"type":"assistant","text":"via HTTP"}\n')
                    output.flush()
                    os.fsync(output.fileno())

                while b'"text":"via HTTP"' not in received:
                    received.extend(stream.recv(65536))
                self.assertIn(b'"kind":"assistant"', received)
                self.assertIn(b'"session_id":"one"', received)
            finally:
                if stream is not None:
                    stream.close()
                self._stop(daemon, thread)
            self.assertFalse((runtime / "http.port").exists())

    def test_active_http_stream_stops_without_thread_or_port_residue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            daemon, thread = self._start(root)
            runtime = root / "runtime"
            info = SidecarClient(runtime_dir=runtime).ping_info()
            connection = socket.create_connection(
                ("127.0.0.1", info.http.port),
                timeout=2.0,
            )
            request = (
                "GET /api/v1/events HTTP/1.1\r\n"
                "Host: 127.0.0.1:{0}\r\n"
                "Authorization: Bearer {1}\r\n\r\n"
            ).format(info.http.port, self._token(runtime))
            connection.sendall(request.encode("ascii"))
            received = bytearray()
            while b'{"ok":true,"op":"subscribe"}\n' not in received:
                received.extend(connection.recv(65536))

            self._stop(daemon, thread)
            connection.settimeout(1.0)
            self.assertEqual(b"", connection.recv(1))
            connection.close()
            self.assertFalse((runtime / "http.port").exists())
            self.assertFalse(
                any(
                    worker.is_alive()
                    and worker.name.startswith("agent-sidecar-http-")
                    for worker in threading.enumerate()
                )
            )

    def test_http_port_zero_restarts_same_daemon_instance_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            daemon, first = self._start(root)
            first_info = SidecarClient(runtime_dir=runtime).ping_info()
            self._stop(daemon, first)
            self.assertFalse((runtime / "http.port").exists())

            second = daemon.start_in_thread()
            self.assertTrue(daemon.wait_until_ready(2.0))
            try:
                second_info = SidecarClient(runtime_dir=runtime).ping_info()
                self.assertTrue(second_info.http.enabled)
                self.assertGreater(first_info.http.port, 0)
                self.assertGreater(second_info.http.port, 0)
            finally:
                self._stop(daemon, second)
            self.assertFalse((runtime / "http.port").exists())

    def test_http_start_failure_unwinds_unix_pid_pool_and_port_record(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                runtime = Path(temporary) / "runtime"
                daemon = SidecarDaemon(
                    scanner=FakeScanner(),
                    runtime_dir=runtime,
                    http_port=blocker.getsockname()[1],
                )
                with self.assertRaises(OSError):
                    daemon.serve_forever()
                self.assertFalse((runtime / "daemon.sock").exists())
                self.assertFalse((runtime / "daemon.pid").exists())
                self.assertFalse((runtime / "http.port").exists())
                self.assertTrue(daemon._tailer_pool.state.closed)
                self.assertIsNone(daemon._listener)
                self.assertIsNone(daemon._http_server)
        finally:
            blocker.close()

    def test_http_close_failure_sets_shutdown_diagnostic_and_retries(self):
        class FlakyHttpServer:
            port = 43124

            def __init__(self, *args, **kwargs):
                del args, kwargs
                self.close_calls = 0

            def start(self):
                pass

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("bounded close")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stop = threading.Event()
            stop.set()
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=root / "runtime",
                http_port=0,
                http_server_factory=FlakyHttpServer,
            )
            daemon.serve_forever(stop)

            self.assertTrue(daemon.shutdown_timed_out)
            self.assertIn("HTTP shutdown", daemon.shutdown_diagnostics[0])
            self.assertIsNone(daemon._http_server)
            self.assertFalse((root / "runtime" / "daemon.sock").exists())
            self.assertFalse((root / "runtime" / "daemon.pid").exists())


class DaemonPersistentLogIntegrationTests(unittest.TestCase):
    @staticmethod
    def _read_records(runtime):
        records = []
        for index in (2, 1, 0):
            suffix = "" if index == 0 else ".{}".format(index)
            path = runtime / "{}{}".format(LOG_NAME, suffix)
            if not path.exists():
                continue
            records.extend(
                json.loads(line)
                for line in path.read_text(encoding="ascii").splitlines()
            )
        return records

    def test_lifecycle_log_closes_and_same_instance_restarts(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=runtime,
                active_interval=0.01,
                idle_interval=0.01,
                max_idle_interval=0.02,
            )

            for _ in range(2):
                thread = daemon.start_in_thread()
                self.assertTrue(daemon.wait_until_ready(1.0))
                self.assertTrue(daemon.stop(timeout=1.0))
                thread.join(1.0)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(daemon._daemon_log)

            records = self._read_records(runtime)
            events = [record["event"] for record in records]
            self.assertEqual(2, events.count("startup"))
            self.assertEqual(2, events.count("ready"))
            self.assertEqual(2, events.count("shutdown"))
            self.assertTrue(
                all(
                    record["timed_out"] is False
                    for record in records
                    if record["event"] == "shutdown"
                )
            )
            self.assertEqual(0o700, stat.S_IMODE(runtime.stat().st_mode))
            self.assertEqual(
                0o600,
                stat.S_IMODE((runtime / LOG_NAME).stat().st_mode),
            )

            unlocked = DaemonLog(runtime).open()
            unlocked.close()

    def test_simultaneous_starters_only_socket_owner_logs_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            ownership_barrier = threading.Barrier(2)
            errors = []

            class SimultaneousDaemon(SidecarDaemon):
                def _acquire_runtime_lock(self):
                    ownership_barrier.wait(timeout=2.0)
                    super()._acquire_runtime_lock()

            daemons = [
                SimultaneousDaemon(
                    scanner=FakeScanner(),
                    runtime_dir=runtime,
                    active_interval=0.01,
                    idle_interval=0.01,
                    max_idle_interval=0.02,
                )
                for _ in range(2)
            ]

            def serve(daemon):
                try:
                    daemon.serve_forever()
                except Exception as error:
                    errors.append(error)

            threads = [
                threading.Thread(target=serve, args=(daemon,))
                for daemon in daemons
            ]
            for thread in threads:
                thread.start()
            try:
                deadline = time.monotonic() + 2.0
                while (
                    not any(daemon.ready.is_set() for daemon in daemons)
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.005)
                winners = [daemon for daemon in daemons if daemon.ready.is_set()]
                self.assertEqual(1, len(winners))
                winner = winners[0]
                self.assertTrue(
                    SidecarClient(runtime_dir=runtime, timeout=0.5).ping()["ok"]
                )
                self.assertTrue(winner.stop(timeout=1.0))
            finally:
                try:
                    ownership_barrier.abort()
                except threading.BrokenBarrierError:
                    pass
                for daemon in daemons:
                    daemon.stop(timeout=1.0)
                for thread in threads:
                    thread.join(2.0)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], DaemonAlreadyRunning)
            events = [
                record["event"]
                for record in self._read_records(runtime)
            ]
            self.assertEqual(1, events.count("startup"))
            self.assertEqual(1, events.count("ready"))
            self.assertEqual(1, events.count("shutdown"))

    def test_partial_pidfile_failure_unwinds_without_opening_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=runtime,
            )
            real_write = os.write
            writes = 0

            def partial_then_fail(descriptor, payload):
                nonlocal writes
                writes += 1
                if writes == 1:
                    return real_write(descriptor, payload[:1])
                raise OSError(errno.ENOSPC, "private pidfile failure")

            with mock.patch(
                "sidecar.daemon.os.write",
                side_effect=partial_then_fail,
            ):
                with self.assertRaises(OSError):
                    daemon.serve_forever()

            self.assertFalse((runtime / "daemon.sock").exists())
            self.assertFalse((runtime / "daemon.pid").exists())
            self.assertFalse((runtime / LOG_NAME).exists())
            self.assertIsNone(daemon._daemon_log)

    def test_log_open_failure_keeps_daemon_available_and_is_path_free(self):
        class FailingLog:
            def __init__(self, *args, **kwargs):
                del args, kwargs

            def open(self):
                raise DaemonLogError("unsafe_log")

        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            stderr = io.StringIO()
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=runtime,
                active_interval=0.01,
                idle_interval=0.01,
                max_idle_interval=0.02,
                daemon_log_factory=FailingLog,
            )
            with mock.patch("sidecar.daemon.sys.stderr", stderr):
                thread = daemon.start_in_thread()
                self.assertTrue(daemon.wait_until_ready(1.0))
                response = daemon._status_response()
                self.assertTrue(
                    SidecarClient(runtime_dir=runtime, timeout=0.5).ping()["ok"]
                )
                self.assertTrue(daemon.stop(timeout=1.0))
                thread.join(1.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual("unsafe_log", daemon.log_error)
            self.assertEqual(
                [
                    {
                        "component": "daemon_log",
                        "event": "log_error",
                        "code": "unsafe_log",
                    }
                ],
                response["diagnostics"],
            )
            self.assertIn("log_error: unsafe_log", stderr.getvalue())
            self.assertNotIn(temporary, stderr.getvalue())
            self.assertTrue(
                any("unsafe_log" in item for item in daemon.shutdown_diagnostics)
            )

    def test_unsupported_locking_disables_log_but_daemon_serves(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            stop = threading.Event()
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=runtime,
                active_interval=0.01,
                idle_interval=0.01,
                max_idle_interval=0.02,
            )
            stderr = io.StringIO()
            with (
                mock.patch("sidecar.daemon_log._fcntl", None),
                mock.patch("sidecar.daemon.sys.stderr", stderr),
            ):
                thread = daemon.start_in_thread(stop)
                self.assertTrue(daemon.wait_until_ready(1.0))
                self.assertTrue(
                    SidecarClient(runtime_dir=runtime, timeout=0.5).ping()["ok"]
                )
                stop.set()
                self.assertTrue(daemon.stop(timeout=1.0))
                thread.join(1.0)

            self.assertFalse(thread.is_alive())
            self.assertEqual("unsupported_platform", daemon.log_error)
            self.assertIn("unsupported_platform", stderr.getvalue())
            self.assertFalse((runtime / LOG_NAME).exists())
            self.assertFalse((runtime / "daemon.log.lock").exists())

    def test_write_failure_disables_once_and_surfaces_status(self):
        instances = []

        class FailingWriter:
            error_code = "disk_full"

            def __init__(self, *args, **kwargs):
                del args, kwargs
                self.append_calls = 0
                self.closed = False
                instances.append(self)

            def open(self):
                return self

            def append(self, *args, **kwargs):
                del args, kwargs
                self.append_calls += 1
                return False

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            stderr = io.StringIO()
            stop = threading.Event()
            stop.set()
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=runtime,
                daemon_log_factory=FailingWriter,
            )
            with mock.patch("sidecar.daemon.sys.stderr", stderr):
                daemon.serve_forever(stop)

            self.assertEqual(1, instances[0].append_calls)
            self.assertTrue(instances[0].closed)
            self.assertEqual("disk_full", daemon.log_error)
            self.assertEqual(1, stderr.getvalue().count("log_error"))
            self.assertEqual(
                "disk_full",
                daemon._status_response()["diagnostics"][0]["code"],
            )

    def test_scan_and_tail_diagnostics_use_bounded_eviction_dedupe(self):
        class CaptureLog:
            error_code = None

            def __init__(self):
                self.records = []

            def append(self, event, **fields):
                self.records.append((event, fields))
                return True

        daemon = SidecarDaemon(scanner=FakeScanner())
        capture = CaptureLog()
        daemon._daemon_log = capture
        scan = {
            "adapter": "cursor",
            "stage": "discover",
            "exception_type": "ReadError",
            "message": "/private/canary secret",
        }
        tail = {
            "agent": "cursor-cli",
            "session_id": "private-session-id",
            "code": "TailError",
        }
        daemon._log_scan_errors((scan, scan))
        daemon._log_tail_errors((tail, tail))
        self.assertEqual(["scan_error", "tail_error"], [item[0] for item in capture.records])
        self.assertNotIn("message", capture.records[0][1])

        daemon._reset_log_dedupe()
        capture.records.clear()
        with mock.patch("sidecar.daemon.MAX_LOG_ERROR_DEDUPE", 2):
            for code in ("OneError", "TwoError", "ThreeError", "OneError"):
                daemon._log_scan_errors(
                    (
                        {
                            "adapter": "cursor",
                            "stage": "discover",
                            "exception_type": code,
                        },
                    )
                )
        self.assertEqual(4, len(capture.records))
        self.assertLessEqual(len(daemon._log_dedupe_order), 2)


class ScanFailureToleranceTests(unittest.TestCase):
    def test_complete_discover_failure_retains_alias_session_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "events.jsonl"
            transcript.write_text(
                '{"type":"old","text":"history"}\n',
                encoding="utf-8",
            )
            session = make_session(
                transcript,
                updated_at=800.0,
                agent="emitted-agent",
            )
            adapter = FlakyAliasAdapter([session])
            scanner = Scanner([adapter], home=root)
            from_starts = []

            def make_tailer(value, from_start=False):
                from_starts.append(from_start)
                return SessionTailer(
                    value,
                    adapter=adapter,
                    from_start=from_start,
                )

            daemon = SidecarDaemon(
                scanner=scanner,
                runtime_dir=root / "runtime",
                tail_recent_seconds=100,
                tailer_factory=make_tailer,
            )
            subscription = daemon.event_bus.subscribe()
            key = ("emitted-agent", "one")

            with mock.patch("sidecar.daemon.time.time", return_value=900.0):
                daemon._scan_once(initial=True)
            self.assertEqual([False], from_starts)

            adapter.sessions = [
                make_session(
                    transcript,
                    updated_at=800.0,
                    status=Status.IDLE,
                    agent="emitted-agent",
                )
            ]
            with mock.patch("sidecar.daemon.time.time", return_value=1_000.0):
                daemon.scan_once()

            adapter.failure = "complete"
            daemon.scan_once()
            self.assertEqual({key}, daemon.index.keys())
            self.assertEqual("registry-name", daemon.scan_errors[0]["adapter"])
            self.assertEqual("discover", daemon.scan_errors[0]["stage"])

            with transcript.open("a", encoding="utf-8") as output:
                output.write('{"type":"assistant","text":"after recovery"}\n')
            adapter.failure = None
            adapter.sessions = [
                make_session(
                    transcript,
                    updated_at=1_001.0,
                    agent="emitted-agent",
                )
            ]
            with mock.patch("sidecar.daemon.time.time", return_value=1_001.0):
                daemon.scan_once()

            self.assertEqual(
                "after recovery",
                subscription.get(timeout=0.1)["text"],
            )
            self.assertTrue(subscription.queue.empty())
            self.assertEqual([False, False], from_starts)
            self.assertEqual([], daemon.scan_errors)
            subscription.close()

    def test_partial_discover_failure_retains_missing_session_until_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "first.jsonl"
            second_path = root / "second.jsonl"
            first_path.write_text(
                '{"type":"old","text":"first history"}\n',
                encoding="utf-8",
            )
            second_path.write_text(
                '{"type":"old","text":"second history"}\n',
                encoding="utf-8",
            )
            first = make_session(
                first_path,
                session_id="first",
                agent="emitted-agent",
            )
            second = make_session(
                second_path,
                session_id="second",
                agent="emitted-agent",
            )
            adapter = FlakyAliasAdapter([first, second])
            scanner = Scanner([adapter], home=root)
            from_starts = []

            def make_tailer(value, from_start=False):
                from_starts.append(from_start)
                return SessionTailer(
                    value,
                    adapter=adapter,
                    from_start=from_start,
                )

            daemon = SidecarDaemon(
                scanner=scanner,
                runtime_dir=root / "runtime",
                tailer_factory=make_tailer,
            )
            subscription = daemon.event_bus.subscribe()

            daemon._scan_once(initial=True)
            adapter.failure = "partial"
            daemon.scan_once()
            self.assertEqual(2, len(daemon.index))
            self.assertEqual("discover", daemon.scan_errors[0]["stage"])

            with second_path.open("a", encoding="utf-8") as output:
                output.write('{"type":"assistant","text":"partial recovery"}\n')
            adapter.failure = None
            adapter.sessions = [
                first,
                make_session(
                    second_path,
                    session_id="second",
                    updated_at=second.updated_at + 1,
                    agent="emitted-agent",
                ),
            ]
            daemon.scan_once()

            event = subscription.get(timeout=0.1)
            self.assertEqual("second", event["session_id"])
            self.assertEqual("partial recovery", event["text"])
            self.assertTrue(subscription.queue.empty())
            self.assertEqual([False, False], from_starts)
            subscription.close()


class DaemonCursorChatGenerationTests(unittest.TestCase):
    def setUp(self):
        reset_default_snapshot_broker()

    def tearDown(self):
        reset_default_snapshot_broker()

    def test_scan_generation_shares_discovery_checkpoint_and_changed_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = (
                root
                / ".cursor"
                / "chats"
                / "cwd-hash"
                / "session-id"
                / "store.db"
            )
            create_cursor_chat_store(store)
            os.utime(store, ns=(1_000_000_000, 1_000_000_000))

            broker = default_snapshot_broker()
            clock = [0.0]
            discovery_depths = []

            class AdvancingCursorAdapter(CursorAdapter):
                def discover(self, home):
                    sessions = list(super().discover(home))
                    discovery_depths.append(broker.scan_depth)
                    clock[0] += broker.ttl_seconds + 1.0
                    return sessions

            daemon = SidecarDaemon(
                scanner=Scanner([AdvancingCursorAdapter()], home=root),
                runtime_dir=root / "runtime",
                tail_recent_seconds=1.0,
            )
            try:
                with mock.patch.object(
                    broker,
                    "_clock",
                    side_effect=lambda: clock[0],
                ):
                    daemon._scan_once(initial=True)

                    self.assertEqual([1], discovery_depths)
                    self.assertEqual(0, broker.scan_depth)
                    self.assertEqual(1, broker.stats.snapshot_loads)
                    self.assertEqual(1, broker.stats.cache_hits)
                    self.assertEqual(
                        {("cursor-cli", "session-id")},
                        set(daemon._tailer_pool.state.checkpoints),
                    )

                    details = store.stat()
                    os.utime(
                        store,
                        ns=(
                            details.st_atime_ns,
                            details.st_mtime_ns + 1_000_000,
                        ),
                    )
                    daemon.scan_once()

                    self.assertEqual([1, 1], discovery_depths)
                    self.assertEqual(0, broker.scan_depth)
                    self.assertEqual(2, broker.stats.snapshot_loads)
            finally:
                daemon._tailer_pool.close()

    def test_scan_generation_exits_after_scanner_and_tailer_failures(self):
        broker = default_snapshot_broker()
        daemon = SidecarDaemon(scanner=FakeScanner())

        with (
            mock.patch.object(broker, "_clock", return_value=0.0),
            mock.patch.object(
                daemon.scanner,
                "scan",
                side_effect=RuntimeError("scanner failed"),
            ),
        ):
            self.assertEqual((False, False), daemon.scan_once())

        self.assertEqual(0, broker.scan_depth)
        scanner_generation = broker.stats.generation

        with (
            mock.patch.object(broker, "_clock", return_value=0.0),
            mock.patch.object(
                daemon._tailer_pool,
                "refresh",
                side_effect=RuntimeError("tailer failed"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "tailer failed"):
                daemon.scan_once()

        self.assertEqual(0, broker.scan_depth)
        self.assertGreater(broker.stats.generation, scanner_generation)


class ScanSerializationTests(unittest.TestCase):
    def test_public_scan_once_serializes_scanner_ownership(self):
        class OverlapScanner:
            errors = []

            def __init__(self):
                self.active = 0
                self.max_active = 0
                self.lock = threading.Lock()

            def scan(self):
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                time.sleep(0.02)
                with self.lock:
                    self.active -= 1
                return []

        scanner = OverlapScanner()
        daemon = SidecarDaemon(scanner=scanner)
        threads = [
            threading.Thread(target=daemon.scan_once)
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(1, scanner.max_active)


class DaemonLifecycleTests(unittest.TestCase):
    def test_stop_waits_for_active_generation_not_delayed_previous_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=Path(temporary) / "runtime",
                active_interval=0.01,
                idle_interval=0.01,
                max_idle_interval=0.02,
            )
            old_at_completion_gap = threading.Event()
            new_at_completion_gap = threading.Event()
            release_old_completion = threading.Event()
            release_new_completion = threading.Event()
            finish_lock = threading.Lock()
            finish_count = 0
            finish_run = daemon._finish_run

            def controlled_finish(run_done):
                nonlocal finish_count
                with finish_lock:
                    finish_count += 1
                    invocation = finish_count
                with daemon._serving_lock:
                    daemon._serving = False
                    daemon._serve_thread = None
                if invocation == 1:
                    old_at_completion_gap.set()
                    release_old_completion.wait(2.0)
                else:
                    new_at_completion_gap.set()
                    release_new_completion.wait(2.0)
                finish_run(run_done)

            first = None
            second = None
            stopper = None
            stop_result = []
            try:
                with mock.patch.object(
                    daemon,
                    "_finish_run",
                    side_effect=controlled_finish,
                ):
                    first = daemon.start_in_thread()
                    self.assertTrue(daemon.wait_until_ready(1.0))
                    self.assertFalse(daemon.stop(timeout=0.0))
                    self.assertTrue(old_at_completion_gap.wait(1.0))

                    second = daemon.start_in_thread()
                    self.assertTrue(daemon.wait_until_ready(1.0))
                    stopper = threading.Thread(
                        target=lambda: stop_result.append(
                            daemon.stop(timeout=1.0)
                        )
                    )
                    stopper.start()
                    self.assertTrue(new_at_completion_gap.wait(1.0))

                    release_old_completion.set()
                    first.join(1.0)
                    self.assertFalse(first.is_alive())
                    time.sleep(0.05)
                    self.assertTrue(stopper.is_alive())
                    self.assertEqual([], stop_result)

                    release_new_completion.set()
                    stopper.join(1.0)
                    second.join(1.0)
            finally:
                release_old_completion.set()
                release_new_completion.set()
                daemon.stop(timeout=1.0)
                for thread in (stopper, first, second):
                    if thread is not None:
                        thread.join(1.0)

            self.assertEqual([True], stop_result)
            self.assertFalse(stopper.is_alive())
            self.assertFalse(second.is_alive())

    def test_shutdown_waits_for_blocked_poll_and_reports_timeout(self):
        class ChangingScanner:
            errors = []

            def __init__(self, transcript):
                self.transcript = transcript
                self.calls = 0

            def scan(self):
                self.calls += 1
                return [
                    make_session(
                        self.transcript,
                        updated_at=time.time() + self.calls,
                    )
                ]

        class BlockingTailer:
            has_pending_records = False

            def __init__(self, session):
                self.session = session
                self.errors = []
                self.poll_started = threading.Event()
                self.poll_release = threading.Event()
                self.close_started = threading.Event()
                self.order = []

            def poll(self):
                self.order.append("poll_started")
                self.poll_started.set()
                self.poll_release.wait(2.0)
                self.order.append("poll_exited")
                return []

            def close(self):
                self.order.append("close_started")
                self.close_started.set()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "events.jsonl"
            transcript.write_text("", encoding="utf-8")
            tailers = []

            def factory(session):
                tailer = BlockingTailer(session)
                tailers.append(tailer)
                return tailer

            daemon = SidecarDaemon(
                scanner=ChangingScanner(transcript),
                runtime_dir=root / "runtime",
                active_interval=0.01,
                idle_interval=0.01,
                max_idle_interval=0.02,
                shutdown_timeout=0.05,
                tailer_factory=factory,
            )
            thread = daemon.start_in_thread()
            stopped = False
            try:
                self.assertTrue(daemon.wait_until_ready(1.0))
                self.assertTrue(tailers[0].poll_started.wait(1.0))
                scan_thread = daemon._scan_thread
                self.assertIsNotNone(scan_thread)

                self.assertFalse(daemon.stop(timeout=0.01))
                self.assertTrue(thread.is_alive())
                self.assertTrue(scan_thread.is_alive())
                self.assertFalse(tailers[0].close_started.is_set())

                deadline = time.monotonic() + 0.5
                while (
                    not daemon.shutdown_timed_out
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.005)
                self.assertTrue(daemon.shutdown_timed_out)
                self.assertFalse(tailers[0].close_started.is_set())

                tailers[0].poll_release.set()
                stopped = daemon.stop(timeout=1.0)
                thread.join(1.0)
            finally:
                if tailers:
                    tailers[0].poll_release.set()
                if not stopped:
                    daemon.stop(timeout=1.0)
                thread.join(1.0)

            self.assertTrue(stopped)
            self.assertFalse(thread.is_alive())
            self.assertFalse(scan_thread.is_alive())
            self.assertIsNone(daemon._scan_thread)
            self.assertEqual(
                ["poll_started", "poll_exited", "close_started"],
                tailers[0].order,
            )

            daemon.scanner = FakeScanner()
            restart = daemon.start_in_thread()
            self.assertTrue(daemon.wait_until_ready(1.0))
            self.assertFalse(daemon.shutdown_timed_out)
            self.assertTrue(daemon.stop(timeout=1.0))
            restart.join(1.0)
            self.assertFalse(restart.is_alive())

    def test_normal_shutdown_closes_pool_and_instance_restarts(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=runtime,
                active_interval=0.02,
                idle_interval=0.02,
                max_idle_interval=0.03,
            )
            first_stop = threading.Event()
            first_thread = daemon.start_in_thread(first_stop)
            self.assertTrue(daemon.wait_until_ready(2.0))

            with mock.patch.object(
                daemon._tailer_pool,
                "close",
                wraps=daemon._tailer_pool.close,
            ) as close:
                first_stop.set()
                daemon.stop()
                first_thread.join(2.0)
                self.assertFalse(first_thread.is_alive())
                close.assert_called_once_with()

            self.assertTrue(daemon._tailer_pool.state.closed)
            second_stop = threading.Event()
            second_thread = daemon.start_in_thread(second_stop)
            self.assertTrue(daemon.wait_until_ready(2.0))
            self.assertFalse(daemon._tailer_pool.state.closed)
            self.assertTrue(
                SidecarClient(runtime_dir=runtime, timeout=0.5).ping()["ok"]
            )

            second_stop.set()
            daemon.stop()
            second_thread.join(2.0)
            self.assertFalse(second_thread.is_alive())
            self.assertTrue(daemon._tailer_pool.state.closed)

    def test_exceptional_shutdown_cleanup_survives_pool_close_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=runtime,
            )
            with (
                mock.patch.object(
                    daemon,
                    "_scan_once",
                    side_effect=RuntimeError("scan failed"),
                ),
                mock.patch.object(
                    daemon,
                    "_close_clients",
                    wraps=daemon._close_clients,
                ) as close_clients,
                mock.patch.object(
                    daemon._tailer_pool,
                    "close",
                    side_effect=RuntimeError("close failed"),
                ) as close_pool,
            ):
                with self.assertRaisesRegex(RuntimeError, "scan failed"):
                    daemon.serve_forever()

            close_clients.assert_called_once_with()
            close_pool.assert_called_once_with()
            self.assertIsNone(daemon._listener)
            self.assertIsNone(daemon._scan_thread)
            self.assertFalse(daemon._serving)
            self.assertFalse((runtime / "daemon.sock").exists())
            self.assertFalse((runtime / "daemon.pid").exists())
            subscription = daemon.event_bus.subscribe()
            with self.assertRaises(bus.SubscriptionClosed):
                subscription.get(timeout=0.01)


class StaleRuntimeTests(unittest.TestCase):
    def test_concurrent_stale_startup_keeps_one_reachable_owner(self):
        context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            runtime.mkdir()
            socket_path = runtime / "daemon.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(socket_path))
            stale.close()
            (runtime / "daemon.pid").write_text("999999\n", encoding="ascii")

            stale_classified = context.Event()
            resume_startup = context.Event()
            owner_stop = context.Event()
            contender_stop = context.Event()
            owner_results = context.Queue()
            contender_results = context.Queue()
            owner = context.Process(
                target=run_daemon_process,
                args=(
                    str(runtime),
                    owner_stop,
                    owner_results,
                    stale_classified,
                    resume_startup,
                ),
            )
            contender = context.Process(
                target=run_daemon_process,
                args=(str(runtime), contender_stop, contender_results),
            )
            replacement = None
            replacement_thread = None
            try:
                owner.start()
                self.assertTrue(stale_classified.wait(5.0))

                contender.start()
                self.assertEqual(
                    ("error", "DaemonAlreadyRunning"),
                    contender_results.get(timeout=5.0),
                )
                contender.join(5.0)
                self.assertFalse(contender.is_alive())

                resume_startup.set()
                owner_state, owner_pid = owner_results.get(timeout=5.0)
                self.assertEqual("ready", owner_state)
                self.assertEqual(
                    owner_pid,
                    int((runtime / "daemon.pid").read_text(encoding="ascii")),
                )
                first_ping = SidecarClient(runtime_dir=runtime, timeout=1.0).ping()
                self.assertTrue(first_ping["ok"])
                self.assertEqual(owner_pid, first_ping["pid"])

                owner.terminate()
                owner.join(5.0)
                self.assertFalse(owner.is_alive())

                replacement = SidecarDaemon(
                    scanner=FakeScanner(),
                    runtime_dir=runtime,
                    active_interval=0.02,
                    idle_interval=0.02,
                    max_idle_interval=0.03,
                )
                replacement_thread = replacement.start_in_thread()
                self.assertTrue(replacement.wait_until_ready(2.0))
                replacement_ping = SidecarClient(
                    runtime_dir=runtime,
                    timeout=1.0,
                ).ping()
                self.assertTrue(replacement_ping["ok"])
                self.assertEqual(os.getpid(), replacement_ping["pid"])
                self.assertEqual(
                    0o600,
                    stat.S_IMODE((runtime / "daemon.lock").stat().st_mode),
                )
            finally:
                resume_startup.set()
                if owner.is_alive():
                    owner_stop.set()
                if contender.is_alive():
                    contender_stop.set()
                if replacement is not None:
                    replacement.stop(timeout=2.0)
                if replacement_thread is not None:
                    replacement_thread.join(2.0)
                for process in (contender, owner):
                    if process.is_alive():
                        process.terminate()
                    process.join(5.0)
                for results in (owner_results, contender_results):
                    results.close()
                    results.join_thread()

            self.assertFalse(replacement_thread.is_alive())
            self.assertFalse(socket_path.exists())
            self.assertFalse((runtime / "daemon.pid").exists())
            self.assertTrue((runtime / "daemon.lock").exists())

    def test_stale_socket_and_pidfile_are_replaced_without_signalling_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            runtime.mkdir()
            socket_path = runtime / "daemon.sock"
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(socket_path))
            stale.close()
            (runtime / "daemon.pid").write_text(
                "{}\n".format(os.getpid()),
                encoding="ascii",
            )

            stop_event = threading.Event()
            daemon = SidecarDaemon(
                scanner=FakeScanner(),
                runtime_dir=runtime,
                active_interval=0.02,
                idle_interval=0.02,
                max_idle_interval=0.03,
            )
            thread = daemon.start_in_thread(stop_event)
            self.assertTrue(daemon.wait_until_ready(2.0))
            self.assertTrue(SidecarClient(runtime_dir=runtime).ping()["ok"])

            duplicate = SidecarDaemon(scanner=FakeScanner(), runtime_dir=runtime)
            with self.assertRaises(DaemonAlreadyRunning):
                duplicate.serve_forever()
            self.assertTrue((runtime / "daemon.pid").exists())
            self.assertTrue(SidecarClient(runtime_dir=runtime).ping()["ok"])

            stop_event.set()
            daemon.stop()
            thread.join(2.0)

            self.assertFalse(thread.is_alive())
            self.assertFalse(socket_path.exists())
            self.assertFalse((runtime / "daemon.pid").exists())


if __name__ == "__main__":
    unittest.main()
