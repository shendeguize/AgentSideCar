import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sidecar.adapters.base import (
    as_mapping,
    content_block_events,
    epoch_seconds,
    local_timestamp,
    read_json_object,
)
from sidecar.adapters.dsh import DSHAdapter, ReplayPage, replay_dsh_events
from sidecar.model import Session
from sidecar.tail import SessionTailer


OLD_REPLAY_BOUNDARY = 8 * 1024 * 1024
ZSTD = shutil.which("zstd")


def _write_record(stream, record):
    raw = json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
    stream.write(raw)
    return len(raw)


def _write_large_transcript(path):
    padding = "x" * (64 * 1024)
    seq = 0
    with path.open("wb") as stream:
        while stream.tell() <= OLD_REPLAY_BOUNDARY:
            seq += 1
            _write_record(
                stream,
                {
                    "seq": seq,
                    "type": "padding",
                    "data": {"text": padding},
                },
            )
        boundary_seq = seq
        for text in ("tail one", "tail two"):
            seq += 1
            _write_record(
                stream,
                {
                    "seq": seq,
                    "type": "user/message",
                    "time": 1_787_429_064_000 + seq,
                    "data": {"content": [{"type": "text", "text": text}]},
                },
            )
    return boundary_seq, (boundary_seq + 1, boundary_seq + 2)


