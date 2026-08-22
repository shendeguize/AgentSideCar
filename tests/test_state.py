import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sidecar.state as state_module
import sidecar.adapters.base as base_module
from sidecar.adapters.base import Adapter, local_timestamp, timestamp_epoch
from sidecar.model import Session, Status
from sidecar.state import (
    StateEngine,
    cursor_terminal_active,
    parse_terminal_metadata,
    terminal_metadata_is_active,
)


class HintAdapter(Adapter):
    name = "hint"
    agent_names = ("hint",)

    def __init__(self, status):
        self.status = status
        self.calls = 0

    def discover(self, home):
        return []

    def normalize(self, record, session):
        return []

    def infer_status(self, session, now=None):
        self.calls += 1
        return self.status


def force_new_signature(path, previous_mtime_ns):
    current = path.stat().st_mtime_ns
    changed = max(current, previous_mtime_ns + 1_000_000)
    os.utime(path, ns=(changed, changed))


class TimestampParsingTests(unittest.TestCase):
    def test_numeric_seconds_milliseconds_and_strings(self):
        self.assertEqual(1_787_429_063.0, timestamp_epoch(1_787_429_063))
        self.assertEqual(1_787_429_063.221, timestamp_epoch(1_787_429_063_221))
        self.assertEqual(1_787_429_063.221, timestamp_epoch("1787429063221"))

    def test_iso_and_datetime_values_use_utc_for_naive_inputs(self):
        expected = dt.datetime(
            2026,
            8,
            23,
            7,
            28,
            tzinfo=dt.timezone.utc,
        ).timestamp()

        self.assertEqual(expected, timestamp_epoch("2026-08-23T07:28:00Z"))
        self.assertEqual(expected, timestamp_epoch("2026-08-23T15:28:00+08:00"))
        self.assertEqual(expected, timestamp_epoch("2026-08-23T07:28:00"))
        self.assertEqual(
            expected,
            timestamp_epoch(dt.datetime(2026, 8, 23, 7, 28)),
        )

    def test_invalid_boolean_and_overflow_values_are_rejected(self):
        self.assertIsNone(timestamp_epoch(True))
        self.assertIsNone(timestamp_epoch("not-a-timestamp"))
        self.assertIsNone(timestamp_epoch(float("inf")))
        self.assertIsNone(timestamp_epoch(10**400))

    def test_invalid_and_unrenderable_values_use_local_fallback(self):
        fallback = 42.0
        expected = (
            dt.datetime.fromtimestamp(fallback, tz=dt.timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds")
        )

        self.assertEqual(expected, local_timestamp("invalid", fallback=fallback))
        self.assertEqual(expected, local_timestamp(1e300, fallback=fallback))

    def test_state_activity_uses_canonical_parser(self):
        with mock.patch.object(
            state_module,
            "timestamp_epoch",
            return_value=123.0,
        ) as parser:
            self.assertEqual(
                123.0,
                state_module._record_timestamp({"timestamp": "native-value"}),
            )

        parser.assert_called_once_with("native-value")

    def test_timestamp_helper_is_exported_without_dead_alias(self):
        self.assertIn("timestamp_epoch", base_module.__all__)
        self.assertNotIn("timestamp_to_local", base_module.__all__)
        self.assertFalse(hasattr(base_module, "timestamp_to_local"))


class StateEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = 1_000.0
        self.engine = StateEngine(
            fresh_seconds=20,
            idle_seconds=120,
            terminal_fresh_seconds=10,
            home=self.root,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def write_transcript(self, name, records):
        path = self.root / name
        with path.open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")
        return path

    def session(self, path, updated_at=None, agent="claude"):
        return Session(
            agent=agent,
            session_id=path.stem,
            project=str(self.root),
            transcript=str(path),
            updated_at=self.now - 1 if updated_at is None else updated_at,
        )

    def cursor_session_with_active_terminal(self, updated_at):
        project_root = self.root / ".cursor" / "projects" / "project"
        transcript_dir = project_root / "agent-transcripts"
        terminal_dir = project_root / "terminals"
        transcript_dir.mkdir(parents=True)
        terminal_dir.mkdir()
        transcript = transcript_dir / "cursor.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "role": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Done."}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        terminal = terminal_dir / "1.txt"
        terminal.write_text(
            "---\npid: 123\nrunning: true\ncurrent_command: python worker.py\n---\n",
            encoding="utf-8",
        )
        os.utime(terminal, (self.now, self.now))
        return self.session(
            transcript,
            updated_at=updated_at,
            agent="cursor-ide",
        )

    def test_unmatched_tool_use_is_working_when_fresh(self):
        transcript = self.write_transcript(
            "working.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call-1",
                                "name": "Shell",
                                "input": {},
                            }
                        ],
                    },
                }
            ],
        )

        status = self.engine.infer_status(self.session(transcript), now=self.now)

        self.assertEqual(Status.WORKING, status)

    def test_completed_assistant_text_is_waiting(self):
        transcript = self.write_transcript(
            "waiting.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Done."}],
                    },
                }
            ],
        )

        status = self.engine.infer_status(self.session(transcript), now=self.now)

        self.assertEqual(Status.WAITING, status)

    def test_stale_activity_is_idle(self):
        transcript = self.write_transcript(
            "idle.jsonl",
            [{"type": "user", "message": {"role": "user", "content": "old prompt"}}],
        )

        status = self.engine.infer_status(
            self.session(transcript, updated_at=self.now - 121),
            now=self.now,
        )

        self.assertEqual(Status.IDLE, status)

    def test_unchanged_tail_is_reused_while_status_ages_to_idle(self):
        transcript = self.write_transcript(
            "aging.jsonl",
            [{"type": "user", "message": {"role": "user", "content": "work"}}],
        )
        session = self.session(transcript)

        with mock.patch.object(
            state_module,
            "read_jsonl_tail",
            wraps=state_module.read_jsonl_tail,
        ) as tail_reader:
            self.assertEqual(
                Status.WORKING,
                self.engine.infer_status(session, now=self.now),
            )
            self.assertEqual(
                Status.IDLE,
                self.engine.infer_status(session, now=self.now + 121),
            )

        self.assertEqual(1, tail_reader.call_count)

    def test_append_truncate_and_replacement_invalidate_cached_tail(self):
        transcript = self.write_transcript(
            "changing.jsonl",
            [{"type": "user", "message": {"role": "user", "content": "work"}}],
        )
        session = self.session(transcript)

        with mock.patch.object(
            state_module,
            "read_jsonl_tail",
            wraps=state_module.read_jsonl_tail,
        ) as tail_reader:
            self.assertEqual(
                Status.WORKING,
                self.engine.infer_status(session, now=self.now),
            )

            previous = transcript.stat().st_mtime_ns
            with transcript.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        {
                            "type": "assistant",
                            "message": {
                                "role": "assistant",
                                "content": "Done.",
                            },
                        }
                    )
                    + "\n"
                )
            force_new_signature(transcript, previous)
            self.assertEqual(
                Status.WAITING,
                self.engine.infer_status(session, now=self.now),
            )

            previous = transcript.stat().st_mtime_ns
            transcript.write_text("", encoding="utf-8")
            force_new_signature(transcript, previous)
            self.assertEqual(
                Status.WAITING,
                self.engine.infer_status(session, now=self.now),
            )

            previous = transcript.stat().st_mtime_ns
            replacement = self.write_transcript(
                "replacement.jsonl",
                [{"type": "user", "message": {"role": "user", "content": "again"}}],
            )
            replacement.replace(transcript)
            force_new_signature(transcript, previous)
            self.assertEqual(
                Status.WORKING,
                self.engine.infer_status(session, now=self.now),
            )

        self.assertEqual(4, tail_reader.call_count)

    def test_missing_required_transcript_is_dead(self):
        missing = self.root / "missing.jsonl"

        status = self.engine.infer_status(self.session(missing), now=self.now)

        self.assertEqual(Status.DEAD, status)

    def test_deleted_transcript_is_dead_and_cached_tail_cannot_revive_it(self):
        transcript = self.write_transcript(
            "deleted.jsonl",
            [{"type": "user", "message": {"role": "user", "content": "work"}}],
        )
        session = self.session(transcript)

        with mock.patch.object(
            state_module,
            "read_jsonl_tail",
            wraps=state_module.read_jsonl_tail,
        ) as tail_reader:
            self.assertEqual(
                Status.WORKING,
                self.engine.infer_status(session, now=self.now),
            )
            transcript.unlink()
            self.assertEqual(
                Status.DEAD,
                self.engine.infer_status(session, now=self.now),
            )

        self.assertEqual(1, tail_reader.call_count)

    def test_adapter_override_is_re_evaluated_before_cached_fallback(self):
        transcript = self.write_transcript(
            "adapter-owned.jsonl",
            [{"type": "user", "message": {"role": "user", "content": "work"}}],
        )
        session = self.session(transcript, agent="custom")
        adapter = HintAdapter(Status.WORKING)

        with mock.patch.object(
            state_module,
            "read_jsonl_tail",
            wraps=state_module.read_jsonl_tail,
        ) as tail_reader:
            self.assertEqual(
                Status.WORKING,
                self.engine.infer_status(
                    session,
                    adapter=adapter,
                    now=self.now,
                ),
            )
            adapter.status = Status.IDLE
            self.assertEqual(
                Status.IDLE,
                self.engine.infer_status(
                    session,
                    adapter=adapter,
                    now=self.now,
                ),
            )

        self.assertEqual(2, adapter.calls)
        self.assertEqual(0, tail_reader.call_count)

    def test_cursor_terminal_evidence_is_rechecked_with_cached_tail(self):
        transcript = self.write_transcript(
            "cursor-terminal.jsonl",
            [
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "Done."},
                }
            ],
        )
        session = self.session(transcript, agent="cursor-ide")

        with mock.patch.object(
            state_module,
            "read_jsonl_tail",
            wraps=state_module.read_jsonl_tail,
        ) as tail_reader, mock.patch.object(
            state_module,
            "cursor_terminal_active",
            side_effect=(True, False),
        ) as terminal_active:
            self.assertEqual(
                Status.WORKING,
                self.engine.infer_status(session, now=self.now),
            )
            self.assertEqual(
                Status.WAITING,
                self.engine.infer_status(session, now=self.now),
            )

        self.assertEqual(1, tail_reader.call_count)
        self.assertEqual(2, terminal_active.call_count)

    def test_tail_cache_capacity_is_bounded(self):
        engine = StateEngine(
            fresh_seconds=20,
            idle_seconds=120,
            tail_cache_size=2,
            home=self.root,
        )
        for index in range(3):
            transcript = self.write_transcript(
                "{}.jsonl".format(index),
                [{"type": "user", "message": {"role": "user", "content": "work"}}],
            )
            engine.infer_status(self.session(transcript), now=self.now)

        self.assertEqual(2, len(engine._tail_cache))

    def test_fresh_active_cursor_terminal_promotes_to_working(self):
        project_root = self.root / ".cursor" / "projects" / "project"
        transcript_dir = project_root / "agent-transcripts"
        terminal_dir = project_root / "terminals"
        transcript_dir.mkdir(parents=True)
        terminal_dir.mkdir()
        transcript = transcript_dir / "cursor.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "role": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Done."}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        terminal = terminal_dir / "1.txt"
        terminal.write_text(
            "---\npid: 123\nrunning: true\ncurrent_command: python worker.py\n---\n",
            encoding="utf-8",
        )
        os.utime(terminal, (self.now, self.now))
        session = self.session(transcript, agent="cursor-ide")

        self.assertTrue(cursor_terminal_active(session, now=self.now))
        self.assertEqual(
            Status.WORKING,
            self.engine.infer_status(session, now=self.now),
        )

    def test_59_hour_cursor_session_stays_idle_with_fresh_active_terminal(self):
        session = self.cursor_session_with_active_terminal(
            updated_at=self.now - (59 * 60 * 60),
        )
        engine = StateEngine(terminal_fresh_seconds=10, home=self.root)

        self.assertTrue(cursor_terminal_active(session, now=self.now))
        self.assertEqual(Status.IDLE, engine.infer_status(session, now=self.now))

    def test_8_minute_cursor_session_can_use_fresh_active_terminal(self):
        session = self.cursor_session_with_active_terminal(
            updated_at=self.now - (8 * 60),
        )
        engine = StateEngine(terminal_fresh_seconds=10, home=self.root)

        self.assertEqual(Status.WORKING, engine.infer_status(session, now=self.now))

    def test_old_active_terminal_does_not_promote(self):
        project_root = self.root / ".cursor" / "projects" / "project"
        transcript_dir = project_root / "agent-transcripts"
        terminal_dir = project_root / "terminals"
        transcript_dir.mkdir(parents=True)
        terminal_dir.mkdir()
        transcript = self.write_transcript(
            "temporary.jsonl",
            [{"role": "assistant", "content": "Done."}],
        )
        cursor_transcript = transcript_dir / "cursor.jsonl"
        transcript.replace(cursor_transcript)
        terminal = terminal_dir / "1.txt"
        terminal.write_text("---\nrunning: true\n---\n", encoding="utf-8")
        os.utime(terminal, (self.now - 30, self.now - 30))
        session = self.session(cursor_transcript, agent="cursor-ide")

        self.assertFalse(cursor_terminal_active(session, now=self.now, fresh_seconds=10))
        self.assertEqual(
            Status.WAITING,
            self.engine.infer_status(session, now=self.now),
        )

    def test_finished_terminal_duration_is_not_active(self):
        metadata = parse_terminal_metadata(
            "---\n"
            "command: python worker.py\n"
            "status: succeeded\n"
            "running_for_ms: 1042440\n"
            "---\n"
        )

        self.assertFalse(terminal_metadata_is_active(metadata))


if __name__ == "__main__":
    unittest.main()
