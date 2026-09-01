import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sidecar.adapters.cursor as cursor_module
from sidecar.adapters.cursor import CursorAdapter, _read_cli_meta
from sidecar.cursor_chat import (
    CursorChatSchemaError,
    CursorChatSnapshotBroker,
    default_snapshot_broker,
    reset_default_snapshot_broker,
)
from sidecar.index import IncrementalIndex
from sidecar.model import Session, Status
from sidecar.scan import Scanner
from sidecar.state import StateEngine
from sidecar.tailer_pool import TailerPool
from sidecar.text_utils import CURSOR_TITLE_MAX_TEXTS, extract_cursor_title


def varint(value):
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def wire_bytes(field, payload):
    return varint((field << 3) | 2) + varint(len(payload)) + payload


def json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def create_production_store(
    path,
    messages,
    *,
    workspace="file:///Users/example/My%20Project",
    agent_id="agent-production-shaped",
    name="Wrong metadata title",
    mode="agent",
    created_at=1_787_430_000_000,
):
    connection = sqlite3.connect(str(path))
    if connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
        raise AssertionError("WAL mode unavailable")
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    message_ids = []
    for message in messages:
        payload = json_bytes(message)
        message_id = hashlib.sha256(payload).hexdigest()
        connection.execute(
            "INSERT INTO blobs (id, data) VALUES (?, ?)",
            (message_id, payload),
        )
        message_ids.append(message_id)
    root_data = b"".join(
        wire_bytes(1, bytes.fromhex(message_id)) for message_id in message_ids
    ) + wire_bytes(9, workspace.encode("utf-8"))
    root_id = hashlib.sha256(root_data).hexdigest()
    connection.execute(
        "INSERT INTO blobs (id, data) VALUES (?, ?)",
        (root_id, root_data),
    )
    metadata = {
        "agentId": agent_id,
        "latestRootBlobId": root_id,
        "name": name,
        "mode": mode,
        "createdAt": created_at,
    }
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        ("0", json_bytes(metadata).hex()),
    )
    connection.commit()
    connection.execute("SELECT count(*) FROM blobs").fetchone()
    return connection, root_id


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
        self.assertEqual(
            "wal", self.writer.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        )
        self.writer.execute("PRAGMA wal_autocheckpoint = 0")
        self.writer.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB)")
        self.writer.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (
                ("name", "Checkpointed session"),
                ("cwd", "/tmp/checkpointed-project"),
            ),
        )
        self.writer.commit()
        self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.writer.executemany(
            "UPDATE meta SET value = ? WHERE key = ?",
            (
                ("Live WAL session", "name"),
                ("/tmp/live-wal-project", "cwd"),
            ),
        )
        self.writer.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("wal-only", "uncheckpointed"),
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
        result = {}
        for path in paths:
            data = path.read_bytes()
            stat_result = path.stat()
            result[path.name] = {
                "bytes": data,
                "sha256": hashlib.sha256(data).hexdigest(),
                "mtime_ns": stat_result.st_mtime_ns,
                "size": stat_result.st_size,
            }
        return result

    def set_mtimes(self, now, db_age, wal_age, shm_age):
        for path, age in zip(
            self.sqlite_files,
            (db_age, wal_age, shm_age),
        ):
            timestamp_ns = int((now - age) * 1e9)
            os.utime(path, ns=(timestamp_ns, timestamp_ns))

    def test_read_and_discover_leave_live_wal_store_bit_identical(self):
        mtimes = {
            "store.db": 1_700_000_000_000_000_000,
            "store.db-wal": 1_700_000_001_000_000_000,
            "store.db-shm": 1_700_000_002_000_000_000,
        }
        for path in self.sqlite_files:
            os.utime(path, ns=(mtimes[path.name], mtimes[path.name]))
        before = self.snapshot()
        self.assertGreater(before["store.db-wal"]["size"], 0)
        self.assertGreater(before["store.db-shm"]["size"], 0)

        title, project, meta = _read_cli_meta(self.store)

        self.assertEqual("Checkpointed session", title)
        self.assertEqual("/tmp/checkpointed-project", project)
        self.assertEqual(["cwd", "name"], meta["meta_keys"])
        self.assertEqual(before, self.snapshot())

        sessions = list(CursorAdapter().discover(self.home))

        self.assertEqual(1, len(sessions))
        session = sessions[0]
        self.assertEqual("Checkpointed session", session.title)
        self.assertEqual("/tmp/checkpointed-project", session.project)
        self.assertEqual(str(self.store), session.transcript)
        self.assertEqual("cursor-chat-sqlite", session.extra["transcript_kind"])
        self.assertEqual("fallback", session.extra["cursor_chat_snapshot"])
        self.assertEqual("CursorChatSchemaError", session.extra["cursor_chat_error"])
        after = self.snapshot()
        self.assertEqual(before, after)
        self.assertEqual(
            max(
                after["store.db"]["mtime_ns"],
                after["store.db-wal"]["mtime_ns"],
            )
            / 1e9,
            session.updated_at,
        )
        signature = session.extra["store_signature"]
        for name, path in zip(("db", "wal", "shm"), self.sqlite_files):
            self.assertEqual(
                {
                    "exists": True,
                    "mtime_ns": after[path.name]["mtime_ns"],
                    "size": after[path.name]["size"],
                },
                signature[name],
            )
        self.assertEqual(
            [
                ("cwd", "/tmp/live-wal-project"),
                ("name", "Live WAL session"),
                ("wal-only", "uncheckpointed"),
            ],
            self.writer.execute("SELECT key, value FROM meta ORDER BY key").fetchall(),
        )

    def test_discovers_cursor_server_project_transcripts(self):
        transcript = (
            self.home
            / ".cursor-server"
            / "projects"
            / "server-project"
            / "agent-transcripts"
            / "server-session.jsonl"
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            '{"role":"user","content":"remote Cursor session"}\n',
            encoding="utf-8",
        )

        sessions = [
            session
            for session in CursorAdapter().discover(self.home)
            if session.extra.get("source") == "cursor-server"
        ]

        self.assertEqual(1, len(sessions))
        self.assertEqual("cursor-ide", sessions[0].agent)
        self.assertEqual("server-project", sessions[0].project)
        self.assertEqual("cursor-server", sessions[0].extra["source"])
        self.assertEqual(str(transcript), sessions[0].transcript)

    def test_discovers_cursor_server_user_history(self):
        history = self.home / ".cursor-server" / "data" / "User" / "History" / "abc123"
        history.mkdir(parents=True)
        (history / "entries.json").write_text(
            '{"version":1,"resource":"/workspace/remote-app","entries":[]}',
            encoding="utf-8",
        )
        (history / "turn.md").write_text("remote Cursor turn", encoding="utf-8")

        sessions = [
            session for session in CursorAdapter().discover(self.home)
            if session.extra.get("source") == "cursor-server"
        ]

        self.assertEqual(1, len(sessions))
        self.assertEqual("cursor-server:abc123", sessions[0].session_id)
        self.assertEqual("/workspace/remote-app", sessions[0].project)
        self.assertEqual("cursor-server-history", sessions[0].extra["transcript_kind"])

    def test_discovers_production_snapshot_metadata_without_source_mutation(self):
        self.writer.close()
        for path in self.sqlite_files:
            path.unlink(missing_ok=True)
        messages = [
            {"role": "system", "content": "Generated instructions"},
            {
                "role": "user",
                "content": "<user_info>sensitive context</user_info>",
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "<user_info>context</user_info>\n"
                        "<user_query>Build production discovery</user_query>",
                    }
                ],
            },
            {"role": "assistant", "content": "Working"},
        ]
        self.writer, root_id = create_production_store(self.store, messages)
        before = self.snapshot()

        session = list(CursorAdapter().discover(self.home))[0]

        self.assertEqual(before, self.snapshot())
        self.assertEqual("session-id", session.session_id)
        self.assertEqual("Build production discovery", session.title)
        self.assertEqual("/Users/example/My Project", session.project)
        self.assertEqual(str(self.store), session.transcript)
        self.assertEqual("cursor-chat-sqlite", session.extra["transcript_kind"])
        self.assertEqual("production", session.extra["cursor_chat_snapshot"])
        self.assertEqual("agent-production-shaped", session.extra["agentId"])
        self.assertEqual("Wrong metadata title", session.extra["name"])
        self.assertEqual("agent", session.extra["mode"])
        self.assertEqual(1_787_430_000.0, session.extra["createdAt"])
        self.assertEqual(root_id, session.extra["latestRoot"])
        self.assertEqual("session-id", session.extra["directory_session_id"])
        self.assertFalse(session.extra["agent_id_matches_directory"])
        self.assertTrue(session.extra["session_id_mismatch"])
        self.assertEqual(str(Path(str(self.store) + "-wal")), session.extra["wal"])
        self.assertEqual(
            session.extra["store_signature"]["db"],
            session.extra["db_signature"],
        )
        self.assertEqual(
            session.extra["store_signature"]["wal"],
            session.extra["wal_signature"],
        )

    def test_one_decode_shared_across_discovery_checkpoint_and_tailing(self):
        self.writer.close()
        for path in self.sqlite_files:
            path.unlink(missing_ok=True)
        self.writer, _root_id = create_production_store(
            self.store,
            [
                {
                    "role": "user",
                    "content": "<user_query>Shared snapshot</user_query>",
                }
            ],
        )
        reset_default_snapshot_broker()
        broker = default_snapshot_broker()
        adapter = CursorAdapter()
        pool = TailerPool(
            lambda event: None,
            tail_recent_seconds=1.0,
        )
        key = ("cursor-cli", "session-id")
        try:
            expired = list(adapter.discover(self.home))[0]
            expired.updated_at = 0.0
            expired.status = Status.IDLE

            pool.refresh(
                [expired],
                changed_keys={key},
                initial=True,
                now=10.0,
            )

            self.assertEqual({key}, pool.state.checkpoints)
            self.assertEqual(1, broker.stats.snapshot_loads)
            self.assertEqual(1, broker.stats.cache_hits)

            unchanged = broker.stats
            pool.refresh(
                [expired],
                changed_keys={key},
                now=11.0,
            )
            self.assertEqual(unchanged.snapshot_loads, broker.stats.snapshot_loads)
            self.assertEqual(unchanged.signature_checks, broker.stats.signature_checks)

            wal = Path(str(self.store) + "-wal")
            details = wal.stat()
            os.utime(
                wal,
                ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000),
            )
            active = list(adapter.discover(self.home))[0]
            active.status = Status.WORKING
            pool.refresh(
                [active],
                changed_keys={key},
                now=12.0,
            )

            self.assertEqual(2, broker.stats.snapshot_loads)
            self.assertEqual(frozenset(), pool.state.checkpoints)
            after_changed = broker.stats

            same = list(adapter.discover(self.home))[0]
            same.status = Status.WORKING
            pool.refresh(
                [same],
                changed_keys={key},
                now=13.0,
            )

            self.assertEqual(
                after_changed.snapshot_loads,
                broker.stats.snapshot_loads,
            )
            self.assertEqual(
                after_changed.signature_checks,
                broker.stats.signature_checks,
            )
        finally:
            pool.close()
            reset_default_snapshot_broker()

    def test_adapter_generation_nests_inside_outer_scope(self):
        self.writer.close()
        for path in self.sqlite_files:
            path.unlink(missing_ok=True)
        self.writer, _root_id = create_production_store(
            self.store,
            [{"role": "user", "content": "Nested generation"}],
        )
        now = [10.0]
        broker = CursorChatSnapshotBroker(
            ttl_seconds=1.0,
            clock=lambda: now[0],
        )
        adapter = CursorAdapter(snapshot_broker=broker)

        with broker.scan_generation() as generation:
            first = list(adapter.discover(self.home))
            self.assertEqual(1, broker.scan_depth)
            self.assertEqual(generation, broker.stats.generation)
            self.assertEqual(1, broker.stats.snapshot_loads)

            now[0] += 2.0
            second = list(adapter.discover(self.home))
            self.assertEqual(1, broker.scan_depth)
            self.assertEqual(generation, broker.stats.generation)
            self.assertEqual(1, broker.stats.snapshot_loads)
            self.assertEqual(first[0].title, second[0].title)
            self.assertEqual(1, broker.stats.entries)

        self.assertEqual(0, broker.scan_depth)
        self.assertEqual(1, broker.stats.entries)
        now[0] += 2.0
        with broker.scan_generation():
            pass
        self.assertEqual(0, broker.stats.entries)

    def test_typed_snapshot_failure_falls_back_without_error_text(self):
        with (
            mock.patch.object(
                cursor_module,
                "snapshot_cursor_chat",
                side_effect=CursorChatSchemaError("private title and user text"),
            ),
            mock.patch.object(
                cursor_module,
                "_read_cli_meta",
                wraps=cursor_module._read_cli_meta,
            ) as fallback,
        ):
            session = list(CursorAdapter().discover(self.home))[0]

        self.assertEqual(1, fallback.call_count)
        self.assertEqual("Checkpointed session", session.title)
        self.assertEqual("/tmp/checkpointed-project", session.project)
        serialized = json.dumps(session.extra)
        self.assertNotIn("private title", serialized)
        self.assertNotIn("user text", serialized)
        self.assertEqual("CursorChatSchemaError", session.extra["cursor_chat_error"])

    def test_malformed_fallback_store_keeps_valid_sibling_and_utf8_json(self):
        malformed_store = (
            self.home
            / ".cursor"
            / "chats"
            / "malformed-cwd-hash"
            / "malformed-session"
            / "store.db"
        )
        malformed_store.parent.mkdir(parents=True)
        connection = sqlite3.connect(str(malformed_store))
        try:
            connection.execute(
                "CREATE TABLE meta (key BLOB PRIMARY KEY, value BLOB)"
            )
            connection.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (
                    ("name", json.dumps("\ud800")),
                    ("cwd", json.dumps({"projectPath": "\udfff"})),
                    ("deep", "[" * 2000 + "0" + "]" * 2000),
                    (sqlite3.Binary(b"\xed\xa0\x80"), "invalid key"),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        title, project, metadata = _read_cli_meta(malformed_store)
        self.assertEqual("", title)
        self.assertEqual("", project)
        self.assertEqual(["cwd", "deep", "name"], metadata["meta_keys"])

        with mock.patch.object(
            cursor_module,
            "snapshot_cursor_chat",
            side_effect=CursorChatSchemaError("malformed"),
        ):
            sessions = list(CursorAdapter().discover(self.home))

        by_id = {session.session_id: session for session in sessions}
        self.assertEqual({"session-id", "malformed-session"}, set(by_id))
        self.assertEqual("Checkpointed session", by_id["session-id"].title)
        self.assertEqual(
            "/tmp/checkpointed-project",
            by_id["session-id"].project,
        )
        malformed = by_id["malformed-session"]
        self.assertEqual("", malformed.title)
        self.assertEqual("cwd-hash:malforme", malformed.project)
        self.assertEqual(["cwd", "deep", "name"], malformed.extra["meta_keys"])

        serialized = json.dumps(
            [session.to_dict() for session in sessions],
            ensure_ascii=False,
        )
        serialized.encode("utf-8")

    def test_fallback_json_type_failure_is_best_effort(self):
        with mock.patch.object(
            cursor_module.json,
            "loads",
            side_effect=TypeError("decoder rejected input"),
        ):
            title, project, metadata = _read_cli_meta(self.store)

        self.assertEqual("Checkpointed session", title)
        self.assertEqual("/tmp/checkpointed-project", project)
        self.assertEqual(["cwd", "name"], metadata["meta_keys"])

    def test_untyped_snapshot_failure_is_not_silently_downgraded(self):
        broker = CursorChatSnapshotBroker()
        with (
            mock.patch.object(
                cursor_module,
                "snapshot_cursor_chat",
                side_effect=RuntimeError("unexpected"),
            ),
            self.assertRaises(RuntimeError),
        ):
            list(
                CursorAdapter(snapshot_broker=broker).discover(self.home)
            )
        self.assertEqual(0, broker.scan_depth)

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
        newer_ns = (
            max(path.stat().st_mtime_ns for path in self.sqlite_files) + 1_000_000_000
        )
        os.utime(wal, ns=(newer_ns, newer_ns))

        second = list(adapter.discover(self.home))[0]
        delta = index.update([second])

        self.assertEqual("Checkpointed session", second.title)
        self.assertNotEqual(first_signature, second.extra["store_signature"]["wal"])
        self.assertEqual(newer_ns / 1e9, second.updated_at)
        self.assertEqual({("cursor-cli", "session-id")}, delta.changed)

    def test_same_size_mtime_wal_replacement_invalidates_snapshot_metadata(self):
        broker = CursorChatSnapshotBroker()
        adapter = CursorAdapter(snapshot_broker=broker)
        first = list(adapter.discover(self.home))[0]
        wal = Path(str(self.store) + "-wal")
        details = wal.stat()
        replacement = self.home / "replacement-wal"
        replacement.write_bytes(wal.read_bytes())
        os.utime(
            replacement,
            ns=(details.st_atime_ns, details.st_mtime_ns),
        )

        os.replace(str(replacement), str(wal))
        second = list(adapter.discover(self.home))[0]

        self.assertEqual(first.title, second.title)
        self.assertEqual(first.project, second.project)
        self.assertEqual(2, broker.stats.snapshot_loads)

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
                self.assertEqual(str(self.store), sessions[0].transcript)
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

        self.assertEqual(now - 3600, session.updated_at)
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

    def test_title_helper_bounds_text_count_and_malformed_metadata(self):
        inspected = []

        def payloads():
            for index in range(CURSOR_TITLE_MAX_TEXTS + 1):
                inspected.append(index)
                if index == 0:
                    yield "First fallback"
                elif index == CURSOR_TITLE_MAX_TEXTS:
                    yield "<user_query>Too late</user_query>"
                else:
                    yield "<user_info>generated</user_info>"

        self.assertEqual("First fallback", extract_cursor_title(payloads()))
        self.assertEqual(CURSOR_TITLE_MAX_TEXTS, len(inspected))
        self.assertEqual(
            "Metadata fallback",
            extract_cursor_title(
                ("private metadata</user_info>",),
                "Metadata fallback",
            ),
        )

    def test_normalizes_cursor_chat_events_and_safe_extras(self):
        session = Session(
            agent="cursor-cli",
            session_id="session-id",
            project="/work",
            transcript=str(self.store),
            updated_at=1_800_000_000.0,
            extra={"transcript_kind": "cursor-chat-sqlite"},
        )
        metadata = {
            "source": "cursor-cli",
            "kind": "message",
            "synthetic": False,
            "provisional": False,
            "root_blob_id": "r" * 64,
            "message_id": "m" * 64,
            "timestamp_source": "observed",
        }
        adapter = CursorAdapter()

        self.assertEqual(
            [],
            list(
                adapter.normalize(
                    {
                        "role": "system",
                        "content": "generated system prompt",
                        "_cursor_chat": metadata,
                    },
                    session,
                )
            ),
        )
        self.assertEqual(
            [],
            list(
                adapter.normalize(
                    {
                        "role": "user",
                        "content": "<user_info>generated context</user_info>",
                        "_cursor_chat": metadata,
                    },
                    session,
                )
            ),
        )

        records = [
            {
                "role": "user",
                "content": [
                    {"type": "unknown", "text": "ignore"},
                    {
                        "type": "text",
                        "text": "<user_info>context</user_info>"
                        "<user_query>Actual user request</user_query>",
                    },
                ],
                "timestamp": 1_800_000_001.0,
                "_cursor_chat": metadata,
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Assistant response"},
                    {"type": "reasoning", "text": "Private reasoning summary"},
                    {
                        "type": "tool-call",
                        "toolCallId": "call-1",
                        "toolName": "Read",
                        "args": {"path": "/private/path", "long": "x" * 200},
                        "providerOptions": {"secret": "do-not-copy"},
                    },
                ],
                "_cursor_chat": metadata,
            },
            {
                "role": "tool",
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": "call-1",
                        "toolName": "Read",
                        "result": "result " + "y" * 200,
                    }
                ],
                "_cursor_chat": metadata,
            },
        ]
        events = [
            event for record in records for event in adapter.normalize(record, session)
        ]

        self.assertEqual(
            ["user", "assistant", "thinking", "tool_call", "tool_result"],
            [event.kind for event in events],
        )
        self.assertEqual("Actual user request", events[0].text)
        self.assertLessEqual(len(events[2].text), 80)
        self.assertLessEqual(len(events[3].text), 120)
        self.assertLessEqual(len(events[4].text), 80)
        tool_call = events[3]
        self.assertEqual("call-1", tool_call.extra["tool_call_id"])
        self.assertEqual("Read", tool_call.extra["tool_name"])
        self.assertEqual("r" * 64, tool_call.extra["root_blob_id"])
        self.assertEqual("m" * 64, tool_call.extra["message_id"])
        self.assertEqual("observed", tool_call.extra["timestamp_source"])
        self.assertFalse(tool_call.extra["synthetic"])
        self.assertFalse(tool_call.extra["provisional"])
        serialized = json.dumps([event.to_dict() for event in events])
        extras = json.dumps([event.extra for event in events])
        self.assertIn("Actual user request", serialized)
        self.assertNotIn("providerOptions", extras)
        self.assertNotIn("do-not-copy", extras)
        self.assertNotIn("/private/path", extras)

    def test_empty_cursor_tool_payloads_keep_paired_events(self):
        session = Session(
            agent="cursor-cli",
            session_id="session-id",
            project="/work",
            transcript=str(self.store),
            updated_at=1_800_000_000.0,
            extra={"transcript_kind": "cursor-chat-sqlite"},
        )
        metadata = {
            "source": "cursor-cli",
            "kind": "message",
            "synthetic": False,
            "provisional": False,
        }
        long_call_id = "call-" + ("i" * 240)
        long_tool_name = "Render" + ("n" * 120)
        recursive = {}
        recursive["self"] = recursive
        records = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": ""},
                    {
                        "type": "tool-call",
                        "toolCallId": "call-empty",
                        "toolName": "Read",
                        "args": "",
                    },
                    {
                        "type": "tool_use",
                        "toolCallId": "call-none",
                        "toolName": "Shell",
                        "args": None,
                    },
                    {
                        "type": "tool-call",
                        "toolCallId": long_call_id,
                        "toolName": long_tool_name,
                        "input": {},
                    },
                ],
                "_cursor_chat": metadata,
            },
            {
                "role": "tool",
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": "call-empty",
                        "toolName": "Read",
                        "result": "",
                    },
                    {"type": "tool-result"},
                    {
                        "type": "tool_result",
                        "toolCallId": "call-none",
                        "toolName": "Shell",
                        "result": None,
                    },
                    {
                        "type": "tool-result",
                        "toolCallId": 42,
                        "toolName": object(),
                        "result": None,
                    },
                    {
                        "type": "tool-result",
                        "toolCallId": "malformed",
                        "toolName": "Broken",
                        "result": recursive,
                    },
                    {
                        "type": "tool-result",
                        "toolCallId": long_call_id,
                        "toolName": long_tool_name,
                        "providerOptions": {"secret": "empty-result-secret"},
                    },
                    {"type": "text", "text": ""},
                ],
                "_cursor_chat": metadata,
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": ""}],
                "_cursor_chat": metadata,
            },
        ]

        events = [
            event
            for record in records
            for event in CursorAdapter().normalize(record, session)
        ]

        self.assertEqual(
            ["tool_call"] * 3 + ["tool_result"] * 3,
            [event.kind for event in events],
        )
        calls = events[:3]
        results = events[3:]
        self.assertEqual(
            [event.extra["tool_call_id"] for event in calls],
            [event.extra["tool_call_id"] for event in results],
        )
        self.assertEqual(
            [event.extra["tool_name"] for event in calls],
            [event.extra["tool_name"] for event in results],
        )
        self.assertEqual(
            ["Read completed", "Shell completed"],
            [event.text for event in results[:2]],
        )
        self.assertTrue(results[2].text.endswith(" completed"))
        self.assertLessEqual(len(results[2].text), 80)
        self.assertLessEqual(len(results[2].extra["tool_call_id"]), 160)
        self.assertLessEqual(len(results[2].extra["tool_name"]), 80)
        serialized = json.dumps([event.to_dict() for event in events])
        self.assertNotIn("providerOptions", serialized)
        self.assertNotIn("empty-result-secret", serialized)
        self.assertNotIn(long_call_id, serialized)
        self.assertNotIn(long_tool_name, serialized)

    def test_provisional_reset_empty_and_malformed_blocks(self):
        session = Session(
            agent="cursor-cli",
            session_id="session-id",
            project="/work",
            transcript=str(self.store),
            updated_at=1_800_000_000.0,
            extra={"transcript_kind": "cursor-chat-sqlite"},
        )
        adapter = CursorAdapter()
        metadata = {
            "source": "cursor-cli",
            "kind": "provisional",
            "synthetic": True,
            "provisional": True,
            "root_blob_id": "r" * 64,
            "timestamp_source": "observed",
        }

        provisional = list(
            adapter.normalize(
                {
                    "type": "cursor_chat_provisional",
                    "role": "assistant",
                    "content": "draft answer",
                    "synthetic": True,
                    "provisional": True,
                    "_cursor_chat": metadata,
                },
                session,
            )
        )
        self.assertEqual(["assistant_update"], [event.kind for event in provisional])
        self.assertTrue(provisional[0].extra["synthetic"])
        self.assertTrue(provisional[0].extra["provisional"])
        self.assertEqual(
            [],
            list(
                adapter.normalize(
                    {
                        "type": "cursor_chat_provisional",
                        "role": "assistant",
                        "content": "",
                        "synthetic": True,
                        "provisional": True,
                        "_cursor_chat": metadata,
                    },
                    session,
                )
            ),
        )

        reset_metadata = dict(metadata, kind="session_reset", provisional=False)
        reset = list(
            adapter.normalize(
                {
                    "type": "session_reset",
                    "role": "system",
                    "content": "Cursor chat history reset",
                    "synthetic": True,
                    "provisional": False,
                    "_cursor_chat": reset_metadata,
                },
                session,
            )
        )
        self.assertEqual(["session_reset"], [event.kind for event in reset])
        self.assertTrue(reset[0].extra["synthetic"])
        self.assertFalse(reset[0].extra["provisional"])

        recursive = {}
        recursive["self"] = recursive
        malformed = list(
            adapter.normalize(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool-call",
                            "toolName": "bad",
                            "args": recursive,
                        },
                        None,
                        {"type": "unknown", "content": object()},
                        {"type": "text", "text": "valid sibling"},
                        {"type": "text", "text": ""},
                    ],
                    "_cursor_chat": dict(
                        metadata,
                        kind="message",
                        synthetic=False,
                        provisional=False,
                    ),
                },
                session,
            )
        )
        self.assertEqual(["assistant"], [event.kind for event in malformed])
        self.assertEqual("valid sibling", malformed[0].text)
        json.dumps(malformed[0].to_dict())

    def test_cursor_ide_normalization_path_is_unchanged(self):
        session = Session(
            agent="cursor-ide",
            session_id="ide",
            project="/work",
            transcript="/tmp/ide.jsonl",
            updated_at=1.0,
            extra={"source": "ide"},
        )
        record = {
            "role": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "same text"},
                    {"type": "tool_use", "name": "Read", "input": {"path": "a"}},
                    {"type": "tool_result", "content": "same result"},
                ]
            },
            "timestamp": 1_800_000_000.0,
        }

        events = list(CursorAdapter().normalize(record, session))

        self.assertEqual(
            ["assistant", "tool_call", "tool_result"],
            [event.kind for event in events],
        )
        self.assertEqual([{}, {}, {}], [event.extra for event in events])


if __name__ == "__main__":
    unittest.main()
