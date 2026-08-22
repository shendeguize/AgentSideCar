import tempfile
import unittest
from pathlib import Path

from sidecar.model import Event, Session
from sidecar.tail import JSONLFollower, SessionTailer


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


if __name__ == "__main__":
    unittest.main()
