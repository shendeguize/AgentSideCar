import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sidecar.cursor_chat import CursorChatSourceError
from sidecar.model import Event, Session
from sidecar.tail import JSONLFollower, SessionTailer, watch_sessions


class PagedDSHAdapter:
    name = "dsh"

    def __init__(self, records, respects_after_seq=True):
        self.records = list(records)
        self.respects_after_seq = respects_after_seq
        self.after_seqs = []

    def replay(self, session, after_seq=None, max_records=256):
        del session
        self.after_seqs.append(after_seq)
        return [
            record
            for record in self.records
            if (
                not self.respects_after_seq
                or after_seq is None
                or record["seq"] > after_seq
            )
        ][:max_records]

    def normalize(self, record, session):
        text = record.get("text")
        if not isinstance(text, str):
            return []
        return [
            Event(
                "2026-08-23T04:00:00+08:00",
                session.agent,
                session.session_id,
                "assistant",
                text,
            )
        ]


def make_dsh_session(seq, transcript="/tmp/session.jsonl.zstd"):
    return Session(
        agent="dsh",
        session_id="dsh-session",
        project="/tmp/project",
        transcript=str(transcript),
        updated_at=1_787_429_064.0,
        extra={"seq": seq},
    )


def make_cursor_session(transcript):
    return Session(
        agent="cursor-cli",
        session_id="cursor-session",
        project="/tmp/project",
        transcript=str(transcript),
        updated_at=1_787_429_064.0,
        extra={"transcript_kind": "cursor-chat-sqlite"},
    )


class CursorAdapterStub:
    name = "cursor"

    def normalize(self, record, session):
        text = record.get("content")
        if not isinstance(text, str):
            return []
        return [
            Event(
                "2026-08-23T04:00:00+08:00",
                session.agent,
                session.session_id,
                str(record.get("role") or "message"),
                text,
            )
        ]


class FakeCursorChatFollower:
    def __init__(self, responses=(), error=None):
        self.responses = list(responses)
        self.last_error = error
        self.poll_calls = 0
        self.restore_calls = []
        self.has_pending_records = bool(self.responses)

    def poll(self):
        self.poll_calls += 1
        records = self.responses.pop(0) if self.responses else []
        self.has_pending_records = bool(self.responses)
        return records

    def export_checkpoint(self):
        return {"version": 1, "kind": "fake-cursor"}

    def restore_checkpoint(self, checkpoint):
        self.restore_calls.append(checkpoint)
        return checkpoint.get("kind") == "fake-cursor"


