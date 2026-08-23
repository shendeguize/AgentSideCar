import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidecar.adapters import codex
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

    def source_state(self, *paths):
        state = {}
        for path in paths:
            if not path.exists():
                state[path.name] = None
                continue
            contents = path.read_bytes()
            details = path.stat()
            state[path.name] = {
                "bytes": contents,
                "sha256": hashlib.sha256(contents).hexdigest(),
                "metadata": (
                    details.st_dev,
                    details.st_ino,
                    details.st_mode,
                    details.st_size,
                    details.st_mtime_ns,
                    details.st_ctime_ns,
                ),
            }
        return state

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

    def test_native_wal_status_does_not_mutate_source_sqlite_files(self):
        status_db = self.root / "thread_history_1.sqlite"
        wal = Path(str(status_db) + "-wal")
        shm = Path(str(status_db) + "-shm")
        transcript = self.root / "wal.jsonl"
        transcript.touch()

        writer = sqlite3.connect(status_db)
        try:
            writer.execute("PRAGMA journal_mode = WAL")
            writer.execute("PRAGMA wal_autocheckpoint = 0")
            writer.execute(
                "CREATE TABLE thread_turns "
                "(thread_id TEXT, status TEXT, updated_at REAL)"
            )
            writer.execute(
                "INSERT INTO thread_turns VALUES (?, ?, ?)",
                ("wal-thread", "working", self.now - 1),
            )
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            writer.execute(
                "INSERT INTO thread_turns VALUES (?, ?, ?)",
                ("wal-thread", "completed", self.now),
            )
            writer.commit()

            with sqlite3.connect(
                status_db.resolve().as_uri() + "?mode=ro&immutable=1",
                uri=True,
            ) as main_only:
                self.assertEqual(
                    "working",
                    main_only.execute(
                        "SELECT status FROM thread_turns "
                        "WHERE thread_id = ? ORDER BY rowid DESC LIMIT 1",
                        ("wal-thread",),
                    ).fetchone()[0],
                )

            paths = (status_db, wal, shm)
            before = self.source_state(*paths)
            self.assertTrue(all(before[path.name] is not None for path in paths))

            session = self.session(
                transcript,
                "wal-thread",
                age_seconds=1,
                status_db=status_db,
            )
            self.assertEqual(
                Status.WAITING,
                self.adapter.infer_status(session, now=self.now),
            )
            self.assertEqual(before, self.source_state(*paths))
        finally:
            writer.close()

    def test_native_status_without_sidecars_does_not_create_them(self):
        status_db = self.write_status_db()
        wal = Path(str(status_db) + "-wal")
        shm = Path(str(status_db) + "-shm")
        transcript = self.root / "no-sidecars.jsonl"
        transcript.touch()
        before = self.source_state(status_db, wal, shm)

        session = self.session(
            transcript,
            "working-thread",
            age_seconds=1,
            status_db=status_db,
        )

        self.assertEqual(
            Status.WORKING,
            self.adapter.infer_status(session, now=self.now),
        )
        self.assertEqual(before, self.source_state(status_db, wal, shm))

    def test_rotating_sqlite_sidecars_retry_without_crashing_scan(self):
        status_db = self.write_status_db()
        wal = Path(str(status_db) + "-wal")
        shm = Path(str(status_db) + "-shm")
        wal.write_bytes(b"rotating wal")
        shm.write_bytes(b"rotating shm")
        transcript = self.root / "rotating-sidecars.jsonl"
        transcript.touch()
        session = self.session(
            transcript,
            "completed-thread",
            age_seconds=1,
            status_db=status_db,
        )
        original_copy = codex._copy_regular_file
        rotated = []

        def rotate_sidecars(source, destination, expected_size):
            if source == wal and not rotated:
                rotated.append(True)
                wal.unlink()
                shm.unlink()
                raise FileNotFoundError(str(wal))
            return original_copy(source, destination, expected_size)

        with mock.patch.object(
            codex,
            "_copy_regular_file",
            side_effect=rotate_sidecars,
        ):
            self.assertEqual(
                Status.WAITING,
                self.adapter.infer_status(session, now=self.now),
            )

        self.assertEqual([True], rotated)
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())

    def test_partial_snapshot_copy_falls_back_without_crashing_scan(self):
        status_db = self.write_status_db()
        transcript = self.root / "partial-copy.jsonl"
        transcript.touch()
        session = self.session(
            transcript,
            "working-thread",
            age_seconds=1,
            status_db=status_db,
        )
        before = self.source_state(status_db)
        original_copy = codex._copy_regular_file
        partial_copies = []

        def copy_partially(source, destination, expected_size):
            if source == status_db and not partial_copies:
                partial_copies.append(True)
                destination.write_bytes(source.read_bytes()[:-1])
                return expected_size - 1
            return original_copy(source, destination, expected_size)

        with mock.patch.object(
            codex,
            "_copy_regular_file",
            side_effect=copy_partially,
        ):
            self.assertEqual(
                Status.WORKING,
                self.adapter.infer_status(session, now=self.now),
            )

        self.assertEqual([True], partial_copies)
        self.assertEqual(before, self.source_state(status_db))


if __name__ == "__main__":
    unittest.main()
