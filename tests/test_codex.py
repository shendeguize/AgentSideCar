import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sidecar.adapters.codex import CodexAdapter
from sidecar.model import Session, Status


class CodexAdapterStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.now = 2_000_000.0
        self.adapter = CodexAdapter()

    def tearDown(self):
        self.temporary.cleanup()

    def write_rollout(self, name, payload_type):
        path = self.root / name
        path.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": payload_type},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    def session(self, transcript, session_id, age_seconds, status_db=None):
        extra = {}
        if status_db is not None:
            extra["status_db"] = str(status_db)
        return Session(
            agent="codex",
            session_id=session_id,
            project=str(self.root),
            transcript=str(transcript),
            updated_at=self.now - age_seconds,
            extra=extra,
        )

    def write_status_db(self):
        path = self.root / "thread_history_1.sqlite"
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE thread_turns "
                "(thread_id TEXT, status TEXT, updated_at REAL)"
            )
            connection.executemany(
                "INSERT INTO thread_turns VALUES (?, ?, ?)",
                [
                    ("working-thread", "working", self.now),
                    ("completed-thread", "completed", self.now),
                ],
            )
        return path

    def test_multi_day_tail_statuses_are_idle(self):
        for payload_type in ("task_started", "task_complete"):
            with self.subTest(payload_type=payload_type):
                transcript = self.write_rollout(
                    "{}.jsonl".format(payload_type),
                    payload_type,
                )
                session = self.session(
                    transcript,
                    payload_type,
                    age_seconds=3 * 24 * 60 * 60,
                )

                self.assertEqual(
                    Status.IDLE,
                    self.adapter.infer_status(session, now=self.now),
                )

    def test_multi_day_native_statuses_are_idle(self):
        status_db = self.write_status_db()
        for session_id in ("working-thread", "completed-thread"):
            with self.subTest(session_id=session_id):
                transcript = self.root / "{}.jsonl".format(session_id)
                transcript.touch()
                session = self.session(
                    transcript,
                    session_id,
                    age_seconds=3 * 24 * 60 * 60,
                    status_db=status_db,
                )

                self.assertEqual(
                    Status.IDLE,
                    self.adapter.infer_status(session, now=self.now),
                )

    def test_recent_tail_transitions_remain_live(self):
        expected = {
            "task_started": Status.WORKING,
            "task_complete": Status.WAITING,
        }
        for payload_type, status in expected.items():
            with self.subTest(payload_type=payload_type):
                transcript = self.write_rollout(
                    "recent-{}.jsonl".format(payload_type),
                    payload_type,
                )
                session = self.session(
                    transcript,
                    payload_type,
                    age_seconds=8 * 60,
                )

                self.assertEqual(
                    status,
                    self.adapter.infer_status(session, now=self.now),
                )

    def test_recent_native_transitions_remain_live(self):
        status_db = self.write_status_db()
        expected = {
            "working-thread": Status.WORKING,
            "completed-thread": Status.WAITING,
        }
        for session_id, status in expected.items():
            with self.subTest(session_id=session_id):
                transcript = self.root / "recent-{}.jsonl".format(session_id)
                transcript.touch()
                session = self.session(
                    transcript,
                    session_id,
                    age_seconds=8 * 60,
                    status_db=status_db,
                )

                self.assertEqual(
                    status,
                    self.adapter.infer_status(session, now=self.now),
                )


if __name__ == "__main__":
    unittest.main()