def _make_slow_binary(path):
    """One record immediately, then a stall far past the replay deadline."""

    path.write_text(
        "#!/bin/sh\n"
        "printf '{\"seq\":1,\"type\":\"turn/start\"}\\n'\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o755)


def _make_passthrough_binary(path):
    path.write_text(
        """#!/usr/bin/env python3
import sys

with open(sys.argv[-1], "rb") as source:
    while True:
        chunk = source.read(65536)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
""",
        encoding="utf-8",
    )
    os.chmod(path, 0o755)


class _PassthroughDSHAdapter(DSHAdapter):
    def __init__(self, binary):
        super().__init__()
        self.binary = binary
        self.after_seqs = []

    def replay(self, session, after_seq=None, max_records=1024):
        self.after_seqs.append(after_seq)
        return replay_dsh_events(
            Path(session.transcript),
            after_seq=after_seq,
            max_records=max_records,
            zstd_binary=self.binary,
        )


class SharedAdapterHelperTests(unittest.TestCase):
    def test_mapping_and_epoch_normalization_reject_malformed_values(self):
        value = {"key": "value"}

        self.assertIs(value, as_mapping(value))
        self.assertEqual({}, as_mapping(["not", "a", "mapping"]))
        self.assertIsNone(epoch_seconds(True))
        self.assertIsNone(epoch_seconds("1787429064000"))
        self.assertEqual(1787429064.0, epoch_seconds(1787429064000))
        self.assertEqual(1787429064.0, epoch_seconds(1787429064))

    def test_bounded_json_object_rejects_malformed_nonobject_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "value.json"

            path.write_bytes(b'{"valid":true}')
            self.assertEqual({"valid": True}, read_json_object(path, path.stat().st_size))

            path.write_bytes(b'{"invalid"')
            self.assertEqual({}, read_json_object(path, 1024))

            path.write_bytes(b'["not","an","object"]')
            self.assertEqual({}, read_json_object(path, 1024))

            path.write_bytes(b'{"too":"large"}')
            self.assertEqual({}, read_json_object(path, path.stat().st_size - 1))
            self.assertEqual({}, read_json_object(root / "missing.json", 1024))

    def test_content_event_builder_ignores_malformed_blocks_and_copies_extra(self):
        session = Session(
            agent="dsh",
            session_id="shared-helper",
            project="/tmp/project",
            transcript="/tmp/session.jsonl.zstd",
            updated_at=1787429064.0,
        )
        timestamp = local_timestamp(session.updated_at)
        extra = {"native_type": "fixture"}

        def render(block, default_kind):
            if block.get("type") == "text":
                return default_kind, block.get("text"), 4
            return None

        self.assertEqual(
            [],
            content_block_events(
                {"type": "text", "text": "mapping is not list content"},
                session,
                timestamp,
                "assistant",
                extra,
                render,
            ),
        )
        events = content_block_events(
            [None, 1, {}, {"type": "text", "text": ""}, {"type": "text", "text": "abcde"}],
            session,
            timestamp,
            "assistant",
            extra,
            render,
        )
        extra["native_type"] = "changed"

        self.assertEqual(["assistant"], [event.kind for event in events])
        self.assertEqual(["abc…"], [event.text for event in events])
        self.assertEqual({"native_type": "fixture"}, events[0].extra)


class DSHNormalizeTests(unittest.TestCase):
    def setUp(self):
        self.adapter = DSHAdapter()
        self.session = Session(
            agent="dsh",
            session_id="dsh-session",
            project="/tmp/project",
            transcript="/tmp/session.jsonl.zstd",
            updated_at=1787429064.0,
        )

    def test_assistant_content_blocks_preserve_kinds_text_and_extra_fields(self):
        record = {
            "seq": 17,
            "type": "assistant/message",
            "time": 1787429064000,
            "data": {
                "message": {
                    "content": [
                        {"type": "text", "text": "final answer"},
                        {"type": "reasoning", "text": "careful thought"},
                        {
                            "type": "tool-call",
                            "name": "Read",
                            "arguments": {"path": "fixture.txt"},
                        },
                        {
                            "type": "tool-result",
                            "content": {"output": "fixture\ncontents"},
                        },
                        {"type": "unknown", "text": "ignored"},
                    ]
                }
            },
        }

        events = list(self.adapter.normalize(record, self.session))

        self.assertEqual(
            ["assistant", "thinking", "tool_call", "tool_result"],
            [event.kind for event in events],
        )
        self.assertEqual(
            [
                "final answer",
                "careful thought",
                "Read {'path': 'fixture.txt'}",
                "fixture contents",
            ],
            [event.text for event in events],
        )
        self.assertEqual(
            [local_timestamp(record["time"])] * 4,
            [event.ts for event in events],
        )
        self.assertEqual(
            [{"native_type": "assistant/message", "seq": 17}] * 4,
            [event.extra for event in events],
        )

    def test_content_block_truncation_limits_are_preserved(self):
        record = {
            "type": "assistant/message",
            "data": {
                "message": {
                    "content": [
                        {"type": "text", "text": "a" * 121},
                        {"type": "reasoning", "text": "b" * 81},
                        {
                            "type": "tool-call",
                            "name": "Read",
                            "arguments": "c" * 121,
                        },
                        {"type": "tool-result", "content": "d" * 81},
                    ]
                }
            },
        }

        events = list(self.adapter.normalize(record, self.session))

        self.assertEqual([120, 80, 120, 80], [len(event.text) for event in events])
        self.assertTrue(all(event.text.endswith("…") for event in events))


class DSHReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.passthrough = self.root / "passthrough.py"
        _make_passthrough_binary(self.passthrough)

    def tearDown(self):
        self.temporary.cleanup()

    def test_watcher_advances_after_seq_beyond_old_eight_mib_boundary(self):
        transcript = self.root / "session.jsonl.zstd"
        boundary_seq, tail_seqs = _write_large_transcript(transcript)
        adapter = _PassthroughDSHAdapter(str(self.passthrough))
        session = Session(
            agent="dsh",
            session_id="long-session",
            project="/tmp/project",
            transcript=str(transcript),
            updated_at=1_787_429_064.0,
            extra={"seq": boundary_seq},
        )
        tailer = SessionTailer(session, adapter=adapter, max_records=1)

        first = tailer.poll()
        second = tailer.poll()

        self.assertEqual(["tail one"], [event.text for event in first])
        self.assertEqual(["tail two"], [event.text for event in second])
        self.assertEqual([boundary_seq, tail_seqs[0]], adapter.after_seqs)
        self.assertEqual(tail_seqs, (first[0].extra["seq"], second[0].extra["seq"]))

    def test_scan_ceiling_remains_configurable_and_bounded(self):
        transcript = self.root / "bounded.jsonl.zstd"
        boundary_seq, _ = _write_large_transcript(transcript)

        records = replay_dsh_events(
            transcript,
            after_seq=boundary_seq,
            max_output_bytes=OLD_REPLAY_BOUNDARY,
            zstd_binary=str(self.passthrough),
        )

        self.assertEqual([], records)
        # The scan-byte ceiling stopped before the end of the stream.
        self.assertFalse(records.exhausted)

    def test_retained_bytes_bound_returns_progressive_pages(self):
        transcript = self.root / "retained.jsonl.zstd"
        with transcript.open("wb") as stream:
            first_size = _write_record(stream, {"seq": 1, "value": "a" * 128})
            _write_record(stream, {"seq": 2, "value": "b" * 128})

        first = replay_dsh_events(
            transcript,
            max_retained_bytes=first_size,
            zstd_binary=str(self.passthrough),
        )
        second = replay_dsh_events(
            transcript,
            after_seq=1,
            max_retained_bytes=first_size,
            zstd_binary=str(self.passthrough),
        )

        self.assertEqual([1], [record["seq"] for record in first])
        self.assertEqual([2], [record["seq"] for record in second])
        # The first page stopped early because the second record exceeded its
        # remaining byte budget; the second page then read to end-of-stream.
        self.assertFalse(first.exhausted)
        self.assertTrue(second.exhausted)

    def test_replay_page_reports_exhausted_only_at_true_end(self):
        transcript = self.root / "exhausted.jsonl.zstd"
        with transcript.open("wb") as stream:
            first_size = _write_record(stream, {"seq": 1, "value": "a" * 128})
            _write_record(stream, {"seq": 2, "value": "b" * 128})

        stopped = replay_dsh_events(
            transcript,
            max_retained_bytes=first_size,
            zstd_binary=str(self.passthrough),
        )
        final = replay_dsh_events(
            transcript,
            after_seq=1,
            zstd_binary=str(self.passthrough),
        )
        empty = replay_dsh_events(
            transcript,
            after_seq=2,
            zstd_binary=str(self.passthrough),
        )

        self.assertIsInstance(stopped, ReplayPage)
        self.assertEqual([1], [record["seq"] for record in stopped])
        self.assertFalse(stopped.exhausted)
        # Paging on from the byte-budget stop retrieves the remaining record
        # and only the page that read to end-of-stream reports exhaustion.
        self.assertEqual([2], [record["seq"] for record in final])
        self.assertTrue(final.exhausted)
        self.assertEqual([], list(empty))
        self.assertTrue(empty.exhausted)

    def test_timeout_early_stop_reports_not_exhausted(self):
        transcript = self.root / "slow.jsonl.zstd"
        transcript.write_bytes(b"placeholder\n")
        slow = self.root / "slow.py"
        _make_slow_binary(slow)

        records = replay_dsh_events(
            transcript,
            timeout=1.0,
            zstd_binary=str(slow),
        )

        self.assertEqual([1], [record["seq"] for record in records])
        self.assertFalse(records.exhausted)

    def test_oversized_line_is_dropped_without_blocking_later_records(self):
        transcript = self.root / "oversized.jsonl.zstd"
        with transcript.open("wb") as stream:
            stream.write(b"x" * (2 * 1024 * 1024) + b"\n")
            _write_record(stream, {"seq": 2, "type": "turn/end"})

        records = replay_dsh_events(
            transcript,
            zstd_binary=str(self.passthrough),
        )

        self.assertEqual([2], [record["seq"] for record in records])
        # Dropping an oversized line is not an early budget stop.
        self.assertTrue(records.exhausted)

    def test_incomplete_tail_degrades_to_complete_records(self):
        transcript = self.root / "incomplete.jsonl.zstd"
        with transcript.open("wb") as stream:
            _write_record(stream, {"seq": 1, "type": "turn/start"})
            stream.write(b'{"seq":2,"type":"turn/end"')

        records = replay_dsh_events(
            transcript,
            zstd_binary=str(self.passthrough),
        )

        self.assertEqual([1], [record["seq"] for record in records])
        # The stream itself was fully read; the dangling partial line is not
        # retrievable by another page, so the page counts as exhausted.
        self.assertTrue(records.exhausted)

    def test_missing_zstd_degrades_to_no_records(self):
        transcript = self.root / "missing.jsonl.zstd"
        transcript.write_bytes(b'{"seq":1}\n')

        records = replay_dsh_events(
            transcript,
            zstd_binary="agent-sidecar-definitely-missing-zstd",
        )

        self.assertEqual([], records)
        # Degraded sources have nothing retrievable, so paging on is futile.
        self.assertTrue(records.exhausted)

    @unittest.skipUnless(ZSTD, "zstd binary is unavailable")
    def test_real_zstd_fixture_reaches_records_after_eight_mib(self):
        source = self.root / "large.jsonl"
        compressed = self.root / "large.jsonl.zstd"
        boundary_seq, tail_seqs = _write_large_transcript(source)
        subprocess.run(
            [ZSTD, "-q", "-f", str(source), "-o", str(compressed)],
            check=True,
            timeout=10,
        )

        records = replay_dsh_events(
            compressed,
            after_seq=boundary_seq,
            max_records=2,
        )

        self.assertEqual(list(tail_seqs), [record["seq"] for record in records])
        # A record-budget stop cannot prove the transcript ended with it.
        self.assertFalse(records.exhausted)


if __name__ == "__main__":
    unittest.main()
