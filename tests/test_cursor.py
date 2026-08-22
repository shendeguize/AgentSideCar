import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sidecar.adapters.cursor import CursorAdapter
from sidecar.index import IncrementalIndex
from sidecar.model import Status
from sidecar.scan import Scanner
from sidecar.state import StateEngine


class CursorAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.store = (
            self.home
            / ".cursor"
            / "chats"
            / "cwd-hash-value"
            / "session-id"
            / "store.db"
        )
        self.store.parent.mkdir(parents=True)
        self.writer = sqlite3.connect(str(self.store))
        self.assertEqual("wal", self.writer.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        self.writer.execute("PRAGMA wal_autocheckpoint = 0")
        self.writer.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB)")
        self.writer.commit()
        self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.writer.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (
                ("name", "Live WAL session"),
                ("cwd", "/tmp/live-wal-project"),
            ),
        )
        self.writer.commit()

    def tearDown(self):
        self.writer.close()
        self.temporary.cleanup()

    @property
    def sqlite_files(self):
        return (
            self.store,
            Path(str(self.store) + "-wal"),
            Path(str(self.store) + "-shm"),
        )

    def snapshot(self, paths=None):
        paths = self.sqlite_files if paths is None else paths
        return {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
            for path in paths
        }

    def set_mtimes(self, now, db_age, wal_age, shm_age):
        for path, age in zip(
            self.sqlite_files,
            (db_age, wal_age, shm_age),
        ):
            timestamp_ns = int((now - age) * 1e9)
            os.utime(path, ns=(timestamp_ns, timestamp_ns))

    def test_discovers_metadata_committed_only_to_wal_without_mutation(self):
        immutable = sqlite3.connect(
            self.store.resolve().as_uri() + "?mode=ro&immutable=1",
            uri=True,
        )
        try:
            self.assertEqual([], immutable.execute("SELECT key, value FROM meta").fetchall())
        finally:
            immutable.close()

        mtimes = {
            "store.db": 1_700_000_000_000_000_000,
            "store.db-wal": 1_700_000_001_000_000_000,
            "store.db-shm": 1_700_000_002_000_000_000,
        }
        for path in self.sqlite_files:
            os.utime(path, ns=(mtimes[path.name], mtimes[path.name]))
        persistent_before = self.snapshot(self.sqlite_files[:2])

        sessions = list(CursorAdapter().discover(self.home))

        self.assertEqual(1, len(sessions))
        session = sessions[0]
        self.assertEqual("Live WAL session", session.title)
        self.assertEqual("/tmp/live-wal-project", session.project)
        after = self.snapshot()
        self.assertEqual(
            max(details[1] for details in after.values()) / 1e9,
            session.updated_at,
        )
        signature = session.extra["store_signature"]
        for name, path in zip(("db", "wal", "shm"), self.sqlite_files):
            self.assertEqual(
                {
                    "exists": True,
                    "mtime_ns": after[path.name][1],
                    "size": after[path.name][2],
                },
                signature[name],
            )
        self.assertEqual(persistent_before, self.snapshot(self.sqlite_files[:2]))
        self.assertEqual(
            [("cwd", "/tmp/live-wal-project"), ("name", "Live WAL session")],
            self.writer.execute("SELECT key, value FROM meta ORDER BY key").fetchall(),
        )

    def test_wal_growth_changes_incremental_signature(self):
        adapter = CursorAdapter()
        index = IncrementalIndex()
        first = list(adapter.discover(self.home))[0]
        index.update([first])
        first_signature = first.extra["store_signature"]["wal"]

        self.writer.execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            ("Updated live WAL session", "name"),
        )
        self.writer.commit()
        wal = Path(str(self.store) + "-wal")
        newer_ns = max(path.stat().st_mtime_ns for path in self.sqlite_files) + 1_000_000_000
        os.utime(wal, ns=(newer_ns, newer_ns))

        second = list(adapter.discover(self.home))[0]
        delta = index.update([second])

        self.assertEqual("Updated live WAL session", second.title)
        self.assertNotEqual(first_signature, second.extra["store_signature"]["wal"])
        self.assertEqual(newer_ns / 1e9, second.updated_at)
        self.assertEqual({("cursor-cli", "session-id")}, delta.changed)

    def test_recent_db_or_wal_keeps_cli_session_working(self):
        now = 2_000_000_000.0
        adapter = CursorAdapter()
        for fresh_name in ("store.db", "store.db-wal"):
            with self.subTest(fresh_name=fresh_name):
                ages = {
                    "store.db": 3600,
                    "store.db-wal": 3600,
                    "store.db-shm": 3600,
                }
                ages[fresh_name] = 30
                self.set_mtimes(
                    now,
                    ages["store.db"],
                    ages["store.db-wal"],
                    ages["store.db-shm"],
                )

                sessions = Scanner([adapter], home=self.home).scan(now=now)

                self.assertEqual(1, len(sessions))
                self.assertEqual("", sessions[0].transcript)
                self.assertEqual(Status.WORKING, sessions[0].status)

    def test_recent_but_not_fresh_cli_store_is_waiting(self):
        now = 2_000_000_000.0
        self.set_mtimes(now, db_age=300, wal_age=3600, shm_age=3600)

        session = Scanner([CursorAdapter()], home=self.home).scan(now=now)[0]

        self.assertEqual(Status.WAITING, session.status)

    def test_fresh_shm_does_not_promote_stale_cli_session(self):
        now = 2_000_000_000.0
        self.set_mtimes(now, db_age=3600, wal_age=3600, shm_age=1)

        session = Scanner([CursorAdapter()], home=self.home).scan(now=now)[0]

        self.assertEqual(now - 1, session.updated_at)
        self.assertEqual(Status.IDLE, session.status)

    def test_missing_cli_store_is_dead(self):
        now = 2_000_000_000.0
        adapter = CursorAdapter()
        session = list(adapter.discover(self.home))[0]
        session.extra["store"] = str(self.home / "missing-store.db")

        self.assertEqual(Status.DEAD, adapter.infer_status(session, now=now))

    def test_cursor_ide_status_still_uses_generic_state_engine(self):
        now = 2_000_000_000.0
        transcript = (
            self.home
            / ".cursor"
            / "projects"
            / "project"
            / "agent-transcripts"
            / "ide-session.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            '{"role":"user","content":"continue working"}\n',
            encoding="utf-8",
        )
        timestamp = now - 10
        os.utime(transcript, (timestamp, timestamp))
        adapter = CursorAdapter()
        session = next(
            item for item in adapter.discover(self.home) if item.agent == "cursor-ide"
        )

        self.assertIsNone(adapter.infer_status(session, now=now))
        self.assertEqual(
            Status.WORKING,
            StateEngine(home=self.home).infer_status(
                session,
                adapter=adapter,
                now=now,
            ),
        )


if __name__ == "__main__":
    unittest.main()
