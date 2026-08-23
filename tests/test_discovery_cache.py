import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import sidecar.adapters.claude as claude_module
import sidecar.adapters.codex as codex_module
import sidecar.adapters.cursor as cursor_module
from sidecar.adapters.base import file_signature
from sidecar.adapters.claude import ClaudeAdapter
from sidecar.adapters.codex import CodexAdapter
from sidecar.adapters.cursor import CursorAdapter
from sidecar.cursor_chat import CursorChatSchemaError


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def force_new_signature(path, previous_mtime_ns):
    current = path.stat().st_mtime_ns
    changed = max(current, previous_mtime_ns + 1_000_000)
    os.utime(path, ns=(changed, changed))


def varint(value):
    encoded = bytearray()
    while value >= 0x80:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def wire_bytes(field, payload):
    return varint((field << 3) | 2) + varint(len(payload)) + payload


def install_production_state(
    connection,
    messages,
    *,
    workspace="file:///work/cursor-cli",
):
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
    root_data = b"".join(
        wire_bytes(1, bytes.fromhex(message_id)) for message_id in message_ids
    ) + wire_bytes(9, workspace.encode("utf-8"))
    root_id = hashlib.sha256(root_data).hexdigest()
    connection.execute(
        "INSERT OR IGNORE INTO blobs (id, data) VALUES (?, ?)",
        (root_id, root_data),
    )
    metadata = {
        "agentId": "production-agent",
        "latestRootBlobId": root_id,
        "name": "metadata fallback",
        "mode": "agent",
        "createdAt": 1_787_430_000_000,
    }
    encoded_metadata = (
        json.dumps(
            metadata,
            separators=(",", ":"),
        )
        .encode("utf-8")
        .hex()
    )
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("0", encoded_metadata),
    )
    connection.commit()


class DiscoveryMetadataCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def cursor_transcript(self):
        return write_jsonl(
            self.home
            / ".cursor"
            / "projects"
            / "project"
            / "agent-transcripts"
            / "cursor-session.jsonl",
            [{"role": "user", "content": "<user_query>Cursor title</user_query>"}],
        )

    def claude_transcript(self, name="claude-session.jsonl", title="Claude title"):
        return write_jsonl(
            self.home / ".claude" / "projects" / "project" / name,
            [
                {
                    "type": "user",
                    "sessionId": Path(name).stem,
                    "cwd": "/work/claude",
                    "message": {"content": title},
                }
            ],
        )

    def codex_transcript(self):
        return write_jsonl(
            self.home
            / ".codex"
            / "sessions"
            / "2026"
            / "08"
            / "23"
            / "rollout-2026-08-23T00-00-00-codex-session.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "codex-session",
                        "cwd": "/work/codex",
                        "model": "gpt-test",
                    },
                },
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Codex title"},
                },
            ],
        )

    def test_file_signature_accepts_regular_file(self):
        transcript = self.claude_transcript()

        signature = file_signature(transcript)

        self.assertIsNotNone(signature)
        assert signature is not None
        self.assertEqual(os.path.abspath(os.fsdecode(transcript)), signature[0])
        self.assertEqual(transcript.stat().st_size, signature[2])

    def test_file_signature_accepts_symlink_to_regular_file(self):
        transcript = self.claude_transcript()
        symlink = transcript.with_name("linked.jsonl")
        try:
            symlink.symlink_to(transcript)
        except (NotImplementedError, OSError) as error:
            self.skipTest("regular-file symlinks unavailable: {}".format(error))

        target_signature = file_signature(transcript)
        symlink_signature = file_signature(symlink)

        self.assertIsNotNone(target_signature)
        self.assertIsNotNone(symlink_signature)
        assert target_signature is not None
        assert symlink_signature is not None
        self.assertEqual(target_signature[1:], symlink_signature[1:])
        self.assertEqual(str(symlink.absolute()), symlink_signature[0])

    def test_file_signature_rejects_non_regular_paths_and_stat_errors(self):
        directory = self.home / "session.jsonl"
        directory.mkdir()

        self.assertIsNone(file_signature(directory))
        self.assertIsNone(file_signature(os.devnull))
        with mock.patch(
            "sidecar.adapters.base.os.stat",
            side_effect=PermissionError("denied"),
        ):
            self.assertIsNone(file_signature(directory))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO support required")
    def test_claude_discovery_rejects_fifo_without_blocking(self):
        fifo = self.home / ".claude" / "projects" / "project" / "blocked.jsonl"
        fifo.parent.mkdir(parents=True)
        os.mkfifo(fifo)
        outcome = []
        errors = []

        def discover():
            try:
                outcome.append(list(ClaudeAdapter().discover(self.home)))
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=discover, daemon=True)
        worker.start()
        worker.join(1.0)
        blocked = worker.is_alive()
        if blocked:
            rescue = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            os.close(rescue)
            worker.join(1.0)

        self.assertFalse(worker.is_alive(), "failed to release blocked FIFO reader")
        self.assertFalse(blocked, "discovery attempted to open a matching FIFO")
        if errors:
            raise errors[0]
        self.assertEqual([[]], outcome)

    def test_unchanged_jsonl_sources_parse_once_per_adapter(self):
        self.cursor_transcript()
        self.claude_transcript()
        self.codex_transcript()
        cursor = CursorAdapter()
        claude = ClaudeAdapter()
        codex = CodexAdapter()

        with (
            mock.patch.object(
                cursor_module,
                "_first_cursor_user_text",
                wraps=cursor_module._first_cursor_user_text,
            ) as cursor_parser,
            mock.patch.object(
                claude_module,
                "_claude_metadata",
                wraps=claude_module._claude_metadata,
            ) as claude_parser,
            mock.patch.object(
                codex_module,
                "_codex_metadata",
                wraps=codex_module._codex_metadata,
            ) as codex_parser,
        ):
            for adapter in (cursor, claude, codex):
                list(adapter.discover(self.home))
                list(adapter.discover(self.home))

        self.assertEqual(1, cursor_parser.call_count)
        self.assertEqual(1, claude_parser.call_count)
        self.assertEqual(1, codex_parser.call_count)

    def test_default_capacity_keeps_more_than_256_cursor_sources_warm(self):
        root = self.home / ".cursor" / "projects" / "project" / "agent-transcripts"
        for index in range(300):
            write_jsonl(
                root / "{}.jsonl".format(index),
                [{"role": "user", "content": "title {}".format(index)}],
            )
        adapter = CursorAdapter()

        with mock.patch.object(
            cursor_module,
            "_first_cursor_user_text",
            wraps=cursor_module._first_cursor_user_text,
        ) as parser:
            self.assertEqual(300, len(list(adapter.discover(self.home))))
            self.assertEqual(300, len(list(adapter.discover(self.home))))

        self.assertEqual(300, parser.call_count)
        self.assertEqual(300, len(adapter._metadata_cache))

    def test_append_replacement_and_truncation_reparse_only_changed_source(self):
        first = self.claude_transcript("first.jsonl", "first title")
        second = self.claude_transcript("second.jsonl", "second title")
        adapter = ClaudeAdapter()

        with mock.patch.object(
            claude_module,
            "_claude_metadata",
            wraps=claude_module._claude_metadata,
        ) as parser:
            self.assertEqual(2, len(list(adapter.discover(self.home))))
            self.assertEqual(2, len(list(adapter.discover(self.home))))
            self.assertEqual(2, parser.call_count)

            previous = first.stat().st_mtime_ns
            write_jsonl(
                first,
                [
                    {
                        "type": "user",
                        "sessionId": "first",
                        "cwd": "/replacement",
                        "message": {"content": "replacement title"},
                    }
                ],
            )
            force_new_signature(first, previous)
            replaced = list(adapter.discover(self.home))
            self.assertEqual(3, parser.call_count)
            self.assertIn("replacement title", {item.title for item in replaced})

            previous = first.stat().st_mtime_ns
            with first.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"type": "ai-title", "aiTitle": "appended title"}) + "\n"
                )
            force_new_signature(first, previous)
            appended = list(adapter.discover(self.home))
            self.assertEqual(4, parser.call_count)
            self.assertIn("appended title", {item.title for item in appended})

            previous = second.stat().st_mtime_ns
            second.write_text("", encoding="utf-8")
            force_new_signature(second, previous)
            truncated = list(adapter.discover(self.home))
            self.assertEqual(5, parser.call_count)
            self.assertIn("", {item.title for item in truncated})

    def test_deleted_sources_are_pruned_and_cache_stays_bounded(self):
        first = self.claude_transcript("first.jsonl", "first")
        second = self.claude_transcript("second.jsonl", "second")
        adapter = ClaudeAdapter(metadata_cache_size=2)

        list(adapter.discover(self.home))
        self.assertEqual(2, len(adapter._metadata_cache))

        first.unlink()
        second.unlink()
        list(adapter.discover(self.home))
        self.assertEqual(0, len(adapter._metadata_cache))

        for index in range(3):
            self.claude_transcript("{}.jsonl".format(index), str(index))
        list(adapter.discover(self.home))
        self.assertEqual(2, len(adapter._metadata_cache))

    def test_cursor_cli_wal_change_invalidates_cached_metadata(self):
        store = self.home / ".cursor" / "chats" / "cwd-hash" / "session-id" / "store.db"
        store.parent.mkdir(parents=True)
        writer = sqlite3.connect(str(store))
        self.addCleanup(writer.close)
        self.assertEqual(
            "wal", writer.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        )
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB)")
        writer.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            (("name", "checkpointed CLI title"), ("cwd", "/work/cursor-cli")),
        )
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            ("first WAL-only title", "name"),
        )
        writer.commit()
        adapter = CursorAdapter()

        with (
            mock.patch.object(
                cursor_module,
                "snapshot_cursor_chat",
                wraps=cursor_module.snapshot_cursor_chat,
            ) as snapshot_parser,
            mock.patch.object(
                cursor_module,
                "_read_cli_meta",
                wraps=cursor_module._read_cli_meta,
            ) as parser,
        ):
            first = list(adapter.discover(self.home))[0]
            unchanged = list(adapter.discover(self.home))[0]
            self.assertEqual(1, snapshot_parser.call_count)
            self.assertEqual(1, parser.call_count)
            self.assertEqual("checkpointed CLI title", first.title)
            self.assertEqual(first.title, unchanged.title)

            wal = Path(str(store) + "-wal")
            previous = wal.stat().st_mtime_ns
            writer.execute(
                "UPDATE meta SET value = ? WHERE key = ?",
                ("updated CLI title", "name"),
            )
            writer.commit()
            force_new_signature(wal, previous)

            changed = list(adapter.discover(self.home))[0]

        self.assertEqual(2, snapshot_parser.call_count)
        self.assertEqual(2, parser.call_count)
        self.assertEqual("checkpointed CLI title", changed.title)
        self.assertEqual(
            "updated CLI title",
            writer.execute("SELECT value FROM meta WHERE key = 'name'").fetchone()[0],
        )

    def test_production_cursor_snapshot_cache_and_wal_invalidation(self):
        store = (
            self.home
            / ".cursor"
            / "chats"
            / "cwd-hash"
            / "directory-session-id"
            / "store.db"
        )
        store.parent.mkdir(parents=True)
        writer = sqlite3.connect(str(store))
        self.addCleanup(writer.close)
        self.assertEqual(
            "wal", writer.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        )
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        writer.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        install_production_state(
            writer,
            [
                {
                    "role": "user",
                    "content": "<user_query>First production title</user_query>",
                }
            ],
        )
        adapter = CursorAdapter()

        with (
            mock.patch.object(
                cursor_module,
                "snapshot_cursor_chat",
                wraps=cursor_module.snapshot_cursor_chat,
            ) as decoder,
            mock.patch.object(
                cursor_module,
                "_read_cli_meta",
                side_effect=AssertionError("production store must not use fallback"),
            ) as fallback,
        ):
            first = list(adapter.discover(self.home))[0]
            unchanged = list(adapter.discover(self.home))[0]
            self.assertEqual(1, decoder.call_count)
            self.assertEqual(0, fallback.call_count)
            self.assertEqual("First production title", first.title)
            self.assertEqual(first.title, unchanged.title)

            wal = Path(str(store) + "-wal")
            previous = wal.stat().st_mtime_ns
            install_production_state(
                writer,
                [
                    {
                        "role": "user",
                        "content": "<user_query>Updated production title</user_query>",
                    }
                ],
            )
            force_new_signature(wal, previous)

            changed = list(adapter.discover(self.home))[0]

        self.assertEqual(2, decoder.call_count)
        self.assertEqual(0, fallback.call_count)
        self.assertEqual("Updated production title", changed.title)

    def test_typed_cache_failure_uses_immutable_fallback_once(self):
        store = self.home / ".cursor" / "chats" / "cwd-hash" / "session-id" / "store.db"
        store.parent.mkdir(parents=True)
        writer = sqlite3.connect(str(store))
        self.addCleanup(writer.close)
        writer.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        writer.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            (("name", "fallback title"), ("cwd", "/fallback/project")),
        )
        writer.commit()
        adapter = CursorAdapter()

        with (
            mock.patch.object(
                cursor_module,
                "snapshot_cursor_chat",
                side_effect=CursorChatSchemaError("private source text"),
            ) as decoder,
            mock.patch.object(
                cursor_module,
                "_read_cli_meta",
                wraps=cursor_module._read_cli_meta,
            ) as fallback,
        ):
            first = list(adapter.discover(self.home))[0]
            second = list(adapter.discover(self.home))[0]

        self.assertEqual(1, decoder.call_count)
        self.assertEqual(1, fallback.call_count)
        self.assertEqual("fallback title", first.title)
        self.assertEqual(first.title, second.title)
        self.assertNotIn("private source text", json.dumps(first.extra))


if __name__ == "__main__":
    unittest.main()
