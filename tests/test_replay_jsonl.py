"""Generic JSONL transcript replay.

Before this, only DSH sessions had a history source, so opening any other
agent's older session showed an empty timeline while the whole conversation
sat in a plain JSONL file. These tests pin the two properties that make the
generic replay safe to page against: the cursor is a stable line ordinal, and
every budget that ends a page early says so instead of pretending the
transcript ended.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidecar.adapters.claude import ClaudeAdapter
from sidecar.adapters.codex import CodexAdapter
from sidecar.adapters.cursor import CursorAdapter
from sidecar.adapters.kimi import KimiAdapter
from sidecar.adapters.replay import (
    ReplayPage,
    ReplayUnsupported,
    replay_jsonl_events,
)
from sidecar.model import Session


def write_lines(path: Path, lines) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def write_records(path: Path, records) -> Path:
    return write_lines(path, [json.dumps(record) for record in records])


class ReplayJsonlEventsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_every_record_carries_its_line_ordinal_as_the_cursor(self):
        path = write_records(
            self.root / "t.jsonl",
            [{"type": "user"}, {"type": "assistant"}, {"type": "user"}],
        )

        page = replay_jsonl_events(path)

        self.assertEqual([1, 2, 3], [record["seq"] for record in page])
        self.assertTrue(page.exhausted)

    def test_after_seq_resumes_exactly_where_the_previous_page_stopped(self):
        path = write_records(
            self.root / "t.jsonl",
            [{"n": index} for index in range(1, 6)],
        )

        first = replay_jsonl_events(path, max_records=2)
        self.assertEqual([1, 2], [record["seq"] for record in first])
        self.assertFalse(first.exhausted)

        second = replay_jsonl_events(path, after_seq=first[-1]["seq"], max_records=2)
        self.assertEqual([3, 4], [record["seq"] for record in second])

        rest = replay_jsonl_events(path, after_seq=second[-1]["seq"], max_records=2)
        self.assertEqual([5], [record["seq"] for record in rest])
        self.assertTrue(rest.exhausted)

    def test_ordinals_count_blank_and_unparseable_lines_so_they_stay_stable(self):
        # A cursor that renumbered when a malformed line was later fixed would
        # silently re-show or skip events; ordinals therefore count lines, not
        # successfully parsed records.
        path = write_lines(
            self.root / "t.jsonl",
            ['{"n": 1}', "", "not json", "[1, 2]", '{"n": 5}'],
        )

        page = replay_jsonl_events(path)

        self.assertEqual([1, 5], [record["seq"] for record in page])
        self.assertEqual([1, 5], [record["n"] for record in page])

    def test_a_line_without_its_newline_is_left_for_the_next_call(self):
        path = self.root / "t.jsonl"
        path.write_text('{"n": 1}\n{"n": 2}', encoding="utf-8")

        partial = replay_jsonl_events(path)
        self.assertEqual([1], [record["seq"] for record in partial])

        with path.open("a", encoding="utf-8") as stream:
            stream.write("\n")
        completed = replay_jsonl_events(path, after_seq=1)
        self.assertEqual([2], [record["seq"] for record in completed])

    def test_a_missing_transcript_is_an_exhausted_empty_page(self):
        page = replay_jsonl_events(self.root / "absent.jsonl")

        self.assertEqual([], list(page))
        self.assertTrue(page.exhausted)

    def test_an_oversized_line_is_skipped_without_losing_the_ordinal(self):
        path = write_lines(
            self.root / "t.jsonl",
            ['{"n": 1}', json.dumps({"pad": "x" * 4096}), '{"n": 3}'],
        )

        page = replay_jsonl_events(path, max_line_bytes=64)

        self.assertEqual([1, 3], [record["seq"] for record in page])

    def test_an_oversized_record_is_skipped_but_the_page_still_returns(self):
        # The retained-byte budget is about the answer's size, not the
        # transcript's: a record too big to fit ends the page rather than
        # discarding the records already gathered.
        path = write_lines(
            self.root / "t.jsonl",
            ['{"n": 1}', json.dumps({"pad": "x" * 2048}), '{"n": 3}'],
        )

        page = replay_jsonl_events(path, max_retained_bytes=512)

        self.assertEqual([1], [record["seq"] for record in page])
        self.assertFalse(page.exhausted)

    def test_an_oversized_first_record_does_not_end_the_page(self):
        path = write_lines(
            self.root / "t.jsonl",
            [json.dumps({"pad": "x" * 2048}), '{"n": 2}'],
        )

        page = replay_jsonl_events(path, max_retained_bytes=512)

        self.assertEqual([2], [record["seq"] for record in page])

    def test_the_time_budget_ends_the_page_without_claiming_the_end(self):
        path = write_records(self.root / "t.jsonl", [{"n": 1}, {"n": 2}])

        page = replay_jsonl_events(path, timeout=1e-9)

        self.assertEqual([], list(page))
        self.assertFalse(page.exhausted)

    def test_an_oversized_line_spanning_chunks_is_dropped_not_reassembled(self):
        # A line longer than the read chunk arrives in pieces. Buffering it
        # would defeat the line budget, so the pieces are dropped and the
        # ordinal resumes on the next complete line.
        path = self.root / "t.jsonl"
        path.write_text(
            json.dumps({"pad": "x" * (512 * 1024)}) + "\n" + '{"n": 2}\n',
            encoding="utf-8",
        )

        page = replay_jsonl_events(path, max_line_bytes=1024)

        self.assertEqual([2], [record["seq"] for record in page])
        self.assertTrue(page.exhausted)

    def test_an_oversized_trailing_line_is_dropped_rather_than_buffered(self):
        path = self.root / "t.jsonl"
        path.write_text('{"n": 1}\n' + "x" * 4096, encoding="utf-8")

        page = replay_jsonl_events(path, max_line_bytes=64)

        self.assertEqual([1], [record["seq"] for record in page])
        self.assertTrue(page.exhausted)

    def test_only_a_page_that_saw_the_end_of_file_reports_exhausted(self):
        # A page that exactly fills its record budget stops without ever
        # reading past the last record, so it cannot claim the transcript
        # ended — it says "maybe more" and costs one cheap empty page.
        path = write_records(self.root / "t.jsonl", [{"n": 1}, {"n": 2}])

        self.assertFalse(replay_jsonl_events(path, max_records=1).exhausted)
        self.assertFalse(replay_jsonl_events(path, max_records=2).exhausted)
        self.assertTrue(replay_jsonl_events(path, max_records=3).exhausted)

    def test_nonpositive_budgets_return_nothing_rather_than_scanning(self):
        path = write_records(self.root / "t.jsonl", [{"n": 1}])

        for kwargs in (
            {"max_records": 0},
            {"max_scan_bytes": 0},
            {"max_retained_bytes": 0},
            {"max_line_bytes": 0},
            {"timeout": 0},
        ):
            with self.subTest(**kwargs):
                self.assertEqual([], list(replay_jsonl_events(path, **kwargs)))


class AdapterReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def session(self, agent, transcript, **extra):
        return Session(
            agent=agent,
            session_id="s1",
            project="/work",
            transcript=str(transcript),
            updated_at=1_700_000_000.0,
            extra=extra,
        )

    def test_claude_replays_its_transcript_into_normalized_events(self):
        transcript = write_records(
            self.root / "claude.jsonl",
            [
                {
                    "type": "user",
                    "timestamp": "2023-11-14T22:13:20Z",
                    "message": {"content": "first question"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2023-11-14T22:14:20Z",
                    "message": {"content": [{"type": "text", "text": "an answer"}]},
                },
            ],
        )
        adapter = ClaudeAdapter()
        session = self.session("claude", transcript)

        page = adapter.replay(session)
        events = [
            event
            for record in page
            for event in adapter.normalize(record, session)
        ]

        self.assertEqual([1, 2], [record["seq"] for record in page])
        self.assertEqual(["first question", "an answer"], [e.text for e in events])

    def test_codex_replays_its_rollout(self):
        transcript = write_records(
            self.root / "rollout.jsonl",
            [
                {
                    "type": "event_msg",
                    "timestamp": "2023-11-14T22:13:20Z",
                    "payload": {"type": "agent_message", "message": "hello"},
                }
            ],
        )
        adapter = CodexAdapter()
        session = self.session("codex", transcript)

        page = adapter.replay(session)

        self.assertEqual([1], [record["seq"] for record in page])
        self.assertTrue(list(adapter.normalize(page[0], session)))

    def test_kimi_replays_its_wire_log(self):
        transcript = write_records(
            self.root / "wire.jsonl",
            [{"type": "turn.prompt", "time": 1_700_000_000, "input": "hi"}],
        )

        page = KimiAdapter().replay(self.session("kimi", transcript))

        self.assertEqual([1], [record["seq"] for record in page])

    def test_cursor_replays_ide_transcripts_but_refuses_the_sqlite_store(self):
        transcript = write_records(
            self.root / "cursor.jsonl",
            [{"role": "user", "content": "hi"}],
        )
        adapter = CursorAdapter()

        page = adapter.replay(self.session("cursor-ide", transcript))
        self.assertEqual([1], [record["seq"] for record in page])

        store = self.root / "store.db"
        store.write_bytes(b"SQLite format 3\x00")
        with self.assertRaises(ReplayUnsupported):
            adapter.replay(
                self.session(
                    "cursor-cli", store, transcript_kind="cursor-chat-sqlite"
                )
            )

    def test_a_session_without_a_transcript_replays_nothing(self):
        page = ClaudeAdapter().replay(self.session("claude", ""))

        self.assertIsInstance(page, ReplayPage)
        self.assertEqual([], list(page))

    def test_a_transcript_that_is_not_a_readable_file_replays_nothing(self):
        directory = self.root / "not-a-transcript"
        directory.mkdir()

        page = ClaudeAdapter().replay(self.session("claude", directory))

        self.assertEqual([], list(page))
        self.assertTrue(page.exhausted)

    def test_an_unstattable_transcript_replays_nothing(self):
        transcript = write_records(self.root / "claude.jsonl", [{"type": "user"}])
        session = self.session("claude", transcript)

        with mock.patch.object(Path, "is_file", side_effect=OSError("denied")):
            page = ClaudeAdapter().replay(session)

        self.assertEqual([], list(page))


if __name__ == "__main__":
    unittest.main()
