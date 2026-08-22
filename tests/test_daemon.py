import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sidecar.adapters.base import Adapter
from sidecar.client import SidecarClient
from sidecar.daemon import DaemonAlreadyRunning, SidecarDaemon
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

    def test_status_response_larger_than_64kb_roundtrips_as_jsonl(self):
        updated_at = time.time()
        sessions = [
            Session(
                agent="fake",
                session_id="bulk-{:03d}".format(index),
                project=str(self.root),
                transcript=str(self.root / "events.log"),
                updated_at=updated_at,
                title="large status payload " + ("x" * 320),
                status=Status.IDLE,
            )
            for index in range(233)
        ]
        expected = [session.to_dict() for session in sessions]
        response_frame = (
            json.dumps(
                {
                    "ok": True,
                    "op": "status",
                    "sessions": expected,
                    "scan_errors": [],
                },
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            + b"\n"
        )
        self.assertGreater(len(response_frame), 64 * 1024)
        self.assertLess(len(response_frame), 2 * 1024 * 1024)

        self.scanner.sessions = sessions
        self.daemon.scan_once()

        client = SidecarClient(runtime_dir=self.runtime, timeout=2.0)
        self.assertEqual(expected, client.status())

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
            acknowledgement = json.loads(stream.readline())
            self.assertEqual({"ok": True, "op": "subscribe"}, acknowledgement)

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


class StaleRuntimeTests(unittest.TestCase):
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
