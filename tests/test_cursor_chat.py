import copy
import hashlib
import getpass
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import sidecar.cursor_chat as cursor_chat
from sidecar import remote
from sidecar.adapters.cursor import _first_cursor_user_text
from sidecar.cursor_chat import (
    CursorChatBlobError,
    CursorChatBusyError,
    CursorChatFollower,
    CursorChatLimitError,
    CursorChatMetadataError,
    CursorChatOpenError,
    CursorChatProtobufError,
    CursorChatSchemaError,
    CursorChatSnapshotBroker,
    CursorChatSourceError,
    decode_file_uri,
    snapshot_cursor_chat,
)
from sidecar.json_limits import (
    JSONLimitError,
    JSONLimits,
    JSONSyntaxError,
    parse_json,
)
from sidecar.text_utils import (
    CURSOR_TITLE_LIMIT,
    CURSOR_TITLE_MAX_INPUT_CHARS,
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


CURSOR_CLI_GOLDEN = (
    Path(__file__).parent
    / "fixtures"
    / "cursor_cli_store_3_16_17_golden.json"
)


def golden_wire_fields(data):
    fields = []
    position = 0
    while position < len(data):
        key, position = cursor_chat._read_varint(data, position)
        number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, position = cursor_chat._read_varint(data, position)
        elif wire_type == 1:
            value = data[position : position + 8]
            position += 8
        elif wire_type == 2:
            length, position = cursor_chat._read_varint(data, position)
            value = data[position : position + length]
            position += length
        elif wire_type == 5:
            value = data[position : position + 4]
            position += 4
        else:
            raise AssertionError("unsupported golden wire type")
        if position > len(data):
            raise AssertionError("truncated golden wire field")
        fields.append((number, wire_type, value))
    return fields


def golden_wire_shape(data):
    return [
        {"field": number, "wire_type": wire_type}
        for number, wire_type, _value in golden_wire_fields(data)
    ]


def golden_json_structure(value):
    if isinstance(value, dict):
        return {key: golden_json_structure(item) for key, item in value.items()}
    if isinstance(value, list):
        return [golden_json_structure(item) for item in value]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "string"


def golden_json_strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [
            item
            for nested in value
            for item in golden_json_strings(nested)
        ]
    if isinstance(value, dict):
        return [
            item
            for nested in value.values()
            for item in golden_json_strings(nested)
        ]
    return []


def golden_json_numbers(value):
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, list):
        return [
            item
            for nested in value
            for item in golden_json_numbers(nested)
        ]
    if isinstance(value, dict):
        return [
            item
            for nested in value.values()
            for item in golden_json_numbers(nested)
        ]
    return []