def _varint(value):
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _install_cursor_store(path, messages):
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, data BLOB)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        message_ids = []
        for message in messages:
            payload = json.dumps(
                message,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            message_id = hashlib.sha256(payload).hexdigest()
            connection.execute(
                "INSERT OR IGNORE INTO blobs (id, data) VALUES (?, ?)",
                (message_id, payload),
            )
            message_ids.append(message_id)
        root = b"".join(
            _varint((1 << 3) | 2)
            + _varint(32)
            + bytes.fromhex(message_id)
            for message_id in message_ids
        )
        root_id = hashlib.sha256(root).hexdigest()
        connection.execute(
            "INSERT OR IGNORE INTO blobs (id, data) VALUES (?, ?)",
            (root_id, root),
        )
        metadata = {
            "agentId": "production-shaped",
            "latestRootBlobId": root_id,
            "name": "Cursor test",
            "mode": "agent",
            "createdAt": 1_700_000_000_000,
        }
        connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (
                "0",
                json.dumps(metadata, separators=(",", ":")).encode("utf-8").hex(),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _source_signature(path):
    details = path.stat()
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


class JSONLFollowerBoundaryTests(unittest.TestCase):
    def test_starting_at_eof_preserves_split_json_without_replaying_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            complete = b'{"value":"history"}\n'
            split_record = b'{"value":"split event"}\n'
            split_at = 16
            path.write_bytes(complete + split_record[:split_at])

            follower = JSONLFollower(path)
            self.assertEqual([], follower.poll())

            with path.open("ab") as stream:
                stream.write(split_record[split_at:])
                stream.flush()

            self.assertEqual([{"value": "split event"}], follower.poll())
            self.assertEqual([], follower.poll())

    def test_checkpoint_resumes_complete_record_appended_while_inactive(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_bytes(b'{"value":"history"}\n')
            checkpoint = JSONLFollower(path).export_checkpoint()

            with path.open("ab") as stream:
                stream.write(b'{"value":"while inactive"}\n')

            resumed = JSONLFollower(path)
            self.assertTrue(resumed.restore_checkpoint(checkpoint))
            self.assertEqual([{"value": "while inactive"}], resumed.poll())
            self.assertEqual([], resumed.poll())

    def test_checkpoint_preserves_partial_record_at_saved_offset(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_bytes(b'{"value":"history"}\n{"value":"split')
            checkpoint = JSONLFollower(path).export_checkpoint()

            with path.open("ab") as stream:
                stream.write(b' checkpoint"}\n')

            resumed = JSONLFollower(path)
            self.assertTrue(resumed.restore_checkpoint(checkpoint))
            self.assertEqual([{"value": "split checkpoint"}], resumed.poll())

    def test_checkpoint_detects_truncate_and_regrow_before_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_bytes(b'{"value":"old"}\n')
            checkpoint = JSONLFollower(path).export_checkpoint()
            replacement = {
                "value": "replacement after truncate regrew beyond saved offset"
            }
            path.write_text(
                '{"value":"replacement after truncate regrew beyond saved offset"}\n',
                encoding="utf-8",
            )

            resumed = JSONLFollower(path)
            self.assertTrue(resumed.restore_checkpoint(checkpoint))
            self.assertEqual([replacement], resumed.poll())

    def test_checkpoint_detects_rotation_before_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "events.jsonl"
            path.write_bytes(b'{"value":"old"}\n')
            checkpoint = JSONLFollower(path).export_checkpoint()
            rotated = root / "replacement.jsonl"
            rotated.write_bytes(b'{"value":"after rotation"}\n')
            rotated.replace(path)

            resumed = JSONLFollower(path)
            self.assertTrue(resumed.restore_checkpoint(checkpoint))
            self.assertEqual([{"value": "after rotation"}], resumed.poll())


class DSHFollowerBoundaryTests(unittest.TestCase):
    def test_full_ignored_page_remains_pending_after_sequence_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl.zstd"
            path.write_bytes(b"compressed frames")
            adapter = PagedDSHAdapter(
                [
                    {"seq": 1, "type": "ignored"},
                    {"seq": 2, "type": "ignored"},
                    {"seq": 3, "type": "assistant", "text": "next page"},
                ]
            )
            tailer = SessionTailer(
                make_dsh_session(0, path),
                adapter=adapter,
                max_records=2,
            )

            self.assertEqual([], tailer.poll())
            self.assertTrue(tailer.has_pending_records)

            events = tailer.poll()

            self.assertEqual(["next page"], [event.text for event in events])
            self.assertFalse(tailer.has_pending_records)
            self.assertEqual([0, 2], adapter.after_seqs)
            self.assertEqual([], tailer.poll())
            self.assertEqual([0, 2], adapter.after_seqs)

    def test_full_page_without_sequence_progress_is_not_pending(self):
        adapter = PagedDSHAdapter(
            [
                {"seq": 1, "type": "ignored"},
                {"seq": 1, "type": "ignored"},
            ],
            respects_after_seq=False,
        )
        tailer = SessionTailer(
            make_dsh_session(1),
            adapter=adapter,
            max_records=2,
        )

        self.assertEqual([], tailer.poll())
        self.assertFalse(tailer.has_pending_records)

    def test_checkpoint_resumes_from_last_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl.zstd"
            path.write_bytes(b"initial frame")
            adapter = PagedDSHAdapter(
                [
                    {"seq": 1, "type": "assistant", "text": "history"},
                ]
            )
            original = SessionTailer(
                make_dsh_session(0, path),
                adapter=adapter,
            )
            self.assertEqual(["history"], [event.text for event in original.poll()])
            checkpoint = original.export_checkpoint()

            with path.open("ab") as stream:
                stream.write(b" appended frame")
            adapter.records.append(
                {"seq": 2, "type": "assistant", "text": "while inactive"}
            )
            resumed = SessionTailer(make_dsh_session(2, path), adapter=adapter)

            self.assertTrue(resumed.restore_checkpoint(checkpoint))
            self.assertEqual(
                ["while inactive"],
                [event.text for event in resumed.poll()],
            )
            self.assertEqual([0, 1], adapter.after_seqs)
            self.assertEqual([], resumed.poll())
            self.assertEqual([0, 1], adapter.after_seqs)

    def test_unchanged_signature_skips_replay_until_append(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.jsonl.zstd"
            path.write_bytes(b"initial frame")
            adapter = PagedDSHAdapter(
                [{"seq": 1, "type": "assistant", "text": "first"}]
            )
            tailer = SessionTailer(make_dsh_session(0, path), adapter=adapter)

            self.assertEqual(["first"], [event.text for event in tailer.poll()])
            self.assertEqual([], tailer.poll())
            self.assertEqual([0], adapter.after_seqs)

            with path.open("ab") as stream:
                stream.write(b" appended frame")
            adapter.records.append(
                {"seq": 2, "type": "assistant", "text": "appended"}
            )

            self.assertEqual(["appended"], [event.text for event in tailer.poll()])
            self.assertEqual([0, 1], adapter.after_seqs)

    def test_replacement_and_truncation_each_trigger_one_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "session.jsonl.zstd"
            path.write_bytes(b"initial frame")
            adapter = PagedDSHAdapter(
                [{"seq": 1, "type": "assistant", "text": "initial"}]
            )
            tailer = SessionTailer(make_dsh_session(0, path), adapter=adapter)
            self.assertEqual(["initial"], [event.text for event in tailer.poll()])

            replacement = root / "replacement.zstd"
            replacement.write_bytes(b"replacement frame")
            replacement.replace(path)
            adapter.records.append(
                {"seq": 2, "type": "assistant", "text": "replacement"}
            )
            self.assertEqual(
                ["replacement"],
                [event.text for event in tailer.poll()],
            )
            self.assertEqual([], tailer.poll())

            path.write_bytes(b"")
            adapter.records.append(
                {"seq": 3, "type": "assistant", "text": "truncated"}
            )
            self.assertEqual(["truncated"], [event.text for event in tailer.poll()])
            self.assertEqual([], tailer.poll())
            self.assertEqual([0, 1, 2], adapter.after_seqs)


class CursorChatSessionTailerTests(unittest.TestCase):
    def test_selects_cursor_follower_and_baselines_without_jsonl(self):
        follower = FakeCursorChatFollower()
        with mock.patch(
            "sidecar.tail.CursorChatFollower",
            return_value=follower,
        ) as cursor_follower, mock.patch(
            "sidecar.tail.JSONLFollower"
        ) as jsonl_follower:
            tailer = SessionTailer(
                make_cursor_session("/tmp/store.db"),
                adapter=CursorAdapterStub(),
            )

        cursor_follower.assert_called_once()
        jsonl_follower.assert_not_called()
        self.assertIs(follower, tailer.follower)
        self.assertEqual(1, follower.poll_calls)
        self.assertTrue(tailer.single_poll_per_refresh)

    def test_from_start_pages_and_normalizes_through_adapter(self):
        follower = FakeCursorChatFollower(
            responses=[
                [{"role": "user", "content": "historical request"}],
                [{"role": "assistant", "content": "historical answer"}],
            ]
        )
        with mock.patch(
            "sidecar.tail.CursorChatFollower",
            return_value=follower,
        ):
            tailer = SessionTailer(
                make_cursor_session("/tmp/store.db"),
                adapter=CursorAdapterStub(),
                from_start=True,
                max_records=1,
            )

        self.assertEqual(0, follower.poll_calls)
        self.assertEqual(
            ["historical request"],
            [event.text for event in tailer.poll()],
        )
        self.assertTrue(tailer.has_pending_records)
        self.assertEqual(
            ["historical answer"],
            [event.text for event in tailer.poll()],
        )
        self.assertFalse(tailer.has_pending_records)

    def test_cursor_checkpoint_is_strict_and_delegated(self):
        follower = FakeCursorChatFollower()
        with mock.patch(
            "sidecar.tail.CursorChatFollower",
            return_value=follower,
        ):
            original = SessionTailer(
                make_cursor_session("/tmp/store.db"),
                adapter=CursorAdapterStub(),
                from_start=True,
            )
            checkpoint = original.export_checkpoint()
            resumed = SessionTailer(
                make_cursor_session("/tmp/store.db"),
                adapter=CursorAdapterStub(),
                from_start=True,
            )

        self.assertEqual("cursor_chat_session", checkpoint["kind"])
        wrong_kind = dict(checkpoint, kind="jsonl_session")
        wrong_path = dict(checkpoint, path="/tmp/other.db")
        extra_key = dict(checkpoint, unexpected=True)
        self.assertFalse(resumed.restore_checkpoint(wrong_kind))
        self.assertFalse(resumed.restore_checkpoint(wrong_path))
        self.assertFalse(resumed.restore_checkpoint(extra_key))
        self.assertEqual([], follower.restore_calls)
        self.assertTrue(resumed.restore_checkpoint(checkpoint))
        self.assertEqual([checkpoint["follower"]], follower.restore_calls)

    def test_typed_cursor_errors_are_bounded_class_only_and_deduplicated(self):
        follower = FakeCursorChatFollower(
            error=CursorChatSourceError("private source and user content")
        )
        with mock.patch(
            "sidecar.tail.CursorChatFollower",
            return_value=follower,
        ):
            tailer = SessionTailer(
                make_cursor_session("/tmp/store.db"),
                adapter=CursorAdapterStub(),
            )

        for _ in range(32):
            self.assertEqual([], tailer.poll())

        self.assertEqual(["CursorChatSourceError"], tailer.errors)
        self.assertNotIn("private", " ".join(tailer.errors))

    def test_production_store_baseline_append_normalizes_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "store.db"
            user = {
                "role": "user",
                "content": "<user_query>Watch this chat</user_query>",
            }
            assistant = {"role": "assistant", "content": "fresh answer"}
            _install_cursor_store(path, [user])
            initial_signature = _source_signature(path)

            tailer = SessionTailer(make_cursor_session(path))

            self.assertEqual(initial_signature, _source_signature(path))
            _install_cursor_store(path, [user, assistant])
            appended_signature = _source_signature(path)
            events = tailer.poll()

            self.assertEqual(["fresh answer"], [event.text for event in events])
            self.assertEqual(["assistant"], [event.kind for event in events])
            self.assertEqual(appended_signature, _source_signature(path))


class WatchSessionsCancellationTests(unittest.TestCase):
    def test_pre_cancelled_watch_does_not_construct_tailers(self):
        cancel = threading.Event()
        cancel.set()
        ready = []
        with mock.patch("sidecar.tail.SessionTailer") as tailer:
            self.assertEqual(
                [],
                list(
                    watch_sessions(
                        [make_dsh_session(0)],
                        cancel_event=cancel,
                        on_ready=lambda: ready.append(True),
                    )
                ),
            )
        tailer.assert_not_called()
        self.assertEqual([], ready)

    def test_cancel_event_interrupts_long_poll_wait_without_busy_loop(self):
        cancel = threading.Event()
        poll_calls = []

        class IdleTailer:
            def __init__(self, session, from_start=False):
                del session, from_start

            def poll(self):
                poll_calls.append(time.monotonic())
                return []

        finished = threading.Event()

        def consume():
            with mock.patch("sidecar.tail.SessionTailer", IdleTailer):
                list(
                    watch_sessions(
                        [make_dsh_session(0)],
                        poll_interval=30.0,
                        cancel_event=cancel,
                    )
                )
            finished.set()

        worker = threading.Thread(target=consume)
        worker.start()
        deadline = time.monotonic() + 1.0
        while not poll_calls and time.monotonic() < deadline:
            time.sleep(0.005)
        started = time.monotonic()
        cancel.set()
        worker.join(timeout=1.0)

        self.assertFalse(worker.is_alive())
        self.assertTrue(finished.is_set())
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(1, len(poll_calls))

    def test_ready_follows_all_tailer_construction_and_precedes_poll(self):
        cancel = threading.Event()
        calls = []

        class OrderedTailer:
            def __init__(self, session, from_start=False):
                del from_start
                self.session_id = session.session_id
                calls.append(("init", self.session_id))

            def poll(self):
                calls.append(("poll", self.session_id))
                cancel.set()
                return []

            def close(self):
                calls.append(("close", self.session_id))

        sessions = [make_dsh_session(0), make_dsh_session(1)]
        sessions[1].session_id = "dsh-second"
        with mock.patch("sidecar.tail.SessionTailer", OrderedTailer):
            self.assertEqual(
                [],
                list(
                    watch_sessions(
                        sessions,
                        cancel_event=cancel,
                        on_ready=lambda: calls.append(("ready", None)),
                    )
                ),
            )

        self.assertEqual(
            [
                ("init", "dsh-session"),
                ("init", "dsh-second"),
                ("ready", None),
                ("poll", "dsh-session"),
                ("close", "dsh-second"),
                ("close", "dsh-session"),
            ],
            calls,
        )

    def test_empty_or_failed_initialization_never_signals_ready(self):
        ready = []
        self.assertEqual(
            [],
            list(watch_sessions([], on_ready=lambda: ready.append(True))),
        )

        constructed = []

        class FailingTailer:
            def __init__(self, session, from_start=False):
                del from_start
                if constructed:
                    raise RuntimeError("initialization failed")
                self.session = session
                constructed.append(self)

            def close(self):
                self.closed = True

        sessions = [make_dsh_session(0), make_dsh_session(1)]
        with mock.patch("sidecar.tail.SessionTailer", FailingTailer):
            with self.assertRaises(RuntimeError):
                list(
                    watch_sessions(
                        sessions,
                        on_ready=lambda: ready.append(True),
                    )
                )

        self.assertEqual([], ready)
        self.assertTrue(constructed[0].closed)

    def test_ready_callback_exception_closes_initialized_tailers(self):
        tailers = []

        class ClosableTailer:
            def __init__(self, session, from_start=False):
                del session, from_start
                self.closed = False
                tailers.append(self)

            def close(self):
                self.closed = True

        callback_error = RuntimeError("ready callback failed")

        def fail_ready():
            raise callback_error

        with mock.patch("sidecar.tail.SessionTailer", ClosableTailer):
            with self.assertRaises(RuntimeError) as raised:
                list(
                    watch_sessions(
                        [make_dsh_session(0)],
                        on_ready=fail_ready,
                    )
                )

        self.assertIs(callback_error, raised.exception)
        self.assertTrue(tailers[0].closed)

    def test_jsonl_follower_validation_and_malformed_lines_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for arguments in (
                {"max_read_bytes": 0},
                {"max_line_bytes": 0},
                {"max_records": 0},
            ):
                with self.subTest(arguments=arguments):
                    with self.assertRaises(ValueError):
                        JSONLFollower(root / "missing", **arguments)

            missing = JSONLFollower(
                root / "missing",
                from_start=True,
                max_read_bytes=32,
                max_line_bytes=8,
                max_records=2,
            )
            self.assertTrue(missing.export_checkpoint()["missing_at_start"])
            self.assertEqual([], missing.poll())

            directory = JSONLFollower(root, from_start=True)
            self.assertTrue(directory.export_checkpoint()["missing_at_start"])

            path = root / "events.jsonl"
            path.write_bytes(b"")
            follower = JSONLFollower(
                path,
                from_start=True,
                max_read_bytes=32,
                max_line_bytes=8,
                max_records=2,
            )
            for raw in (b"", b" " * 9, b"not-json", b"[]"):
                follower._consume_line(raw)
            self.assertFalse(follower.has_pending_records)

            follower._consume_chunk(b"x" * 9)
            self.assertTrue(follower.export_checkpoint()["dropping_line"])
            follower._consume_chunk(b"discarded\n{\"a\":1}\n")
            self.assertFalse(follower.export_checkpoint()["dropping_line"])
            self.assertEqual([{"a": 1}], follower.poll())

    def test_jsonl_checkpoint_rejects_each_untrusted_field_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text('{"event":1}\n', encoding="utf-8")
            follower = JSONLFollower(path, from_start=True, max_line_bytes=32)
            original = follower.export_checkpoint()
            invalid_updates = (
                {"version": 2},
                {"kind": "other"},
                {"path": "/different"},
                {"offset": True},
                {"offset": -1},
                {"anchor": "text"},
                {"pending": "text"},
                {"dropping_line": 1},
                {"records": "records"},
                {"missing_at_start": 0},
                {"identity": (1,)},
                {"identity": (1, True)},
                {"records": ("not-a-record",)},
            )
            for update in invalid_updates:
                with self.subTest(update=update):
                    checkpoint = dict(original)
                    checkpoint.update(update)
                    self.assertFalse(follower.restore_checkpoint(checkpoint))
                    self.assertEqual(original, follower.export_checkpoint())

    def test_session_tailer_rejects_untrusted_dsh_checkpoints_and_replay_errors(self):
        class FailingReplayAdapter(PagedDSHAdapter):
            def replay(self, session, after_seq=None, max_records=256):
                del session, after_seq, max_records
                raise RuntimeError("bounded replay failed")

        session = make_dsh_session(2)
        tailer = SessionTailer(session, adapter=PagedDSHAdapter([]))
        valid = tailer.export_checkpoint()
        invalid_updates = (
            {"kind": "jsonl"},
            {"last_seq": True},
            {"initialized": 1},
            {"page_pending": 1},
            {"signature": (1, 2)},
            {"signature": (1, 2, 3, 4, 5, True)},
            {"force_replay": 1},
        )
        for update in invalid_updates:
            with self.subTest(update=update):
                self.assertFalse(tailer.restore_checkpoint({**valid, **update}))

        failing = SessionTailer(session, adapter=FailingReplayAdapter([]))
        self.assertEqual([], failing.poll())
        self.assertIn("RuntimeError: bounded replay failed", failing.errors)
        self.assertTrue(failing.export_checkpoint()["force_replay"])

        class FailingNormalizeAdapter(PagedDSHAdapter):
            def normalize(self, record, session):
                del record, session
                raise ValueError("normalize failed")

        normalizer = SessionTailer(
            session,
            adapter=FailingNormalizeAdapter([{"seq": 3, "text": "event"}]),
        )
        self.assertEqual([], normalizer.poll())
        self.assertIn("ValueError: normalize failed", normalizer.errors)

        with self.assertRaises(ValueError):
            list(watch_sessions([], poll_interval=0))

    def test_jsonl_file_read_races_and_oversized_partial_rows_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "oversized.jsonl"
            path.write_bytes(b"x" * 20)
            follower = JSONLFollower(
                path,
                from_start=False,
                max_line_bytes=8,
            )
            self.assertTrue(follower.export_checkpoint()["dropping_line"])
            self.assertEqual(b"", follower._read_anchor(0))

            with mock.patch(
                "pathlib.Path.open",
                side_effect=OSError("open failed"),
            ):
                self.assertEqual(b"", follower._read_anchor(1))
                follower._capture_trailing_partial(1)
                self.assertEqual([], follower.poll())

            directory = JSONLFollower(root, from_start=True)
            self.assertEqual([], directory.poll())

    def test_tailer_missing_follower_and_watch_cleanup_errors_are_bounded(self):
        session = make_cursor_session("/tmp/missing.db")
        follower = FakeCursorChatFollower()
        with mock.patch("sidecar.tail.CursorChatFollower", return_value=follower):
            tailer = SessionTailer(session, adapter=CursorAdapterStub(), from_start=True)
        tailer.follower = None
        self.assertEqual([], tailer.poll())

        created = []

        class FailingCloser:
            def __init__(self, session, from_start=False):
                del session, from_start
                created.append(self)

            def poll(self):
                return []

            def close(self):
                raise OSError("close failed")

        with mock.patch("sidecar.tail.SessionTailer", FailingCloser), mock.patch(
            "sidecar.tail.time.sleep",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                list(watch_sessions([make_dsh_session(0)], poll_interval=0.01))
        self.assertEqual(1, len(created))


if __name__ == "__main__":
    unittest.main()
