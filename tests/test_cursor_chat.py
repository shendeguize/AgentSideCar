import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sidecar.cursor_chat as cursor_chat
from sidecar.cursor_chat import (
    CursorChatBlobError,
    CursorChatBusyError,
    CursorChatFollower,
    CursorChatLimitError,
    CursorChatMetadataError,
    CursorChatProtobufError,
    CursorChatSchemaError,
    decode_file_uri,
    snapshot_cursor_chat,
)


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


class CursorStore:
    def __init__(self, root, wal=True):
        self.path = root / "store.db"
        self.connection = sqlite3.connect(str(self.path))
        if wal:
            mode = self.connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if mode != "wal":
                raise AssertionError("WAL mode unavailable")
            self.connection.execute("PRAGMA wal_autocheckpoint = 0")
        self.connection.execute(
            "CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)"
        )
        self.connection.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.connection.commit()
        self.latest_root = None
        self.latest_message_ids = ()

    def install(
        self,
        messages,
        provisional=None,
        workspace="file:///Users/example/My%20Project",
        name="Metadata fallback",
        created_at=1_787_430_000_000,
        extra_wire=True,
    ):
        message_ids = []
        for message in messages:
            payload = json_bytes(message)
            blob_id = hashlib.sha256(payload).hexdigest()
            self.connection.execute(
                "INSERT OR IGNORE INTO blobs (id, data) VALUES (?, ?)",
                (blob_id, payload),
            )
            message_ids.append(blob_id)
        root = bytearray()
        if extra_wire:
            root.extend(varint((2 << 3) | 0) + varint(17))
            root.extend(varint((3 << 3) | 1) + b"12345678")
            root.extend(varint((5 << 3) | 5) + b"1234")
        for message_id in message_ids:
            root.extend(wire_bytes(1, bytes.fromhex(message_id)))
        if provisional is not None:
            root.extend(wire_bytes(4, json_bytes(provisional)))
        if workspace is not None:
            root.extend(wire_bytes(9, workspace.encode("utf-8")))
        root_data = bytes(root)
        root_id = hashlib.sha256(root_data).hexdigest()
        self.connection.execute(
            "INSERT OR IGNORE INTO blobs (id, data) VALUES (?, ?)",
            (root_id, root_data),
        )
        metadata = {
            "agentId": "agent-production-shaped",
            "latestRootBlobId": root_id,
            "name": name,
            "mode": "agent",
            "createdAt": created_at,
        }
        self.connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("0", json_bytes(metadata).hex()),
        )
        self.connection.commit()
        self.latest_root = root_id
        self.latest_message_ids = tuple(message_ids)
        return root_id, tuple(message_ids)

    def install_raw_message(self, payload):
        message_id = hashlib.sha256(payload).hexdigest()
        self.connection.execute(
            "INSERT OR IGNORE INTO blobs (id, data) VALUES (?, ?)",
            (message_id, payload),
        )
        root_data = wire_bytes(1, bytes.fromhex(message_id))
        root_id = hashlib.sha256(root_data).hexdigest()
        self.connection.execute(
            "INSERT OR IGNORE INTO blobs (id, data) VALUES (?, ?)",
            (root_id, root_data),
        )
        metadata = {
            "agentId": "agent-production-shaped",
            "latestRootBlobId": root_id,
            "name": "Raw message",
            "mode": "agent",
            "createdAt": 1_787_430_000_000,
        }
        self.connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("0", json_bytes(metadata).hex()),
        )
        self.connection.commit()
        self.latest_root = root_id
        self.latest_message_ids = (message_id,)
        return root_id, message_id

    def checkpoint(self):
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


SYSTEM = {"role": "system", "content": "Generated instructions"}
USER_INFO = {
    "role": "user",
    "content": "<user_info>generated context only</user_info>",
}
USER_QUERY = {
    "role": "user",
    "content": [
        {
            "type": "text",
            "text": "<user_info>context</user_info>\n"
            "<user_query>\nImplement logical Cursor following\n</user_query>",
        }
    ],
}
ASSISTANT = {
    "role": "assistant",
    "content": [{"type": "text", "text": "Working on it"}],
}


class CursorChatSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temporary.cleanup()

    def make_store(self, wal=True):
        directory = self.root / "store-{}".format(len(self.stores))
        directory.mkdir()
        store = CursorStore(directory, wal=wal)
        self.stores.append(store)
        return store

    @staticmethod
    def file_state(paths):
        result = {}
        for path in paths:
            data = path.read_bytes()
            details = path.stat()
            result[path.name] = (
                hashlib.sha256(data).hexdigest(),
                details.st_mtime_ns,
                details.st_size,
            )
        return result

    @staticmethod
    def source_paths(store):
        return tuple(
            path
            for path in (
                store.path,
                Path(str(store.path) + "-wal"),
                Path(str(store.path) + "-shm"),
            )
            if path.exists()
        )

    def test_main_checkpointed_and_wal_only_schema_states(self):
        main = self.make_store(wal=False)
        main.install([USER_QUERY])
        main.close()

        checkpointed = self.make_store()
        checkpointed.install([USER_QUERY, ASSISTANT])
        checkpointed.checkpoint()

        wal_only = self.make_store()
        wal_only.install([SYSTEM, USER_INFO, USER_QUERY])

        self.assertEqual(1, len(snapshot_cursor_chat(main.path).messages))
        self.assertEqual(2, len(snapshot_cursor_chat(checkpointed.path).messages))
        self.assertEqual(3, len(snapshot_cursor_chat(wal_only.path).messages))

    def test_order_metadata_project_title_and_deep_immutability(self):
        store = self.make_store()
        root_id, message_ids = store.install(
            [SYSTEM, USER_INFO, USER_QUERY, ASSISTANT],
            name="Wrong metadata title",
        )

        state = snapshot_cursor_chat(store.path)

        self.assertEqual(root_id, state.root_blob_id)
        self.assertFalse(hasattr(state, "root_id"))
        self.assertEqual(message_ids, state.message_ids)
        self.assertEqual(
            ["system", "user", "user", "assistant"],
            [message["role"] for message in state.messages],
        )
        self.assertEqual("/Users/example/My Project", state.project)
        self.assertEqual("Implement logical Cursor following", state.title)
        self.assertEqual(1_787_430_000.0, state.created_at)
        self.assertEqual("agent-production-shaped", state.metadata.agent_id)
        with self.assertRaises(TypeError):
            state.messages[0]["role"] = "user"
        with self.assertRaises(TypeError):
            state.messages[2]["content"][0]["text"] = "changed"

    def test_fallback_title_skips_generated_user_info(self):
        store = self.make_store()
        store.install(
            [
                SYSTEM,
                USER_INFO,
                {"role": "user", "content": "Fallback actual user prompt"},
            ],
            name="Metadata name",
        )

        self.assertEqual(
            "Fallback actual user prompt",
            snapshot_cursor_chat(store.path).title,
        )

    def test_source_db_wal_and_shm_remain_bit_identical(self):
        store = self.make_store()
        store.install([USER_QUERY], provisional=ASSISTANT)
        store.connection.execute("SELECT count(*) FROM blobs").fetchone()
        paths = [
            store.path,
            Path(str(store.path) + "-wal"),
            Path(str(store.path) + "-shm"),
        ]
        for index, path in enumerate(paths):
            timestamp = 1_700_000_000_000_000_000 + index
            os.utime(path, ns=(timestamp, timestamp))
        before = self.file_state(paths)

        snapshot_cursor_chat(store.path)

        self.assertEqual(before, self.file_state(paths))

    def test_snapshot_retries_a_source_race(self):
        store = self.make_store()
        store.install([USER_QUERY])
        original = cursor_chat._copy_regular_file
        raced = [False]

        def copy_and_race(source, destination, expected_size):
            copied = original(source, destination, expected_size)
            if source == store.path and not raced[0]:
                raced[0] = True
                store.install([USER_QUERY, ASSISTANT], name="After retry")
            return copied

        with mock.patch(
            "sidecar.cursor_chat._copy_regular_file",
            side_effect=copy_and_race,
        ) as copied:
            state = snapshot_cursor_chat(store.path)

        self.assertTrue(raced[0])
        self.assertGreaterEqual(copied.call_count, 3)
        self.assertEqual(2, len(state.messages))
        self.assertEqual("After retry", state.metadata.name)

    def test_repeated_races_raise_busy(self):
        store = self.make_store()
        store.install([USER_QUERY])
        original = cursor_chat._copy_regular_file

        def always_change(source, destination, expected_size):
            copied = original(source, destination, expected_size)
            if source == store.path:
                store.connection.execute(
                    "UPDATE meta SET value = value WHERE key = ?",
                    ("0",),
                )
                store.connection.commit()
                wal = Path(str(store.path) + "-wal")
                now = wal.stat().st_mtime_ns + 1_000_000
                os.utime(wal, ns=(now, now))
            return copied

        with mock.patch(
            "sidecar.cursor_chat._copy_regular_file",
            side_effect=always_change,
        ):
            with self.assertRaises(CursorChatBusyError):
                snapshot_cursor_chat(store.path)

    def test_malformed_metadata_hex_json_and_fields_are_typed(self):
        cases = (
            "not-hex",
            b"{".hex(),
            json_bytes({"agentId": "only-one-field"}).hex(),
        )
        for index, value in enumerate(cases):
            with self.subTest(index=index):
                store = self.make_store()
                store.install([USER_QUERY])
                store.connection.execute(
                    "UPDATE meta SET value = ? WHERE key = ?",
                    (value, "0"),
                )
                store.connection.commit()
                with self.assertRaises(
                    (CursorChatMetadataError, CursorChatLimitError)
                ):
                    snapshot_cursor_chat(store.path)

    def test_created_at_preserves_normal_seconds_and_milliseconds(self):
        store = self.make_store()
        cases = (
            ("integer seconds", 1_787_430_000, 1_787_430_000.0),
            ("string seconds", "1787430000", 1_787_430_000.0),
            ("integer milliseconds", 1_787_430_000_000, 1_787_430_000.0),
            ("string milliseconds", "1787430000000", 1_787_430_000.0),
        )
        for label, created_at, expected in cases:
            with self.subTest(case=label):
                store.install([USER_QUERY], created_at=created_at)
                state = snapshot_cursor_chat(store.path)
                self.assertEqual(expected, state.created_at)
                self.assertEqual(expected, state.metadata.created_at)

    def test_invalid_numeric_created_at_values_are_typed_and_redacted(self):
        store = self.make_store()
        huge = 10**400
        cases = (
            ("huge positive integer", huge, "Cursor chat createdAt is invalid"),
            ("huge negative integer", -huge, "Cursor chat createdAt is invalid"),
            ("oversized positive float", 1e308, "Cursor chat createdAt is invalid"),
            ("oversized negative float", -1e308, "Cursor chat createdAt is invalid"),
            ("string overflow", "1e309", "Cursor chat createdAt is invalid"),
            ("negative string overflow", "-1e309", "Cursor chat createdAt is invalid"),
            ("string NaN", "NaN", "Cursor chat createdAt is invalid"),
            ("string Infinity", "Infinity", "Cursor chat createdAt is invalid"),
            ("string negative Infinity", "-Infinity", "Cursor chat createdAt is invalid"),
            ("JSON NaN", float("nan"), "Cursor chat JSON is malformed"),
            ("JSON Infinity", float("inf"), "Cursor chat JSON is malformed"),
            ("JSON negative Infinity", float("-inf"), "Cursor chat JSON is malformed"),
        )
        for label, created_at, expected_message in cases:
            with self.subTest(case=label):
                store.install([USER_QUERY], created_at=created_at)
                with self.assertRaises(CursorChatMetadataError) as raised:
                    snapshot_cursor_chat(store.path)
                self.assertEqual(expected_message, str(raised.exception))

    def test_json_depth_is_bounded_and_source_remains_unchanged(self):
        nested = "leaf"
        for _index in range(cursor_chat.MAX_JSON_DEPTH + 1):
            nested = [nested]
        with self.assertRaises(CursorChatLimitError):
            cursor_chat._freeze_json(nested)

        store = self.make_store()
        depth = 2_000
        payload = (
            b'{"role":"user","content":'
            + (b"[" * depth)
            + b'"deep"'
            + (b"]" * depth)
            + b"}"
        )
        store.install_raw_message(payload)
        paths = self.source_paths(store)
        before = self.file_state(paths)

        with self.assertRaises(CursorChatLimitError):
            snapshot_cursor_chat(store.path)

        self.assertEqual(before, self.file_state(paths))

    def test_lone_surrogate_strings_and_keys_are_typed_and_readonly(self):
        malformed_messages = (
            b'{"role":"user","content":"\\ud800"}',
            b'{"role":"user","content":"ok","\\udfff":true}',
        )
        for index, payload in enumerate(malformed_messages):
            with self.subTest(message=index):
                store = self.make_store()
                store.install_raw_message(payload)
                paths = self.source_paths(store)
                before = self.file_state(paths)

                with self.assertRaises(CursorChatBlobError):
                    snapshot_cursor_chat(store.path)

                self.assertEqual(before, self.file_state(paths))

        metadata_store = self.make_store()
        metadata_store.install([USER_QUERY])
        raw_metadata = (
            b'{"agentId":"agent","latestRootBlobId":"'
            + metadata_store.latest_root.encode("ascii")
            + b'","name":"\\ud800","mode":"agent","createdAt":1}'
        )
        metadata_store.connection.execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            (raw_metadata.hex(), "0"),
        )
        metadata_store.connection.commit()
        paths = self.source_paths(metadata_store)
        before = self.file_state(paths)

        with self.assertRaises(CursorChatMetadataError):
            snapshot_cursor_chat(metadata_store.path)

        self.assertEqual(before, self.file_state(paths))

    def test_valid_multilingual_emoji_and_control_whitespace_survive(self):
        store = self.make_store()
        content = "你好 👩‍💻\n\t\r"
        store.install(
            [
                {
                    "role": "user",
                    "content": content,
                    "扩展": {"键": "值"},
                }
            ]
        )

        message = snapshot_cursor_chat(store.path).messages[0]

        self.assertEqual(content, message["content"])
        self.assertEqual("值", message["扩展"]["键"])

    def test_root_and_message_hashes_are_verified(self):
        store = self.make_store()
        store.install([USER_QUERY])
        bad_root = "0" * 64
        store.connection.execute(
            "INSERT INTO blobs (id, data) VALUES (?, ?)",
            (bad_root, b"not the matching root"),
        )
        metadata = {
            "agentId": "agent",
            "latestRootBlobId": bad_root,
            "name": "",
            "mode": "agent",
            "createdAt": 1,
        }
        store.connection.execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            (json_bytes(metadata).hex(), "0"),
        )
        store.connection.commit()

        with self.assertRaises(CursorChatBlobError):
            snapshot_cursor_chat(store.path)

        message_id = "1" * 64
        root_data = wire_bytes(1, bytes.fromhex(message_id))
        root_id = hashlib.sha256(root_data).hexdigest()
        store.connection.execute(
            "INSERT INTO blobs (id, data) VALUES (?, ?)",
            (message_id, b'{"role":"user","content":"wrong hash"}'),
        )
        store.connection.execute(
            "INSERT INTO blobs (id, data) VALUES (?, ?)",
            (root_id, root_data),
        )
        metadata["latestRootBlobId"] = root_id
        store.connection.execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            (json_bytes(metadata).hex(), "0"),
        )
        store.connection.commit()
        with self.assertRaises(CursorChatBlobError):
            snapshot_cursor_chat(store.path)

    def test_malformed_protobuf_cases_are_rejected(self):
        malformed = (
            b"\x80",
            varint((1 << 3) | 2) + varint(40) + b"short",
            varint((1 << 3) | 3),
            varint((1 << 3) | 0) + b"\x01",
            b"\xff\xff\xff\xff\xff\xff\xff\xff\xff\x02",
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                with self.assertRaises(CursorChatProtobufError):
                    cursor_chat._scan_root(payload)

    def test_schema_runtime_types_and_source_size_are_bounded(self):
        directory = self.root / "bad-schema"
        directory.mkdir()
        path = directory / "store.db"
        connection = sqlite3.connect(str(path))
        connection.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data TEXT)")
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.commit()
        connection.close()
        with self.assertRaises(CursorChatSchemaError):
            snapshot_cursor_chat(path)

        store = self.make_store(wal=False)
        store.install([USER_QUERY])
        store.close()
        with mock.patch("sidecar.cursor_chat.MAX_DB_BYTES", 1):
            with self.assertRaises(CursorChatLimitError):
                snapshot_cursor_chat(store.path)

    def test_reference_and_content_bounds(self):
        digest = bytes.fromhex("2" * 64)
        with mock.patch("sidecar.cursor_chat.MAX_MESSAGE_REFERENCES", 1):
            with self.assertRaises(CursorChatLimitError):
                cursor_chat._scan_root(wire_bytes(1, digest) * 2)

        with mock.patch("sidecar.cursor_chat.MAX_JSON_NODES", 3):
            with self.assertRaises(CursorChatLimitError):
                cursor_chat._strict_json(
                    b'{"role":"user","content":"bounded"}',
                    CursorChatBlobError,
                )

        store = self.make_store()
        store.install(
            [
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
                }
            ]
        )
        with mock.patch("sidecar.cursor_chat.MAX_CONTENT_BLOCKS", 1):
            with self.assertRaises(CursorChatLimitError):
                snapshot_cursor_chat(store.path)

    def test_file_uri_rejects_remote_invalid_and_control_paths(self):
        self.assertEqual("/tmp/a b", decode_file_uri(b"file:///tmp/a%20b"))
        for value in (
            b"https://example.test/path",
            b"file://remote/path",
            b"file:///tmp/%ZZ",
            b"file:///tmp/%00bad",
        ):
            with self.subTest(value=value):
                with self.assertRaises(CursorChatProtobufError):
                    decode_file_uri(value)


class CursorChatFollowerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = CursorStore(self.root)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def test_baseline_then_append_emits_only_new_suffix(self):
        self.store.install([USER_QUERY])
        follower = CursorChatFollower(
            self.store.path,
            clock=lambda: 1_800_000_000.0,
        )
        self.assertEqual([], follower.poll())

        self.store.install([USER_QUERY, ASSISTANT])
        records = follower.poll()

        self.assertEqual(1, len(records))
        self.assertEqual("assistant", records[0]["role"])
        self.assertEqual("observed", records[0]["_cursor_chat"]["timestamp_source"])
        self.assertFalse(records[0]["synthetic"])
        self.assertFalse(follower.has_pending_records)

    def test_unchanged_polls_skip_snapshot_but_db_and_wal_changes_invalidate(self):
        self.store.install([USER_QUERY])
        follower = CursorChatFollower(self.store.path)
        original = cursor_chat.snapshot_cursor_chat

        with mock.patch(
            "sidecar.cursor_chat.snapshot_cursor_chat",
            wraps=original,
        ) as snapshots:
            self.assertEqual([], follower.poll())
            self.assertEqual([], follower.poll())
            self.assertEqual([], follower.poll())
            self.assertEqual(1, snapshots.call_count)

            shm = Path(str(self.store.path) + "-shm")
            shm_details = shm.stat()
            os.utime(
                shm,
                ns=(shm_details.st_atime_ns, shm_details.st_mtime_ns + 1_000_000),
            )
            self.assertEqual([], follower.poll())
            self.assertEqual(1, snapshots.call_count)

            wal = Path(str(self.store.path) + "-wal")
            wal_details = wal.stat()
            os.utime(
                wal,
                ns=(wal_details.st_atime_ns, wal_details.st_mtime_ns + 1_000_000),
            )
            self.assertEqual([], follower.poll())
            self.assertEqual(2, snapshots.call_count)

            db_details = self.store.path.stat()
            os.utime(
                self.store.path,
                ns=(db_details.st_atime_ns, db_details.st_mtime_ns + 1_000_000),
            )
            self.assertEqual([], follower.poll())
            self.assertEqual(3, snapshots.call_count)

    def test_snapshot_race_forces_the_next_poll(self):
        self.store.install([USER_QUERY])
        follower = CursorChatFollower(self.store.path)
        original = cursor_chat.snapshot_cursor_chat
        raced = [False]

        def snapshot_then_race(path, **kwargs):
            state = original(path, **kwargs)
            if not raced[0]:
                raced[0] = True
                wal = Path(str(self.store.path) + "-wal")
                details = wal.stat()
                os.utime(
                    wal,
                    ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000),
                )
            return state

        with mock.patch(
            "sidecar.cursor_chat.snapshot_cursor_chat",
            side_effect=snapshot_then_race,
        ) as snapshots:
            self.assertEqual([], follower.poll())
            self.assertIsNone(follower._last_source_signature)
            self.assertEqual([], follower.poll())
            self.assertEqual([], follower.poll())

        self.assertTrue(raced[0])
        self.assertEqual(2, snapshots.call_count)

    def test_wal_appearance_replacement_and_removal_invalidate(self):
        self.store.install([USER_QUERY])
        self.store.close()
        state = snapshot_cursor_chat(self.store.path)
        wal = Path(str(self.store.path) + "-wal")
        if wal.exists():
            wal.unlink()
        follower = CursorChatFollower(self.store.path)

        with mock.patch(
            "sidecar.cursor_chat.snapshot_cursor_chat",
            return_value=state,
        ) as snapshots:
            self.assertEqual([], follower.poll())
            self.assertEqual([], follower.poll())
            self.assertEqual(1, snapshots.call_count)

            wal.write_bytes(b"")
            self.assertEqual([], follower.poll())
            self.assertEqual([], follower.poll())
            self.assertEqual(2, snapshots.call_count)

            replacement = self.root / "replacement-wal"
            replacement.write_bytes(b"")
            os.replace(str(replacement), str(wal))
            self.assertEqual([], follower.poll())
            self.assertEqual([], follower.poll())
            self.assertEqual(3, snapshots.call_count)

            wal.unlink()
            self.assertEqual([], follower.poll())
            self.assertEqual([], follower.poll())
            self.assertEqual(4, snapshots.call_count)

    def test_pending_pages_continue_to_snapshot_until_drained(self):
        self.store.install(
            [
                USER_QUERY,
                {"role": "assistant", "content": "one"},
                {"role": "assistant", "content": "two"},
            ]
        )
        follower = CursorChatFollower(
            self.store.path,
            from_start=True,
            max_records=1,
        )
        original = cursor_chat.snapshot_cursor_chat

        with mock.patch(
            "sidecar.cursor_chat.snapshot_cursor_chat",
            wraps=original,
        ) as snapshots:
            records = []
            records.extend(follower.poll())
            records.extend(follower.poll())
            records.extend(follower.poll())
            self.assertFalse(follower.has_pending_records)
            self.assertEqual([], follower.poll())

        self.assertEqual(3, len(records))
        self.assertEqual(3, snapshots.call_count)

    def test_from_start_pages_history_with_session_created_timestamps(self):
        messages = [
            USER_QUERY,
            {"role": "assistant", "content": "one"},
            {"role": "assistant", "content": "two"},
        ]
        self.store.install(messages, created_at=1_700_000_000_000)
        follower = CursorChatFollower(
            self.store.path,
            from_start=True,
            max_records=1,
            clock=lambda: 1_800_000_000.0,
        )

        records = []
        while True:
            records.extend(follower.poll())
            if not follower.has_pending_records:
                break

        self.assertEqual(3, len(records))
        self.assertEqual(
            ["session_created"] * 3,
            [record["_cursor_chat"]["timestamp_source"] for record in records],
        )
        self.assertEqual(
            [1_700_000_000.0] * 3,
            [record["timestamp"] for record in records],
        )

    def test_provisional_updates_then_durable_append_and_clear(self):
        self.store.install([USER_QUERY])
        follower = CursorChatFollower(
            self.store.path,
            clock=lambda: 1_800_000_000.0,
        )
        follower.poll()
        provisional = {"role": "assistant", "content": "draft answer"}
        self.store.install([USER_QUERY], provisional=provisional)

        update = follower.poll()

        self.assertEqual(1, len(update))
        self.assertTrue(update[0]["synthetic"])
        self.assertTrue(update[0]["provisional"])
        self.assertEqual("updated", update[0]["provisional_state"])

        self.store.install([USER_QUERY, provisional])
        durable_and_clear = follower.poll()
        self.assertEqual(
            [False, True],
            [record["synthetic"] for record in durable_and_clear],
        )
        self.assertEqual("cleared", durable_and_clear[1]["provisional_state"])

    def test_checkpoint_restore_emits_inactive_append(self):
        self.store.install([USER_QUERY])
        original = CursorChatFollower(self.store.path)
        original.poll()
        checkpoint = original.export_checkpoint()
        self.store.install([USER_QUERY, ASSISTANT])

        resumed = CursorChatFollower(self.store.path)
        self.assertTrue(resumed.restore_checkpoint(checkpoint))

        self.assertEqual(
            ["assistant"],
            [record["role"] for record in resumed.poll()],
        )

    def test_resolved_reorder_uses_longest_common_prefix(self):
        first = {"role": "user", "content": "first"}
        old = {"role": "assistant", "content": "old"}
        replacement = {"role": "assistant", "content": "replacement"}
        self.store.install([first, old])
        follower = CursorChatFollower(self.store.path)
        follower.poll()
        self.store.install([first, replacement])

        records = follower.poll()

        self.assertEqual(["replacement"], [record["content"] for record in records])
        self.assertNotEqual("session_reset", records[0]["type"] if "type" in records[0] else "")

    def test_previous_root_resolves_even_if_divergent_message_was_pruned(self):
        first = {"role": "user", "content": "first"}
        old = {"role": "assistant", "content": "old"}
        replacement = {"role": "assistant", "content": "replacement"}
        _old_root, old_ids = self.store.install([first, old])
        original = CursorChatFollower(self.store.path)
        original.poll()
        checkpoint = original.export_checkpoint()
        self.store.install([first, replacement])
        self.store.connection.execute(
            "DELETE FROM blobs WHERE id = ?",
            (old_ids[1],),
        )
        self.store.connection.commit()

        resumed = CursorChatFollower(self.store.path)
        self.assertTrue(resumed.restore_checkpoint(checkpoint))

        self.assertEqual(
            ["replacement"],
            [record["content"] for record in resumed.poll()],
        )

    def test_unresolvable_previous_root_emits_one_reset_without_replay(self):
        first = {"role": "user", "content": "first"}
        old = {"role": "assistant", "content": "old"}
        replacement = {"role": "assistant", "content": "replacement"}
        old_root, _ids = self.store.install([first, old])
        original = CursorChatFollower(self.store.path)
        original.poll()
        checkpoint = original.export_checkpoint()
        self.store.install([replacement])
        self.store.connection.execute("DELETE FROM blobs WHERE id = ?", (old_root,))
        self.store.connection.commit()

        resumed = CursorChatFollower(self.store.path)
        self.assertTrue(resumed.restore_checkpoint(checkpoint))
        reset = resumed.poll()

        self.assertEqual(1, len(reset))
        self.assertEqual("session_reset", reset[0]["type"])
        self.assertTrue(reset[0]["synthetic"])
        self.assertEqual([], resumed.poll())

    def test_busy_poll_preserves_logical_checkpoint(self):
        self.store.install([USER_QUERY, ASSISTANT])
        follower = CursorChatFollower(
            self.store.path,
            from_start=True,
            max_records=1,
        )
        self.assertEqual(1, len(follower.poll()))
        self.assertTrue(follower.has_pending_records)
        checkpoint = follower.export_checkpoint()

        with mock.patch(
            "sidecar.cursor_chat.snapshot_cursor_chat",
            side_effect=CursorChatBusyError("busy"),
        ):
            self.assertEqual([], follower.poll())

        self.assertEqual(checkpoint, follower.export_checkpoint())
        self.assertTrue(follower.has_pending_records)
        self.assertIsInstance(follower.last_error, CursorChatBusyError)

    def test_checkpoint_and_pending_bounds(self):
        self.store.install([USER_QUERY, ASSISTANT])
        follower = CursorChatFollower(
            self.store.path,
            from_start=True,
            max_records=1,
        )
        self.assertEqual(1, len(follower.poll()))
        self.assertTrue(follower.has_pending_records)
        checkpoint = follower.export_checkpoint()
        self.assertLess(
            len(json.dumps(checkpoint).encode("utf-8")),
            cursor_chat.MAX_CHECKPOINT_BYTES,
        )

        invalid = dict(checkpoint)
        invalid["message_count"] = cursor_chat.MAX_MESSAGE_REFERENCES + 1
        untouched = follower.export_checkpoint()
        self.assertFalse(follower.restore_checkpoint(invalid))
        self.assertEqual(untouched, follower.export_checkpoint())
        with self.assertRaises(ValueError):
            CursorChatFollower(
                self.store.path,
                max_records=cursor_chat.MAX_PENDING_RECORDS + 1,
            )


if __name__ == "__main__":
    unittest.main()