def golden_thaw(value):
    if hasattr(value, "items"):
        return {
            str(key): golden_thaw(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [golden_thaw(item) for item in value]
    return value


def golden_decoded_state(state):
    return {
        "metadata": {
            "agent_id": state.metadata.agent_id,
            "latest_root_blob_id": state.metadata.latest_root_blob_id,
            "name": state.metadata.name,
            "mode": state.metadata.mode,
            "created_at": state.metadata.created_at,
        },
        "root_blob_id": state.root_blob_id,
        "message_ids": list(state.message_ids),
        "messages": golden_thaw(state.messages),
        "provisional": golden_thaw(state.provisional),
        "provisional_hash": state.provisional_hash,
        "project": state.project,
        "created_at": state.created_at,
        "title": state.title,
    }


def reconstruct_golden_store(root, fixture, filename):
    database = root / filename
    connection = sqlite3.connect(str(database))
    try:
        for table in fixture["provenance"]["sqlite_schema"]:
            connection.execute(table["sql"])
        for key_hex, value_hex in fixture["kv"]["meta"]:
            connection.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                (
                    bytes.fromhex(key_hex).decode("utf-8"),
                    bytes.fromhex(value_hex).decode("utf-8"),
                ),
            )
        for blob_id_hex, data_hex in fixture["kv"]["blobs"]:
            connection.execute(
                "INSERT INTO blobs (id, data) VALUES (?, ?)",
                (
                    bytes.fromhex(blob_id_hex).decode("ascii"),
                    bytes.fromhex(data_hex),
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return database


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


class SharedBoundedJSONTests(unittest.TestCase):
    def test_syntax_cases_keep_neutral_and_caller_specific_types(self):
        payloads = (
            b'{"key":1,"key":2}',
            b'{"value":NaN}',
            b'{"value":"\\ud800"}',
        )

        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(JSONSyntaxError):
                    parse_json(payload, JSONLimits(max_bytes=len(payload)))
                with self.assertRaises(ValueError) as remote_error:
                    remote.parse_bounded_json(payload, max_bytes=len(payload))
                self.assertNotIsInstance(
                    remote_error.exception,
                    remote.ProtocolResourceLimitError,
                )
                with self.assertRaises(CursorChatMetadataError):
                    cursor_chat._strict_json(payload, CursorChatMetadataError)
                with self.assertRaises(CursorChatBlobError):
                    cursor_chat._strict_json(payload, CursorChatBlobError)

    def test_depth_nodes_and_recursion_keep_resource_limit_types(self):
        deeply_nested = ("[" * 5000 + "0" + "]" * 5000).encode("ascii")
        with self.assertRaises(JSONLimitError):
            parse_json(
                deeply_nested,
                JSONLimits(
                    max_bytes=len(deeply_nested),
                    max_depth=cursor_chat.MAX_JSON_DEPTH,
                ),
            )
        with self.assertRaises(remote.ProtocolResourceLimitError):
            remote.parse_bounded_json(
                deeply_nested,
                max_bytes=len(deeply_nested),
            )
        with self.assertRaises(CursorChatLimitError):
            cursor_chat._strict_json(deeply_nested, CursorChatBlobError)

        node_payload = b"[null,null,null]"
        with self.assertRaises(JSONLimitError):
            parse_json(
                node_payload,
                JSONLimits(max_bytes=len(node_payload), max_nodes=3),
            )
        with mock.patch.object(remote, "MAX_JSON_ITEMS", 3):
            with self.assertRaises(remote.ProtocolResourceLimitError):
                remote.parse_bounded_json(
                    node_payload,
                    max_bytes=len(node_payload),
                )
        with mock.patch.object(cursor_chat, "MAX_JSON_NODES", 3):
            with self.assertRaises(CursorChatLimitError):
                cursor_chat._strict_json(node_payload, CursorChatBlobError)

    def test_old_private_json_helpers_are_removed(self):
        self.assertFalse(hasattr(remote, "_duplicate_checked_object"))
        self.assertFalse(hasattr(remote, "_reject_json_constant"))
        self.assertFalse(hasattr(remote, "_validate_json_value"))
        self.assertFalse(hasattr(cursor_chat, "_DuplicateJSONKey"))
        self.assertFalse(hasattr(cursor_chat, "_json_object"))
        self.assertFalse(hasattr(cursor_chat, "_reject_json_constant"))
        self.assertFalse(hasattr(cursor_chat, "_validate_json_text"))
        self.assertFalse(hasattr(cursor_chat, "_validate_json_tree"))


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

    def test_checkpointed_wal_without_sidecars_is_readable(self):
        store = self.make_store()
        store.install([SYSTEM, USER_QUERY, ASSISTANT])
        expected = golden_decoded_state(snapshot_cursor_chat(store.path))
        store.checkpoint()
        store.close()

        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(store.path) + suffix)
            if sidecar.exists():
                sidecar.unlink()

        self.assertEqual(expected, golden_decoded_state(snapshot_cursor_chat(store.path)))

    def test_private_snapshot_open_failures_have_a_distinct_error(self):
        store = self.make_store()
        store.install([USER_QUERY])
        with mock.patch.object(
            cursor_chat.sqlite3,
            "connect",
            side_effect=sqlite3.DatabaseError("private snapshot open failed"),
        ):
            with self.assertRaises(CursorChatOpenError):
                snapshot_cursor_chat(store.path)

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

    def test_installed_cursor_cli_golden_decodes_rich_message_sequence(self):
        fixture = json.loads(CURSOR_CLI_GOLDEN.read_text(encoding="utf-8"))
        database = reconstruct_golden_store(
            self.root,
            fixture,
            "golden-store.db",
        )
        state = snapshot_cursor_chat(
            database,
            broker=CursorChatSnapshotBroker(),
        )

        self.assertEqual(
            fixture["expected"]["decoded_state"],
            golden_decoded_state(state),
        )

    def test_full_golden_comparison_rejects_nested_mutation_and_omission(self):
        fixture = json.loads(CURSOR_CLI_GOLDEN.read_text(encoding="utf-8"))
        database = reconstruct_golden_store(
            self.root,
            fixture,
            "golden-mutation-store.db",
        )
        state = snapshot_cursor_chat(
            database,
            broker=CursorChatSnapshotBroker(),
        )
        expected = fixture["expected"]["decoded_state"]
        actual = golden_decoded_state(state)
        self.assertEqual(expected, actual)

        changed_argument = copy.deepcopy(actual)
        changed_argument["messages"][3]["content"][1]["args"][
            "command"
        ] = "fixture-mutated-command"

        omitted_tool_result = copy.deepcopy(actual)
        del omitted_tool_result["messages"][4]["providerOptions"]["cursor"][
            "highLevelToolCallResult"
        ]["output"]["success"]["stdout"]

        omitted_reasoning_metadata = copy.deepcopy(actual)
        del omitted_reasoning_metadata["messages"][3]["content"][0][
            "signature"
        ]

        for label, mutation in (
            ("nested tool argument changed", changed_argument),
            ("nested tool result omitted", omitted_tool_result),
            ("reasoning metadata omitted", omitted_reasoning_metadata),
        ):
            with self.subTest(mutation=label):
                with self.assertRaises(AssertionError):
                    self.assertEqual(expected, mutation)

    def test_installed_cursor_cli_golden_is_structural_and_sanitized(self):
        fixture_bytes = CURSOR_CLI_GOLDEN.read_bytes()
        self.assertLessEqual(len(fixture_bytes), 200 * 1024)
        fixture = json.loads(fixture_bytes)
        self.assertEqual(1, fixture["format"])
        self.assertEqual("3.16.17", fixture["provenance"]["cursor_version"])

        decoded_bytes = [fixture_bytes]
        for rows in fixture["kv"].values():
            for row in rows:
                decoded_bytes.extend(bytes.fromhex(value) for value in row)
        scanned = b"\n".join(decoded_bytes)

        home = str(Path.home()).encode("utf-8")
        username = getpass.getuser().encode("utf-8")
        self.assertNotIn(home, scanned)
        if username.lower() not in {b"root", b"runner", b"user"}:
            self.assertNotIn(username, scanned)
        forbidden_patterns = (
            rb"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}"
            rb"-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            rb"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
            rb"(?i)(?:sk-|ghp_|github_pat_|AKIA|ASIA|xox[baprs]-|"
            rb"Bearer\s|-----BEGIN|AIza|pypi-|npm_|glpat-|hf_)",
        )
        for pattern in forbidden_patterns:
            self.assertIsNone(re.search(pattern, scanned))

        blobs = {
            bytes.fromhex(blob_id_hex).decode("ascii"): bytes.fromhex(data_hex)
            for blob_id_hex, data_hex in fixture["kv"]["blobs"]
        }
        for blob_id, data in blobs.items():
            self.assertRegex(blob_id, r"^[0-9a-f]{64}$")
            self.assertEqual(blob_id, hashlib.sha256(data).hexdigest())

        meta_key_hex, meta_value_hex = fixture["kv"]["meta"][0]
        self.assertEqual(b"0", bytes.fromhex(meta_key_hex))
        metadata_hex = bytes.fromhex(meta_value_hex).decode("ascii")
        metadata = json.loads(bytes.fromhex(metadata_hex))
        root_id = fixture["expected"]["root_blob_id"]
        self.assertEqual(root_id, metadata["latestRootBlobId"])
        self.assertEqual(1, metadata["createdAt"])

        root = blobs[root_id]
        root_fields = golden_wire_fields(root)
        self.assertEqual(
            fixture["provenance"]["root_wire_fields"],
            golden_wire_shape(root),
        )
        self.assertEqual(
            fixture["expected"]["message_ids"],
            [
                value.hex()
                for number, wire_type, value in root_fields
                if number == 1 and wire_type == 2
            ],
        )
        field_8_id = next(
            value.hex()
            for number, wire_type, value in root_fields
            if number == 8 and wire_type == 2
        )
        self.assertIn(field_8_id, blobs)
        self.assertEqual(
            fixture["provenance"]["nested_wire_fields"]["root_field_8_blob"],
            golden_wire_shape(blobs[field_8_id]),
        )
        for field_number in (5, 15):
            payload = next(
                value
                for number, wire_type, value in root_fields
                if number == field_number and wire_type == 2
            )
            self.assertEqual(
                fixture["provenance"]["nested_wire_fields"][
                    "root_field_{}".format(field_number)
                ],
                golden_wire_shape(payload),
            )

        messages = [
            json.loads(blobs[blob_id])
            for blob_id in fixture["expected"]["message_ids"]
        ]
        self.assertEqual(
            fixture["provenance"]["message_shapes"],
            [golden_json_structure(message) for message in messages],
        )
        structural = set(fixture["sanitization"]["structural_values"])
        prefixes = tuple(fixture["sanitization"]["synthetic_prefixes"])
        for value in golden_json_strings(metadata) + golden_json_strings(
            fixture["expected"]["decoded_state"]
        ) + [
            string
            for message in messages
            for string in golden_json_strings(message)
        ]:
            if re.fullmatch(r"[0-9a-f]{64}", value) or value in structural:
                continue
            self.assertTrue(value.startswith(prefixes), value)
        numeric_values = [
            item
            for message in messages
            for item in golden_json_numbers(message)
        ]
        self.assertTrue(all(value == 0 for value in numeric_values))

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

    def test_jsonl_and_chat_titles_share_bounded_wrapper_precedence(self):
        huge_query = "x" * (CURSOR_TITLE_MAX_INPUT_CHARS + 100)
        cases = (
            (
                "later explicit query",
                [
                    {"role": "user", "content": "Earlier plain fallback"},
                    {
                        "role": "user",
                        "content": "<user_info>private</user_info>"
                        "<user_query>Explicit request</user_query>",
                    },
                ],
                "Explicit request",
            ),
            (
                "related metadata fallback",
                [
                    {
                        "role": "user",
                        "content": "<open_and_recently_viewed_files>private files"
                        "</open_and_recently_viewed_files>"
                        "<rules>generated rules</rules>\nVisible request",
                    }
                ],
                "Visible request",
            ),
            (
                "split content blocks",
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "<user_query>Split"},
                            {
                                "type": "text",
                                "text": "across blocks</user_query>",
                            },
                        ],
                    }
                ],
                "Split across blocks",
            ),
            (
                "malformed metadata before query",
                [
                    {
                        "role": "user",
                        "content": "<user_info>unterminated private metadata",
                    },
                    {
                        "role": "user",
                        "content": "<user_query>Recovered request</user_query>",
                    },
                ],
                "Recovered request",
            ),
            (
                "nested query wrappers",
                [
                    {
                        "role": "user",
                        "content": "<user_query>Outer <user_query>inner"
                        "</user_query> tail</user_query>",
                    }
                ],
                "Outer inner tail",
            ),
            (
                "huge unterminated bounded query",
                [
                    {
                        "role": "user",
                        "content": "<user_query>{}</user_query>"
                        "<user_info>must not leak</user_info>".format(huge_query),
                    }
                ],
                "x" * (CURSOR_TITLE_LIMIT - 1) + "…",
            ),
            (
                "terminal controls remain unchanged",
                [
                    {
                        "role": "user",
                        "content": "<user_query>Keep \x1b[31mred\x1b[0m"
                        " title</user_query>",
                    }
                ],
                "Keep \x1b[31mred\x1b[0m title",
            ),
        )
        for index, (label, messages, expected) in enumerate(cases):
            with self.subTest(case=label):
                transcript = self.root / "title-{}.jsonl".format(index)
                transcript.write_text(
                    "".join(
                        json.dumps(message, ensure_ascii=False) + "\n"
                        for message in messages
                    ),
                    encoding="utf-8",
                )
                store = self.make_store()
                store.install(messages, name="Metadata fallback")

                jsonl_title = _first_cursor_user_text(transcript)
                chat_title = snapshot_cursor_chat(store.path).title

                self.assertEqual(expected, jsonl_title)
                self.assertEqual(jsonl_title, chat_title)
                self.assertLessEqual(len(chat_title), CURSOR_TITLE_LIMIT)
                self.assertNotIn("must not leak", chat_title)

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
        delays = []
        broker = CursorChatSnapshotBroker()

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
            state = snapshot_cursor_chat(
                store.path,
                broker=broker,
                sleeper=delays.append,
            )

        self.assertTrue(raced[0])
        self.assertGreaterEqual(copied.call_count, 3)
        self.assertEqual(
            [cursor_chat.SNAPSHOT_BACKOFF_INITIAL_SECONDS],
            delays,
        )
        self.assertEqual(2, len(state.messages))
        self.assertEqual("After retry", state.metadata.name)

    def test_repeated_races_raise_busy(self):
        store = self.make_store()
        store.install([USER_QUERY])
        original = cursor_chat._copy_regular_file
        delays = []
        broker = CursorChatSnapshotBroker()

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
                snapshot_cursor_chat(
                    store.path,
                    broker=broker,
                    sleeper=delays.append,
                )

        self.assertEqual(
            [
                cursor_chat.SNAPSHOT_BACKOFF_INITIAL_SECONDS,
                min(
                    cursor_chat.SNAPSHOT_BACKOFF_INITIAL_SECONDS * 2,
                    cursor_chat.SNAPSHOT_BACKOFF_MAX_SECONDS,
                ),
            ],
            delays,
        )
        self.assertLessEqual(
            sum(delays),
            (cursor_chat.SNAPSHOT_ATTEMPTS - 1)
            * cursor_chat.SNAPSHOT_BACKOFF_MAX_SECONDS,
        )
        self.assertEqual(0, broker.stats.entries)
        self.assertEqual(1, broker.stats.errors)

    def test_permanent_copy_permission_error_is_immediate(self):
        store = self.make_store()
        store.install([USER_QUERY])
        broker = CursorChatSnapshotBroker()
        delays = []

        with mock.patch(
            "sidecar.cursor_chat._copy_regular_file",
            side_effect=PermissionError("denied"),
        ) as copied:
            with self.assertRaises(CursorChatSourceError) as raised:
                snapshot_cursor_chat(
                    store.path,
                    broker=broker,
                    sleeper=delays.append,
                )

        self.assertEqual(
            "Cursor chat snapshot cannot be copied",
            str(raised.exception),
        )
        self.assertEqual(1, copied.call_count)
        self.assertEqual([], delays)
        self.assertEqual(1, broker.stats.snapshot_loads)
        self.assertEqual(1, broker.stats.errors)
        self.assertEqual(0, broker.stats.entries)

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

    def test_broker_reuses_and_invalidates_immutable_decoded_state(self):
        store = self.make_store()
        store.install([USER_QUERY])
        broker = CursorChatSnapshotBroker()
        cache_key = broker.cache_key(store.path)
        db_details = store.path.stat()
        wal_path = Path(str(store.path) + "-wal")
        wal_details = wal_path.stat()

        self.assertEqual(os.path.realpath(str(store.path)), cache_key[0])
        self.assertEqual(
            (
                True,
                db_details.st_ino,
                db_details.st_mtime_ns,
                db_details.st_size,
            ),
            cache_key[1][0][:4],
        )
        self.assertEqual(
            (
                True,
                wal_details.st_ino,
                wal_details.st_mtime_ns,
                wal_details.st_size,
            ),
            cache_key[1][1][:4],
        )

        first = broker.snapshot(store.path)
        second = broker.snapshot(store.path)
        follower = CursorChatFollower(
            store.path,
            snapshot_broker=broker,
        )
        self.assertEqual([], follower.poll())
        self.assertEqual([], follower.poll())

        self.assertIs(first, second)
        self.assertEqual(1, broker.stats.snapshot_loads)
        self.assertGreaterEqual(broker.stats.cache_hits, 2)

        store.install([USER_QUERY, ASSISTANT])
        updated = broker.snapshot(store.path)

        self.assertIsNot(first, updated)
        self.assertEqual(2, broker.stats.snapshot_loads)
        self.assertEqual(2, len(updated.messages))

    def test_broker_ttl_eviction_reset_and_error_caching_bounds(self):
        now = [10.0]
        broker = CursorChatSnapshotBroker(
            max_entries=1,
            hard_max_entries=1,
            ttl_seconds=1.0,
            clock=lambda: now[0],
        )
        first = self.make_store()
        first.install([USER_QUERY])
        second = self.make_store()
        second.install([ASSISTANT])

        success_delays = []
        broker.snapshot(first.path, sleeper=success_delays.append)
        broker.snapshot(second.path)
        self.assertEqual([], success_delays)
        self.assertEqual(1, broker.stats.entries)
        self.assertEqual(1, broker.stats.evictions)

        now[0] += 2.0
        broker.snapshot(second.path)
        self.assertEqual(3, broker.stats.snapshot_loads)
        self.assertEqual(1, broker.stats.expirations)
        generation = broker.stats.generation

        broker.reset()
        self.assertEqual(0, broker.stats.entries)
        self.assertEqual(0, broker.stats.snapshot_loads)
        self.assertEqual(generation + 1, broker.stats.generation)

        bad_path = self.root / "bad-broker-schema.db"
        connection = sqlite3.connect(str(bad_path))
        connection.execute("CREATE TABLE invalid (value TEXT)")
        connection.commit()
        connection.close()
        delays = []
        for _attempt in range(2):
            with self.assertRaises(CursorChatSchemaError):
                broker.snapshot(bad_path, sleeper=delays.append)
        self.assertEqual([], delays)
        self.assertEqual(2, broker.stats.snapshot_loads)
        self.assertEqual(2, broker.stats.errors)
        self.assertEqual(0, broker.stats.entries)

        limit_delays = []
        with mock.patch("sidecar.cursor_chat.MAX_DB_BYTES", 1):
            with self.assertRaises(CursorChatLimitError):
                CursorChatSnapshotBroker().snapshot(
                    first.path,
                    sleeper=limit_delays.append,
                )
        self.assertEqual([], limit_delays)

    def test_concurrent_same_signature_is_single_flight(self):
        store = self.make_store()
        store.install([USER_QUERY, ASSISTANT])
        broker = CursorChatSnapshotBroker()
        callers = 8
        start = threading.Barrier(callers)
        all_signed = threading.Event()
        count_lock = threading.Lock()
        result_lock = threading.Lock()
        signed = [0]
        results = []
        errors = []
        original_signature = cursor_chat._source_signature
        original_loader = cursor_chat._snapshot_cursor_chat_uncached

        def counted_signature(path):
            signature = original_signature(path)
            with count_lock:
                signed[0] += 1
                if signed[0] == callers:
                    all_signed.set()
            return signature

        def gated_loader(path, **kwargs):
            if not all_signed.wait(2.0):
                raise RuntimeError("concurrent callers did not reach the broker")
            return original_loader(path, **kwargs)

        def worker():
            try:
                start.wait()
                state = broker.snapshot(store.path)
                with result_lock:
                    results.append(state)
            except BaseException as error:
                with result_lock:
                    errors.append(error)

        with (
            mock.patch(
                "sidecar.cursor_chat._source_signature",
                side_effect=counted_signature,
            ),
            mock.patch(
                "sidecar.cursor_chat._snapshot_cursor_chat_uncached",
                side_effect=gated_loader,
            ),
        ):
            threads = [threading.Thread(target=worker) for _index in range(callers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(callers, len(results))
        self.assertTrue(all(state is results[0] for state in results))
        self.assertEqual(1, broker.stats.snapshot_loads)
        self.assertGreater(broker.stats.coalesced_waits, 0)
        self.assertEqual(
            callers - 1,
            broker.stats.coalesced_waits + broker.stats.cache_hits,
        )
        self.assertEqual(0, broker.stats.in_flight)

    def test_different_stores_load_concurrently_with_bounded_entries(self):
        stores = [self.make_store(), self.make_store(), self.make_store()]
        for index, store in enumerate(stores):
            store.install([{"role": "user", "content": str(index)}])
        broker = CursorChatSnapshotBroker(
            max_entries=1,
            hard_max_entries=1,
        )
        entered = threading.Barrier(2)
        original_loader = cursor_chat._snapshot_cursor_chat_uncached
        results = []
        errors = []
        result_lock = threading.Lock()

        def parallel_loader(path, **kwargs):
            entered.wait(2.0)
            return original_loader(path, **kwargs)

        def worker(store):
            try:
                state = broker.snapshot(store.path)
                with result_lock:
                    results.append(state)
            except BaseException as error:
                with result_lock:
                    errors.append(error)

        with mock.patch(
            "sidecar.cursor_chat._snapshot_cursor_chat_uncached",
            side_effect=parallel_loader,
        ):
            threads = [
                threading.Thread(target=worker, args=(store,))
                for store in stores[:2]
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(2, broker.stats.snapshot_loads)
        self.assertEqual(1, broker.stats.entries)
        self.assertEqual(1, broker.stats.evictions)

        broker.snapshot(stores[2].path)
        self.assertEqual(1, broker.stats.entries)
        self.assertEqual(2, broker.stats.evictions)

    def test_publication_clock_failure_completes_flight_with_typed_error(self):
        store = self.make_store()
        store.install([USER_QUERY])
        clock_calls = [0]

        def raising_publication_clock():
            clock_calls[0] += 1
            if clock_calls[0] == 2:
                raise OSError("clock failed")
            return 10.0

        broker = CursorChatSnapshotBroker(clock=raising_publication_clock)

        with self.assertRaises(CursorChatSourceError) as raised:
            broker.snapshot(store.path)

        self.assertEqual(
            "Cursor chat snapshot clock failed",
            str(raised.exception),
        )
        self.assertEqual(0, broker.stats.in_flight)
        self.assertEqual(0, broker.stats.entries)
        self.assertEqual(1, broker.stats.errors)

        broker._clock = lambda: 10.0
        self.assertEqual(1, len(broker.snapshot(store.path).messages))
        self.assertEqual(2, broker.stats.snapshot_loads)

    def test_publication_probe_wakes_all_single_flight_waiters(self):
        store = self.make_store()
        store.install([USER_QUERY])
        publication_probe = mock.Mock(side_effect=RuntimeError("publish failed"))
        broker = CursorChatSnapshotBroker(
            publication_probe=publication_probe,
        )
        original_loader = cursor_chat._snapshot_cursor_chat_uncached
        loader_started = threading.Event()
        release_loader = threading.Event()
        errors = []
        errors_lock = threading.Lock()

        def gated_loader(path, **kwargs):
            loader_started.set()
            if not release_loader.wait(2.0):
                raise RuntimeError("loader was not released")
            return original_loader(path, **kwargs)

        def worker():
            try:
                broker.snapshot(store.path)
            except BaseException as error:
                with errors_lock:
                    errors.append(error)

        with mock.patch(
            "sidecar.cursor_chat._snapshot_cursor_chat_uncached",
            side_effect=gated_loader,
        ):
            leader = threading.Thread(target=worker)
            waiter = threading.Thread(target=worker)
            leader.start()
            self.assertTrue(loader_started.wait(1.0))
            waiter.start()
            for _attempt in range(1_000):
                if broker.stats.coalesced_waits == 1:
                    break
                threading.Event().wait(0.001)
            release_loader.set()
            leader.join(2.0)
            waiter.join(2.0)

        self.assertFalse(leader.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(2, len(errors))
        self.assertTrue(
            all(isinstance(error, CursorChatSourceError) for error in errors)
        )
        self.assertEqual(
            {"Cursor chat snapshot publication failed"},
            {str(error) for error in errors},
        )
        self.assertEqual(1, publication_probe.call_count)
        self.assertEqual(1, broker.stats.snapshot_loads)
        self.assertEqual(1, broker.stats.coalesced_waits)
        self.assertEqual(1, broker.stats.errors)
        self.assertEqual(0, broker.stats.in_flight)
        self.assertEqual(0, broker.stats.entries)

    def test_scan_generation_pins_65_stores_until_ttl(self):
        template_store = self.make_store()
        template_store.install([USER_QUERY])
        template = snapshot_cursor_chat(template_store.path)
        paths = []
        for index in range(65):
            path = self.root / "scan-{}.db".format(index)
            path.write_bytes(str(index).encode("ascii"))
            paths.append(path)
        now = [10.0]
        broker = CursorChatSnapshotBroker(
            max_entries=64,
            hard_max_entries=80,
            ttl_seconds=1.0,
            clock=lambda: now[0],
        )
        loads = {}

        def load(path, **_kwargs):
            key = str(path)
            loads[key] = loads.get(key, 0) + 1
            return template, cursor_chat._source_signature(path)

        with mock.patch(
            "sidecar.cursor_chat._snapshot_cursor_chat_uncached",
            side_effect=load,
        ):
            with broker.scan_generation() as outer_generation:
                for path in paths:
                    broker.snapshot(path)
                now[0] += 2.0
                with broker.scan_generation() as nested_generation:
                    self.assertEqual(outer_generation, nested_generation)
                    self.assertEqual(2, broker.scan_depth)
                    for path in paths:
                        broker.snapshot(path)
                self.assertEqual(1, broker.scan_depth)
                self.assertEqual({1}, set(loads.values()))
                self.assertEqual(65, broker.stats.snapshot_loads)
                self.assertEqual(65, broker.stats.entries)
                self.assertEqual(0, broker.stats.evictions)

        self.assertEqual(0, broker.scan_depth)
        self.assertEqual(64, broker.stats.entries)
        self.assertEqual(1, broker.stats.evictions)

        now[0] += 2.0
        with broker.scan_generation():
            pass
        self.assertEqual(0, broker.stats.entries)
        self.assertEqual(64, broker.stats.expirations)

    def test_soft_capacity_is_pinned_but_hard_bound_is_deterministic(self):
        template_store = self.make_store()
        template_store.install([USER_QUERY])
        template = snapshot_cursor_chat(template_store.path)
        paths = []
        for index in range(5):
            path = self.root / "bounded-{}.db".format(index)
            path.write_bytes(str(index).encode("ascii"))
            paths.append(path)
        now = [20.0]
        broker = CursorChatSnapshotBroker(
            max_entries=2,
            hard_max_entries=4,
            ttl_seconds=1.0,
            clock=lambda: now[0],
        )
        loads = {}

        def load(path, **_kwargs):
            key = str(path)
            loads[key] = loads.get(key, 0) + 1
            return template, cursor_chat._source_signature(path)

        with mock.patch(
            "sidecar.cursor_chat._snapshot_cursor_chat_uncached",
            side_effect=load,
        ):
            with broker.scan_generation():
                for path in paths[:3]:
                    broker.snapshot(path)
                with broker.scan_generation():
                    for path in paths[:3]:
                        self.assertTrue(broker.pin(broker.cache_key(path)))

                self.assertEqual({1}, set(loads.values()))
                self.assertEqual(3, broker.stats.entries)
                self.assertEqual(0, broker.stats.evictions)

                broker.snapshot(paths[3])
                broker.snapshot(paths[4])
                self.assertEqual(4, broker.stats.entries)
                self.assertEqual(1, broker.stats.evictions)

                broker.snapshot(paths[0])
                self.assertEqual(2, loads[str(paths[0])])
                self.assertEqual(4, broker.stats.entries)
                self.assertEqual(2, broker.stats.evictions)

        self.assertEqual(2, broker.stats.entries)
        self.assertLessEqual(broker.stats.entries, broker.hard_max_entries)

        now[0] += 2.0
        with broker.scan_generation():
            pass
        self.assertEqual(0, broker.stats.entries)

    def test_scan_generation_exception_unpins_and_trims(self):
        stores = [self.make_store(), self.make_store()]
        for store in stores:
            store.install([USER_QUERY])
        broker = CursorChatSnapshotBroker(
            max_entries=1,
            hard_max_entries=2,
        )

        with self.assertRaisesRegex(ValueError, "scan failed"):
            with broker.scan_generation():
                broker.snapshot(stores[0].path)
                broker.snapshot(stores[1].path)
                self.assertEqual(2, broker.stats.entries)
                self.assertEqual(1, broker.scan_depth)
                raise ValueError("scan failed")

        self.assertEqual(0, broker.scan_depth)
        self.assertEqual(1, broker.stats.entries)
        self.assertEqual(1, broker.stats.evictions)

    def test_recursive_state_weight_and_hard_cache_bytes_bound_large_stores(self):
        state_store = self.make_store()
        large_text = "x" * (256 * 1024)
        state_store.install(
            [{"role": "assistant", "content": large_text}]
        )
        state = snapshot_cursor_chat(state_store.path)
        self.assertGreater(
            cursor_chat.cursor_chat_state_weight(state),
            len(large_text.encode("utf-8")),
        )
        self.assertGreater(cursor_chat._retained_size(b"z" * 1024), 1024)

        paths = []
        for index in range(10):
            path = self.root / "max-source-{}.db".format(index)
            with path.open("wb") as stream:
                stream.truncate(cursor_chat.MAX_DB_BYTES)
            paths.append(path)
        sample_key = (
            str(cursor_chat._canonical_store_path(paths[0])),
            cursor_chat._source_signature(paths[0]),
        )
        entry_weight = cursor_chat._cache_entry_weight(sample_key, state)
        broker = CursorChatSnapshotBroker(
            max_cache_bytes=entry_weight * 2 + 4096,
            hard_max_cache_bytes=entry_weight * 3 + 4096,
            max_entry_bytes=entry_weight + 4096,
        )
        loads = {}

        def load(path, **_kwargs):
            key = str(path)
            loads[key] = loads.get(key, 0) + 1
            return state, cursor_chat._source_signature(path)

        with mock.patch(
            "sidecar.cursor_chat._snapshot_cursor_chat_uncached",
            side_effect=load,
        ):
            with broker.scan_generation():
                for path in paths:
                    self.assertIs(state, broker.snapshot(path))
                    self.assertLessEqual(
                        broker.stats.cache_bytes,
                        broker.hard_max_cache_bytes,
                    )
                loads_before_hit = broker.stats.snapshot_loads
                self.assertIs(state, broker.snapshot(paths[-1]))
                self.assertEqual(
                    loads_before_hit,
                    broker.stats.snapshot_loads,
                )

        self.assertEqual({1}, set(loads.values()))
        self.assertEqual(10, broker.stats.snapshot_loads)
        self.assertGreater(broker.stats.evictions, 0)
        self.assertLessEqual(
            broker.stats.cache_bytes,
            broker.max_cache_bytes,
        )
        self.assertLessEqual(
            broker.stats.peak_cache_bytes,
            broker.hard_max_cache_bytes,
        )

    def test_oversized_decoded_state_is_visible_but_never_cached(self):
        store = self.make_store()
        store.install(
            [{"role": "assistant", "content": "y" * (64 * 1024)}]
        )
        state = snapshot_cursor_chat(store.path)
        key = (
            str(cursor_chat._canonical_store_path(store.path)),
            cursor_chat._source_signature(store.path),
        )
        entry_weight = cursor_chat._cache_entry_weight(key, state)
        broker = CursorChatSnapshotBroker(
            max_cache_bytes=entry_weight * 2,
            hard_max_cache_bytes=entry_weight * 2,
            max_entry_bytes=entry_weight - 1,
        )

        with mock.patch(
            "sidecar.cursor_chat._snapshot_cursor_chat_uncached",
            return_value=(state, cursor_chat._source_signature(store.path)),
        ):
            self.assertIs(state, broker.snapshot(store.path))
            self.assertIs(state, broker.snapshot(store.path))

        self.assertEqual(2, broker.stats.snapshot_loads)
        self.assertEqual(2, broker.stats.oversized_states)
        self.assertEqual(0, broker.stats.entries)
        self.assertEqual(0, broker.stats.cache_bytes)

    def test_source_byte_budget_backpressures_different_store_loads(self):
        paths = []
        for index in range(2):
            path = self.root / "budget-{}.db".format(index)
            path.write_bytes(b"x" * 60)
            paths.append(path)
        template_store = self.make_store()
        template_store.install([USER_QUERY])
        state = snapshot_cursor_chat(template_store.path)
        broker = CursorChatSnapshotBroker(
            max_in_flight=2,
            max_in_flight_source_bytes=100,
        )
        first_started = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        active = [0]
        peak_active = [0]
        calls = [0]
        lock = threading.Lock()
        errors = []

        def load(path, **_kwargs):
            with lock:
                calls[0] += 1
                call = calls[0]
                active[0] += 1
                peak_active[0] = max(peak_active[0], active[0])
            try:
                if call == 1:
                    first_started.set()
                    if not release_first.wait(2.0):
                        raise RuntimeError("first load was not released")
                    raise CursorChatSourceError("first load failed")
                else:
                    second_started.set()
                return state, cursor_chat._source_signature(path)
            finally:
                with lock:
                    active[0] -= 1

        def worker(path):
            try:
                broker.snapshot(path)
            except BaseException as error:
                errors.append(error)

        with mock.patch(
            "sidecar.cursor_chat._snapshot_cursor_chat_uncached",
            side_effect=load,
        ):
            first = threading.Thread(target=worker, args=(paths[0],))
            second = threading.Thread(target=worker, args=(paths[1],))
            first.start()
            self.assertTrue(first_started.wait(1.0))
            second.start()
            for _attempt in range(1_000):
                if broker.stats.budget_waits >= 1:
                    break
                threading.Event().wait(0.001)
            self.assertFalse(second_started.is_set())
            self.assertEqual(1, broker.stats.in_flight)
            self.assertEqual(100, broker.stats.in_flight_source_bytes)
            self.assertGreaterEqual(broker.stats.budget_waits, 1)
            release_first.set()
            first.join(2.0)
            second.join(2.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], CursorChatSourceError)
        self.assertTrue(second_started.is_set())
        self.assertEqual(1, peak_active[0])
        self.assertEqual(2, broker.stats.snapshot_loads)
        self.assertEqual(0, broker.stats.in_flight)
        self.assertEqual(0, broker.stats.in_flight_source_bytes)
        self.assertEqual(100, broker.stats.peak_in_flight_source_bytes)

        limited = CursorChatSnapshotBroker(
            max_in_flight_source_bytes=50,
        )
        with self.assertRaises(CursorChatLimitError):
            limited.snapshot(paths[0])
        self.assertEqual(0, limited.stats.in_flight)
        self.assertEqual(0, limited.stats.in_flight_source_bytes)

    def test_global_copy_slots_bound_independent_brokers(self):
        stores = [self.make_store(), self.make_store(), self.make_store()]
        for store in stores:
            store.install([USER_QUERY])
        brokers = [CursorChatSnapshotBroker() for _store in stores]
        original = cursor_chat._read_stable_copy_with_signature_unlimited
        release = threading.Event()
        two_started = threading.Event()
        lock = threading.Lock()
        active = [0]
        peak = [0]
        entered = [0]
        errors = []

        def blocked_copy(*args, **kwargs):
            with lock:
                active[0] += 1
                entered[0] += 1
                peak[0] = max(peak[0], active[0])
                if active[0] == cursor_chat.DEFAULT_SNAPSHOT_MAX_IN_FLIGHT:
                    two_started.set()
            try:
                if not release.wait(2.0):
                    raise RuntimeError("global copies were not released")
                return original(*args, **kwargs)
            finally:
                with lock:
                    active[0] -= 1

        def worker(index):
            try:
                brokers[index].snapshot(stores[index].path)
            except BaseException as error:
                errors.append(error)

        with mock.patch(
            "sidecar.cursor_chat._read_stable_copy_with_signature_unlimited",
            side_effect=blocked_copy,
        ):
            threads = [
                threading.Thread(target=worker, args=(index,))
                for index in range(3)
            ]
            threads[0].start()
            threads[1].start()
            self.assertTrue(two_started.wait(1.0))
            threads[2].start()
            threading.Event().wait(0.05)
            self.assertEqual(2, entered[0])
            self.assertTrue(threads[2].is_alive())
            release.set()
            for thread in threads:
                thread.join(2.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(3, entered[0])
        self.assertEqual(
            cursor_chat.DEFAULT_SNAPSHOT_MAX_IN_FLIGHT,
            peak[0],
        )


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
