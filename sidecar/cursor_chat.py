"""Strict-readonly snapshots and logical following for Cursor CLI chats.

The source SQLite database is never opened.  A stable, bounded copy of the
main database and optional WAL is opened in a private temporary directory so
SQLite cannot read or update the live ``-shm`` file.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)
from urllib.parse import unquote_to_bytes, urlsplit


SNAPSHOT_ATTEMPTS = 3
COPY_CHUNK_BYTES = 1024 * 1024
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_WAL_BYTES = 256 * 1024 * 1024
MAX_META_BYTES = 256 * 1024
MAX_ROOT_BLOB_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_BLOB_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_PROTO_FIELDS = 65_536
MAX_PROTO_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_REFERENCES = 4_096
MAX_CONTENT_BLOCKS = 4_096
MAX_WORKSPACE_URI_BYTES = 16 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 65_536
MAX_SQL_BATCH = 128
DEFAULT_FOLLOW_RECORDS = 256
MAX_PENDING_RECORDS = MAX_MESSAGE_REFERENCES + 1
MAX_CHECKPOINT_BYTES = 16 * 1024

_HEX_ID = re.compile(r"^[0-9a-f]{64}$")
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_USER_QUERY = re.compile(
    r"<user_query>\s*(.*?)\s*</user_query>",
    re.DOTALL | re.IGNORECASE,
)
_USER_INFO = re.compile(
    r"<user_info>.*?</user_info>",
    re.DOTALL | re.IGNORECASE,
)
_MAX_FIELD_NUMBER = (1 << 29) - 1
_EMPTY_PREFIX_HASH = hashlib.sha256(b"").hexdigest()

FileSignature = Tuple[int, int, int, int, int]
SourceSignature = Tuple[FileSignature, Optional[FileSignature]]
ReadResult = TypeVar("ReadResult")
FrozenJSON = Union[
    None,
    bool,
    int,
    float,
    str,
    Tuple["FrozenJSON", ...],
    Mapping[str, "FrozenJSON"],
]


class CursorChatError(Exception):
    """Base class for safe, typed Cursor chat read failures."""

    recoverable = False


class CursorChatSourceError(CursorChatError):
    """The source store is absent, invalid, or unreadable."""

    recoverable = True


class CursorChatBusyError(CursorChatError):
    """The source changed during every bounded snapshot attempt."""

    recoverable = True


class CursorChatLimitError(CursorChatError):
    """A configured resource bound was exceeded."""


class CursorChatSchemaError(CursorChatError):
    """The copied SQLite schema or runtime SQLite types are invalid."""


class CursorChatMetadataError(CursorChatError):
    """The production metadata record is absent or malformed."""


class CursorChatProtobufError(CursorChatError):
    """The latest-root protobuf wire message is invalid."""


class CursorChatBlobError(CursorChatError):
    """A referenced content-addressed blob is absent or invalid."""


@dataclass(frozen=True)
class CursorChatMetadata:
    """Validated production metadata from key ``"0"``."""

    agent_id: str
    latest_root_blob_id: str
    name: str
    mode: str
    created_at: float


@dataclass(frozen=True)
class CursorChatState:
    """One immutable logical chat state decoded from a stable snapshot."""

    metadata: CursorChatMetadata
    root_blob_id: str
    message_ids: Tuple[str, ...]
    messages: Tuple[Mapping[str, FrozenJSON], ...]
    provisional: Optional[Mapping[str, FrozenJSON]]
    provisional_hash: Optional[str]
    project: str
    created_at: float
    title: str

def _safe_message(error_type: type, message: str) -> CursorChatError:
    return error_type(message)


def _normalize_path(path: Union[str, os.PathLike]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _regular_signature(path: Path, maximum: int, required: bool) -> Optional[FileSignature]:
    try:
        details = os.stat(str(path))
    except FileNotFoundError:
        if required:
            raise _safe_message(CursorChatSourceError, "Cursor chat database is missing")
        return None
    except OSError:
        raise _safe_message(CursorChatSourceError, "Cursor chat source cannot be inspected")
    if not stat.S_ISREG(details.st_mode):
        raise _safe_message(CursorChatSourceError, "Cursor chat source is not a regular file")
    size = int(details.st_size)
    if size < 0 or size > maximum:
        raise _safe_message(CursorChatLimitError, "Cursor chat source exceeds its size limit")
    return (
        int(details.st_dev),
        int(details.st_ino),
        size,
        int(getattr(details, "st_mtime_ns", int(details.st_mtime * 1e9))),
        int(getattr(details, "st_ctime_ns", int(details.st_ctime * 1e9))),
    )


def _copy_regular_file(source: Path, destination: Path, expected_size: int) -> int:
    copied = 0
    with source.open("rb") as reader, destination.open("xb") as writer:
        while copied < expected_size:
            chunk = reader.read(min(COPY_CHUNK_BYTES, expected_size - copied))
            if not chunk:
                break
            writer.write(chunk)
            copied += len(chunk)
    return copied


class _DuplicateJSONKey(ValueError):
    pass


def _json_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey()
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError()


def _validate_json_text(value: str, error_type: type) -> None:
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise _safe_message(error_type, "Cursor chat JSON text is invalid")


def _validate_json_tree(value: Any, error_type: type) -> None:
    stack: List[Tuple[Any, int]] = [(value, 1)]
    nodes = 1
    while stack:
        current, depth = stack.pop()
        if depth > MAX_JSON_DEPTH:
            raise _safe_message(CursorChatLimitError, "Cursor chat JSON is nested too deeply")
        if isinstance(current, float) and not math.isfinite(current):
            raise _safe_message(error_type, "Cursor chat JSON number is invalid")
        if current is None or isinstance(current, (bool, int, float)):
            continue
        if isinstance(current, str):
            _validate_json_text(current, error_type)
            continue
        if isinstance(current, list):
            nodes += len(current)
            if nodes > MAX_JSON_NODES:
                raise _safe_message(
                    CursorChatLimitError,
                    "Cursor chat JSON has too many nodes",
                )
            stack.extend((item, depth + 1) for item in reversed(current))
            continue
        if isinstance(current, Mapping):
            nodes += len(current) * 2
            if nodes > MAX_JSON_NODES:
                raise _safe_message(
                    CursorChatLimitError,
                    "Cursor chat JSON has too many nodes",
                )
            for key, item in current.items():
                if not isinstance(key, str):
                    raise _safe_message(
                        error_type,
                        "Cursor chat JSON key is invalid",
                    )
                _validate_json_text(key, error_type)
                stack.append((item, depth + 1))
            continue
        raise _safe_message(error_type, "Cursor chat JSON value is unsupported")


def _strict_json(raw: bytes, error_type: type) -> Any:
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise _safe_message(error_type, "Cursor chat JSON is malformed")
    try:
        decoded = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except RecursionError:
        raise _safe_message(CursorChatLimitError, "Cursor chat JSON is nested too deeply")
    except (json.JSONDecodeError, ValueError):
        raise _safe_message(error_type, "Cursor chat JSON is malformed")
    _validate_json_tree(decoded, error_type)
    return decoded


def _freeze_json(value: Any) -> FrozenJSON:
    _validate_json_tree(value, CursorChatBlobError)

    def freeze(current: Any) -> FrozenJSON:
        if current is None or isinstance(current, (bool, int, float, str)):
            return current
        if isinstance(current, list):
            return tuple(freeze(item) for item in current)
        if isinstance(current, Mapping):
            return MappingProxyType(
                {key: freeze(item) for key, item in current.items()}
            )
        raise _safe_message(CursorChatBlobError, "Cursor chat JSON value is unsupported")

    try:
        return freeze(value)
    except RecursionError:
        raise _safe_message(CursorChatLimitError, "Cursor chat JSON is nested too deeply")


def _thaw_json(value: FrozenJSON) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _bounded_text(value: Any, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata type is invalid")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata text is invalid")
    if len(encoded) > MAX_META_BYTES:
        raise _safe_message(CursorChatLimitError, "Cursor chat metadata text is too large")
    if not allow_empty and not value:
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata field is empty")
    if any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in value
    ):
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata text is invalid")
    return value


def _created_at(value: Any) -> float:
    numeric = False
    if isinstance(value, bool) or value is None:
        raise _safe_message(CursorChatMetadataError, "Cursor chat createdAt is invalid")
    if isinstance(value, (int, float)):
        try:
            epoch = float(value)
        except (OverflowError, ValueError):
            raise _safe_message(
                CursorChatMetadataError,
                "Cursor chat createdAt is invalid",
            )
        numeric = True
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            epoch = float(raw)
            numeric = True
        except OverflowError:
            raise _safe_message(
                CursorChatMetadataError,
                "Cursor chat createdAt is invalid",
            )
        except ValueError:
            normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            try:
                moment = dt.datetime.fromisoformat(normalized)
            except ValueError:
                raise _safe_message(
                    CursorChatMetadataError,
                    "Cursor chat createdAt is invalid",
                )
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=dt.timezone.utc)
            try:
                epoch = moment.timestamp()
            except (OverflowError, OSError, ValueError):
                raise _safe_message(
                    CursorChatMetadataError,
                    "Cursor chat createdAt is invalid",
                )
    else:
        raise _safe_message(CursorChatMetadataError, "Cursor chat createdAt is invalid")
    if numeric:
        if not math.isfinite(epoch):
            raise _safe_message(CursorChatMetadataError, "Cursor chat createdAt is invalid")
        if abs(epoch) >= 100_000_000_000:
            epoch /= 1000.0
    if not math.isfinite(epoch):
        raise _safe_message(CursorChatMetadataError, "Cursor chat createdAt is invalid")
    try:
        dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise _safe_message(CursorChatMetadataError, "Cursor chat createdAt is invalid")
    return epoch


def _validate_blob_id(value: Any, error_type: type) -> str:
    if not isinstance(value, str) or _HEX_ID.fullmatch(value) is None:
        raise _safe_message(error_type, "Cursor chat blob id is invalid")
    return value


def _decode_metadata(raw_value: Any, runtime_type: Any) -> CursorChatMetadata:
    if runtime_type != "text" or not isinstance(raw_value, str):
        raise _safe_message(CursorChatSchemaError, "Cursor chat meta value must be TEXT")
    try:
        encoded = raw_value.encode("ascii", "strict")
    except UnicodeEncodeError:
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata hex is invalid")
    if len(encoded) > MAX_META_BYTES * 2:
        raise _safe_message(CursorChatLimitError, "Cursor chat metadata exceeds its limit")
    if not encoded or len(encoded) % 2:
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata hex is invalid")
    if any(character not in b"0123456789abcdefABCDEF" for character in encoded):
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata hex is invalid")
    try:
        decoded_bytes = bytes.fromhex(raw_value)
    except ValueError:
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata hex is invalid")
    decoded = _strict_json(decoded_bytes, CursorChatMetadataError)
    if not isinstance(decoded, Mapping):
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata is not an object")
    required = ("agentId", "latestRootBlobId", "name", "mode", "createdAt")
    if any(key not in decoded for key in required):
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata field is missing")
    return CursorChatMetadata(
        agent_id=_bounded_text(decoded["agentId"], allow_empty=False),
        latest_root_blob_id=_validate_blob_id(
            decoded["latestRootBlobId"],
            CursorChatMetadataError,
        ),
        name=_bounded_text(decoded["name"]),
        mode=_bounded_text(decoded["mode"]),
        created_at=_created_at(decoded["createdAt"]),
    )


def _read_varint(data: bytes, position: int) -> Tuple[int, int]:
    value = 0
    for index in range(10):
        if position >= len(data):
            raise _safe_message(CursorChatProtobufError, "Truncated protobuf varint")
        byte = data[position]
        position += 1
        if index == 9 and byte > 1:
            raise _safe_message(CursorChatProtobufError, "Protobuf varint overflow")
        value |= (byte & 0x7F) << (index * 7)
        if not byte & 0x80:
            return value, position
    raise _safe_message(CursorChatProtobufError, "Protobuf varint is too long")


def _scan_root(data: bytes) -> Tuple[Tuple[str, ...], Optional[bytes], Optional[bytes]]:
    if len(data) > MAX_ROOT_BLOB_BYTES:
        raise _safe_message(CursorChatLimitError, "Cursor chat root blob is too large")
    position = 0
    fields = 0
    payload_bytes = 0
    references: List[str] = []
    provisional: Optional[bytes] = None
    workspace_uri: Optional[bytes] = None
    while position < len(data):
        fields += 1
        if fields > MAX_PROTO_FIELDS:
            raise _safe_message(CursorChatLimitError, "Cursor chat protobuf has too many fields")
        key, position = _read_varint(data, position)
        field_number = key >> 3
        wire_type = key & 0x07
        if field_number <= 0 or field_number > _MAX_FIELD_NUMBER:
            raise _safe_message(CursorChatProtobufError, "Protobuf field number is invalid")
        if field_number in (1, 4, 9) and wire_type != 2:
            raise _safe_message(
                CursorChatProtobufError,
                "Cursor chat root field has the wrong wire type",
            )
        if wire_type == 0:
            _ignored, position = _read_varint(data, position)
            continue
        if wire_type == 1:
            if len(data) - position < 8:
                raise _safe_message(CursorChatProtobufError, "Truncated fixed64 field")
            position += 8
            continue
        if wire_type == 5:
            if len(data) - position < 4:
                raise _safe_message(CursorChatProtobufError, "Truncated fixed32 field")
            position += 4
            continue
        if wire_type != 2:
            raise _safe_message(CursorChatProtobufError, "Unsupported protobuf wire type")
        length, position = _read_varint(data, position)
        if length > len(data) - position:
            raise _safe_message(CursorChatProtobufError, "Protobuf length exceeds payload")
        payload_bytes += length
        if payload_bytes > MAX_PROTO_PAYLOAD_BYTES:
            raise _safe_message(CursorChatLimitError, "Cursor chat protobuf payload is too large")
        end = position + length
        payload = data[position:end]
        position = end
        if field_number == 1:
            if length != hashlib.sha256().digest_size:
                raise _safe_message(
                    CursorChatProtobufError,
                    "Cursor chat message digest has the wrong length",
                )
            references.append(payload.hex())
            if len(references) > MAX_MESSAGE_REFERENCES:
                raise _safe_message(
                    CursorChatLimitError,
                    "Cursor chat has too many message references",
                )
        elif field_number == 4:
            if length > MAX_MESSAGE_BLOB_BYTES:
                raise _safe_message(
                    CursorChatLimitError,
                    "Cursor chat provisional message is too large",
                )
            provisional = payload
        elif field_number == 9:
            if length > MAX_WORKSPACE_URI_BYTES:
                raise _safe_message(CursorChatLimitError, "Cursor workspace URI is too large")
            workspace_uri = payload
    return tuple(references), provisional, workspace_uri


def decode_file_uri(raw: Optional[bytes]) -> str:
    """Decode a local ``file://`` workspace URI without touching the path."""

    if raw is None:
        return ""
    try:
        uri = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise _safe_message(CursorChatProtobufError, "Cursor workspace URI is invalid")
    if _BAD_PERCENT_ESCAPE.search(uri):
        raise _safe_message(CursorChatProtobufError, "Cursor workspace URI is invalid")
    try:
        parsed = urlsplit(uri)
    except ValueError:
        raise _safe_message(CursorChatProtobufError, "Cursor workspace URI is invalid")
    if (
        parsed.scheme.lower() != "file"
        or parsed.netloc not in ("", "localhost")
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise _safe_message(CursorChatProtobufError, "Cursor workspace URI is not local")
    try:
        path = unquote_to_bytes(parsed.path).decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise _safe_message(CursorChatProtobufError, "Cursor workspace URI is invalid")
    if "\x00" in path or any(
        ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
        for character in path
    ):
        raise _safe_message(CursorChatProtobufError, "Cursor workspace URI is invalid")
    return path


def _validate_schema(connection: sqlite3.Connection) -> None:
    expected = {
        "blobs": {"id": ("TEXT", True), "data": ("BLOB", False)},
        "meta": {"key": ("TEXT", True), "value": ("TEXT", False)},
    }
    for table, columns in expected.items():
        try:
            master_rows = connection.execute(
                "SELECT type FROM sqlite_master WHERE name = ?",
                (table,),
            ).fetchall()
            pragma = "PRAGMA table_info({})".format(table)
            rows = connection.execute(pragma).fetchall()
        except sqlite3.DatabaseError:
            raise _safe_message(CursorChatSchemaError, "Cursor chat schema cannot be read")
        if len(master_rows) != 1 or master_rows[0][0] != "table":
            raise _safe_message(CursorChatSchemaError, "Cursor chat table is missing")
        actual = {
            str(row[1]): (str(row[2]).upper(), bool(row[5]))
            for row in rows
            if len(row) >= 6
        }
        for name, declaration in columns.items():
            if actual.get(name) != declaration:
                raise _safe_message(CursorChatSchemaError, "Cursor chat column is invalid")


def _fetch_blob(
    connection: sqlite3.Connection,
    blob_id: str,
    maximum: int,
) -> bytes:
    try:
        rows = connection.execute(
            "SELECT length(data), typeof(id), typeof(data) FROM blobs WHERE id = ?",
            (blob_id,),
        ).fetchall()
    except sqlite3.DatabaseError:
        raise _safe_message(CursorChatSchemaError, "Cursor chat blob index cannot be read")
    if len(rows) != 1:
        raise _safe_message(CursorChatBlobError, "Cursor chat blob is missing")
    length, id_type, data_type = rows[0]
    if (
        id_type != "text"
        or data_type != "blob"
        or not isinstance(length, int)
        or isinstance(length, bool)
        or length < 0
    ):
        raise _safe_message(CursorChatSchemaError, "Cursor chat blob runtime type is invalid")
    if length > maximum:
        raise _safe_message(CursorChatLimitError, "Cursor chat blob exceeds its size limit")
    try:
        row = connection.execute(
            "SELECT data FROM blobs WHERE id = ?",
            (blob_id,),
        ).fetchone()
    except sqlite3.DatabaseError:
        raise _safe_message(CursorChatSchemaError, "Cursor chat blob cannot be read")
    if row is None or not isinstance(row[0], bytes):
        raise _safe_message(CursorChatSchemaError, "Cursor chat blob data is not bytes")
    data = row[0]
    if len(data) != length or hashlib.sha256(data).hexdigest() != blob_id:
        raise _safe_message(CursorChatBlobError, "Cursor chat blob hash does not match")
    return data


def _fetch_message_blobs(
    connection: sqlite3.Connection,
    message_ids: Sequence[str],
) -> Mapping[str, bytes]:
    unique_ids = tuple(dict.fromkeys(message_ids))
    lengths: Dict[str, int] = {}
    total = 0
    for start in range(0, len(unique_ids), MAX_SQL_BATCH):
        batch = unique_ids[start : start + MAX_SQL_BATCH]
        placeholders = ",".join("?" for _ in batch)
        try:
            rows = connection.execute(
                "SELECT id, length(data), typeof(id), typeof(data) "
                "FROM blobs WHERE id IN ({})".format(placeholders),
                batch,
            ).fetchall()
        except sqlite3.DatabaseError:
            raise _safe_message(CursorChatSchemaError, "Cursor chat blob index cannot be read")
        for blob_id, length, id_type, data_type in rows:
            if (
                not isinstance(blob_id, str)
                or blob_id not in batch
                or id_type != "text"
                or data_type != "blob"
                or not isinstance(length, int)
                or isinstance(length, bool)
                or length < 0
            ):
                raise _safe_message(
                    CursorChatSchemaError,
                    "Cursor chat message blob type is invalid",
                )
            if length > MAX_MESSAGE_BLOB_BYTES:
                raise _safe_message(
                    CursorChatLimitError,
                    "Cursor chat message blob is too large",
                )
            lengths[blob_id] = length
        if len(rows) != len(batch):
            raise _safe_message(CursorChatBlobError, "Cursor chat message blob is missing")
    for length in lengths.values():
        total += length
        if total > MAX_MESSAGE_BYTES:
            raise _safe_message(CursorChatLimitError, "Cursor chat messages are too large")

    blobs: Dict[str, bytes] = {}
    for start in range(0, len(unique_ids), MAX_SQL_BATCH):
        batch = unique_ids[start : start + MAX_SQL_BATCH]
        placeholders = ",".join("?" for _ in batch)
        try:
            rows = connection.execute(
                "SELECT id, data FROM blobs WHERE id IN ({})".format(placeholders),
                batch,
            ).fetchall()
        except sqlite3.DatabaseError:
            raise _safe_message(CursorChatSchemaError, "Cursor chat message blobs cannot be read")
        for blob_id, data in rows:
            if (
                not isinstance(blob_id, str)
                or blob_id not in lengths
                or not isinstance(data, bytes)
                or len(data) != lengths[blob_id]
                or hashlib.sha256(data).hexdigest() != blob_id
            ):
                raise _safe_message(CursorChatBlobError, "Cursor chat message hash does not match")
            blobs[blob_id] = data
        if len(rows) != len(batch):
            raise _safe_message(CursorChatBlobError, "Cursor chat message blob is missing")
    return MappingProxyType(blobs)


def _validate_message(raw: bytes) -> Mapping[str, FrozenJSON]:
    decoded = _strict_json(raw, CursorChatBlobError)
    if not isinstance(decoded, Mapping):
        raise _safe_message(CursorChatBlobError, "Cursor chat message is not an object")
    role = decoded.get("role")
    content = decoded.get("content")
    if role not in ("system", "user", "assistant", "tool"):
        raise _safe_message(CursorChatBlobError, "Cursor chat message role is invalid")
    if isinstance(content, str):
        pass
    elif isinstance(content, list):
        if len(content) > MAX_CONTENT_BLOCKS:
            raise _safe_message(CursorChatLimitError, "Cursor chat has too many content blocks")
        if any(not isinstance(block, Mapping) for block in content):
            raise _safe_message(CursorChatBlobError, "Cursor chat content block is invalid")
    else:
        raise _safe_message(CursorChatBlobError, "Cursor chat message content is invalid")
    frozen = _freeze_json(decoded)
    if not isinstance(frozen, Mapping):
        raise _safe_message(CursorChatBlobError, "Cursor chat message is invalid")
    return frozen


def _message_text(message: Mapping[str, FrozenJSON]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, tuple):
        return ""
    parts: List[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        text = block.get("text")
        if isinstance(text, str) and block.get("type") in (None, "text"):
            parts.append(text)
    return "\n".join(parts)


def _snip(value: str, limit: int = 160) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def extract_title(
    messages: Sequence[Mapping[str, FrozenJSON]],
    fallback: str = "",
) -> str:
    """Extract the first real user query, ignoring generated user context."""

    fallback_user = ""
    for message in messages:
        if message.get("role") != "user":
            continue
        text = _message_text(message)
        match = _USER_QUERY.search(text)
        if match and match.group(1).strip():
            return _snip(match.group(1))
        candidate = _USER_INFO.sub("", text).strip()
        if candidate and not fallback_user:
            fallback_user = candidate
    return _snip(fallback_user or fallback or "Cursor chat")


def _read_state(
    connection: sqlite3.Connection,
    root_blob_id: Optional[str],
) -> CursorChatState:
    _validate_schema(connection)
    try:
        rows = connection.execute(
            "SELECT value, typeof(key), typeof(value) FROM meta WHERE key = ?",
            ("0",),
        ).fetchall()
    except sqlite3.DatabaseError:
        raise _safe_message(CursorChatSchemaError, "Cursor chat metadata cannot be read")
    if len(rows) != 1:
        raise _safe_message(CursorChatMetadataError, "Cursor chat metadata record is missing")
    value, key_type, value_type = rows[0]
    if key_type != "text":
        raise _safe_message(CursorChatSchemaError, "Cursor chat meta key must be TEXT")
    metadata = _decode_metadata(value, value_type)
    selected_root = (
        _validate_blob_id(root_blob_id, CursorChatBlobError)
        if root_blob_id is not None
        else metadata.latest_root_blob_id
    )
    root_data = _fetch_blob(connection, selected_root, MAX_ROOT_BLOB_BYTES)
    message_ids, provisional_raw, workspace_raw = _scan_root(root_data)
    blobs = _fetch_message_blobs(connection, message_ids)
    decoded_by_id = {
        blob_id: _validate_message(data)
        for blob_id, data in blobs.items()
    }
    messages = tuple(decoded_by_id[blob_id] for blob_id in message_ids)
    provisional = (
        _validate_message(provisional_raw)
        if provisional_raw is not None
        else None
    )
    provisional_hash = (
        hashlib.sha256(provisional_raw).hexdigest()
        if provisional_raw is not None
        else None
    )
    return CursorChatState(
        metadata=metadata,
        root_blob_id=selected_root,
        message_ids=message_ids,
        messages=messages,
        provisional=provisional,
        provisional_hash=provisional_hash,
        project=decode_file_uri(workspace_raw),
        created_at=metadata.created_at,
        title=extract_title(messages, metadata.name),
    )


def _read_stable_copy(
    path: Union[str, os.PathLike],
    reader: Callable[[sqlite3.Connection], ReadResult],
) -> ReadResult:
    source_db = _normalize_path(path)
    source_wal = Path(str(source_db) + "-wal")
    for _attempt in range(SNAPSHOT_ATTEMPTS):
        before_db = _regular_signature(source_db, MAX_DB_BYTES, required=True)
        before_wal = _regular_signature(source_wal, MAX_WAL_BYTES, required=False)
        if before_db is None:
            raise _safe_message(CursorChatSourceError, "Cursor chat database is missing")
        copy_failed = False
        with tempfile.TemporaryDirectory(prefix="cursor-chat-") as temporary:
            os.chmod(temporary, 0o700)
            copied_db = Path(temporary) / "snapshot.db"
            copied_wal = Path(str(copied_db) + "-wal")
            try:
                db_bytes = _copy_regular_file(source_db, copied_db, before_db[2])
                wal_bytes = (
                    _copy_regular_file(source_wal, copied_wal, before_wal[2])
                    if before_wal is not None
                    else 0
                )
            except OSError:
                copy_failed = True
                db_bytes = -1
                wal_bytes = -1

            try:
                after_db = _regular_signature(source_db, MAX_DB_BYTES, required=True)
                after_wal = _regular_signature(source_wal, MAX_WAL_BYTES, required=False)
            except CursorChatSourceError:
                after_db = None
                after_wal = None
            if (
                copy_failed
                or before_db != after_db
                or before_wal != after_wal
                or db_bytes != before_db[2]
                or (
                    before_wal is not None
                    and wal_bytes != before_wal[2]
                )
            ):
                continue

            connection: Optional[sqlite3.Connection] = None
            try:
                uri = copied_db.as_uri() + "?mode=ro"
                connection = sqlite3.connect(uri, uri=True)
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
                return reader(connection)
            except CursorChatError:
                raise
            except sqlite3.DatabaseError:
                raise _safe_message(
                    CursorChatSourceError,
                    "Cursor chat snapshot cannot be opened",
                )
            finally:
                if connection is not None:
                    connection.close()
    raise _safe_message(CursorChatBusyError, "Cursor chat changed during snapshot")


def _source_signature(path: Union[str, os.PathLike]) -> SourceSignature:
    source_db = _normalize_path(path)
    source_wal = Path(str(source_db) + "-wal")
    database = _regular_signature(source_db, MAX_DB_BYTES, required=True)
    if database is None:
        raise _safe_message(CursorChatSourceError, "Cursor chat database is missing")
    return (
        database,
        _regular_signature(source_wal, MAX_WAL_BYTES, required=False),
    )


def snapshot_cursor_chat(
    path: Union[str, os.PathLike],
    *,
    root_blob_id: Optional[str] = None,
) -> CursorChatState:
    """Return a decoded stable snapshot without opening any source SQLite file."""

    return _read_stable_copy(
        path,
        lambda connection: _read_state(connection, root_blob_id),
    )


def _snapshot_message_ids(
    path: Union[str, os.PathLike],
    root_blob_id: str,
) -> Tuple[str, ...]:
    validated_id = _validate_blob_id(root_blob_id, CursorChatBlobError)

    def read_ids(connection: sqlite3.Connection) -> Tuple[str, ...]:
        _validate_schema(connection)
        root_data = _fetch_blob(connection, validated_id, MAX_ROOT_BLOB_BYTES)
        message_ids, _provisional, _workspace = _scan_root(root_data)
        return message_ids

    return _read_stable_copy(path, read_ids)


def _prefix_hash(message_ids: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for blob_id in message_ids:
        digest.update(bytes.fromhex(blob_id))
    return digest.hexdigest()


class CursorChatFollower:
    """Follow logical root revisions rather than SQLite byte offsets."""

    def __init__(
        self,
        path: Union[str, os.PathLike],
        *,
        from_start: bool = False,
        max_records: int = DEFAULT_FOLLOW_RECORDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or max_records <= 0
            or max_records > MAX_PENDING_RECORDS
        ):
            raise ValueError("max_records is outside the follower bound")
        self.path = _normalize_path(path)
        self.from_start = bool(from_start)
        self.max_records = max_records
        self._clock = clock
        self._root_blob_id: Optional[str] = None
        self._message_count = 0
        self._prefix_hash = _EMPTY_PREFIX_HASH
        self._provisional_hash: Optional[str] = None
        self._initialized = False
        self._historical_count = 0
        self._message_ids: Optional[Tuple[str, ...]] = ()
        self._pending = False
        self._last_source_signature: Optional[SourceSignature] = None
        self.last_error: Optional[CursorChatError] = None

    @property
    def has_pending_records(self) -> bool:
        return self._pending

    def export_checkpoint(self) -> Dict[str, object]:
        checkpoint: Dict[str, object] = {
            "version": 1,
            "kind": "cursor_chat",
            "path": str(self.path),
            "root_blob_id": self._root_blob_id,
            "message_count": self._message_count,
            "prefix_hash": self._prefix_hash,
            "provisional_hash": self._provisional_hash,
            "initialized": self._initialized,
            "historical_count": self._historical_count,
        }
        encoded = json.dumps(checkpoint, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_CHECKPOINT_BYTES:
            raise CursorChatLimitError("Cursor chat checkpoint exceeds its limit")
        return checkpoint

    def restore_checkpoint(self, checkpoint: Mapping[str, object]) -> bool:
        allowed_keys = {
            "version",
            "kind",
            "path",
            "root_blob_id",
            "message_count",
            "prefix_hash",
            "provisional_hash",
            "initialized",
            "historical_count",
        }
        if (
            not isinstance(checkpoint, Mapping)
            or len(checkpoint) > len(allowed_keys)
            or any(key not in allowed_keys for key in checkpoint)
            or checkpoint.get("version") != 1
            or checkpoint.get("kind") != "cursor_chat"
            or checkpoint.get("path") != str(self.path)
        ):
            return False
        root_blob_id = checkpoint.get("root_blob_id")
        message_count = checkpoint.get("message_count")
        prefix_hash = checkpoint.get("prefix_hash")
        provisional_hash = checkpoint.get("provisional_hash")
        initialized = checkpoint.get("initialized")
        historical_count = checkpoint.get("historical_count", 0)
        if (
            (root_blob_id is not None and not _is_hash(root_blob_id))
            or not _is_count(message_count)
            or message_count > MAX_MESSAGE_REFERENCES
            or not _is_hash(prefix_hash)
            or (provisional_hash is not None and not _is_hash(provisional_hash))
            or not isinstance(initialized, bool)
            or not _is_count(historical_count)
            or historical_count > MAX_MESSAGE_REFERENCES
        ):
            return False
        if (
            not initialized
            and (
                root_blob_id is not None
                or message_count != 0
                or prefix_hash != _EMPTY_PREFIX_HASH
                or provisional_hash is not None
                or historical_count != 0
            )
        ):
            return False
        try:
            encoded = json.dumps(dict(checkpoint), default=str).encode("utf-8")
        except (TypeError, ValueError):
            return False
        if len(encoded) > MAX_CHECKPOINT_BYTES:
            return False
        self._root_blob_id = root_blob_id
        self._message_count = message_count
        self._prefix_hash = prefix_hash
        self._provisional_hash = provisional_hash
        self._initialized = initialized
        self._historical_count = historical_count
        self._message_ids = None if initialized else ()
        self._pending = False
        self._last_source_signature = None
        self.last_error = None
        return True

    def _anchor(self, state: CursorChatState) -> None:
        self._root_blob_id = state.root_blob_id
        self._message_count = len(state.message_ids)
        self._prefix_hash = _prefix_hash(state.message_ids)
        self._provisional_hash = state.provisional_hash
        self._message_ids = state.message_ids
        self._pending = False

    def _metadata(
        self,
        state: CursorChatState,
        *,
        kind: str,
        synthetic: bool,
        provisional: bool,
        observed_at: float,
        timestamp_source: str,
        message_id: Optional[str] = None,
    ) -> Dict[str, object]:
        return {
            "source": "cursor-cli",
            "kind": kind,
            "synthetic": synthetic,
            "provisional": provisional,
            "root_blob_id": state.root_blob_id,
            "message_id": message_id,
            "timestamp_source": timestamp_source,
            "session_created_at": state.created_at,
            "observed_at": observed_at,
        }

    def _durable_record(
        self,
        state: CursorChatState,
        index: int,
        observed_at: float,
    ) -> Dict[str, object]:
        record = dict(_thaw_json(state.messages[index]))
        historical = index < self._historical_count
        use_created = historical and math.isfinite(state.created_at)
        timestamp = state.created_at if use_created else observed_at
        source = "session_created" if use_created else "observed"
        record.setdefault("timestamp", timestamp)
        record["synthetic"] = False
        record["provisional"] = False
        record["_cursor_chat"] = self._metadata(
            state,
            kind="message",
            synthetic=False,
            provisional=False,
            observed_at=observed_at,
            timestamp_source=source,
            message_id=state.message_ids[index],
        )
        return record

    def _provisional_record(
        self,
        state: CursorChatState,
        observed_at: float,
    ) -> Dict[str, object]:
        if state.provisional is None:
            record: Dict[str, object] = {
                "type": "cursor_chat_provisional",
                "role": "assistant",
                "content": "",
                "provisional_state": "cleared",
            }
        else:
            record = dict(_thaw_json(state.provisional))
            record["type"] = "cursor_chat_provisional"
            record["provisional_state"] = "updated"
        record.setdefault("timestamp", observed_at)
        record["synthetic"] = True
        record["provisional"] = True
        record["_cursor_chat"] = self._metadata(
            state,
            kind="provisional",
            synthetic=True,
            provisional=True,
            observed_at=observed_at,
            timestamp_source="observed",
        )
        return record

    def _reset_record(
        self,
        state: CursorChatState,
        observed_at: float,
    ) -> Dict[str, object]:
        return {
            "type": "session_reset",
            "role": "system",
            "content": "Cursor chat history reset",
            "timestamp": observed_at,
            "synthetic": True,
            "provisional": False,
            "_cursor_chat": self._metadata(
                state,
                kind="session_reset",
                synthetic=True,
                provisional=False,
                observed_at=observed_at,
                timestamp_source="observed",
            ),
        }

    def _previous_ids(self) -> Optional[Tuple[str, ...]]:
        if self._message_ids is not None:
            return self._message_ids
        if self._root_blob_id is None:
            return ()
        previous = _snapshot_message_ids(
            self.path,
            self._root_blob_id,
        )
        return previous[: self._message_count]

    def _emit_append(
        self,
        state: CursorChatState,
        observed_at: float,
    ) -> List[Dict[str, object]]:
        records: List[Dict[str, object]] = []
        end = min(len(state.message_ids), self._message_count + self.max_records)
        for index in range(self._message_count, end):
            records.append(self._durable_record(state, index, observed_at))
        self._message_count = end
        self._prefix_hash = _prefix_hash(state.message_ids[:end])
        self._message_ids = state.message_ids[:end]
        self._root_blob_id = state.root_blob_id
        if end < len(state.message_ids):
            self._pending = True
            return records

        if (
            state.provisional_hash != self._provisional_hash
            and len(records) < self.max_records
        ):
            records.append(self._provisional_record(state, observed_at))
            self._provisional_hash = state.provisional_hash
        self._pending = state.provisional_hash != self._provisional_hash
        return records

    def poll(self) -> List[Dict[str, object]]:
        saved_state = (
            self._root_blob_id,
            self._message_count,
            self._prefix_hash,
            self._provisional_hash,
            self._initialized,
            self._historical_count,
            self._message_ids,
            self._pending,
            self._last_source_signature,
        )
        try:
            before_signature = _source_signature(self.path)
            if (
                self._last_source_signature is not None
                and before_signature == self._last_source_signature
                and not self._pending
            ):
                self.last_error = None
                return []
            state = snapshot_cursor_chat(self.path)
            after_signature = _source_signature(self.path)
            self._last_source_signature = (
                before_signature
                if before_signature == after_signature
                else None
            )
            observed_at = float(self._clock())
            if not math.isfinite(observed_at):
                observed_at = time.time()
            if not self._initialized:
                self._initialized = True
                if not self.from_start:
                    self._historical_count = 0
                    self._anchor(state)
                    self.last_error = None
                    return []
                self._historical_count = len(state.message_ids)

            prefix_matches = (
                len(state.message_ids) >= self._message_count
                and _prefix_hash(state.message_ids[: self._message_count])
                == self._prefix_hash
            )
            if prefix_matches:
                records = self._emit_append(state, observed_at)
                self.last_error = None
                return records

            try:
                previous_ids = self._previous_ids()
            except CursorChatBusyError:
                raise
            except CursorChatError:
                previous_ids = None
            if previous_ids is None:
                record = self._reset_record(state, observed_at)
                self._historical_count = 0
                self._anchor(state)
                self.last_error = None
                return [record]

            limit = min(len(previous_ids), len(state.message_ids))
            lcp = 0
            while lcp < limit and previous_ids[lcp] == state.message_ids[lcp]:
                lcp += 1
            self._message_count = lcp
            self._prefix_hash = _prefix_hash(state.message_ids[:lcp])
            self._message_ids = state.message_ids[:lcp]
            self._historical_count = min(self._historical_count, lcp)
            records = self._emit_append(state, observed_at)
            self.last_error = None
            return records
        except CursorChatError as error:
            (
                self._root_blob_id,
                self._message_count,
                self._prefix_hash,
                self._provisional_hash,
                self._initialized,
                self._historical_count,
                self._message_ids,
                self._pending,
                self._last_source_signature,
            ) = saved_state
            self.last_error = error
            return []


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _HEX_ID.fullmatch(value) is not None


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = [
    "COPY_CHUNK_BYTES",
    "DEFAULT_FOLLOW_RECORDS",
    "MAX_CHECKPOINT_BYTES",
    "MAX_CONTENT_BLOCKS",
    "MAX_DB_BYTES",
    "MAX_JSON_DEPTH",
    "MAX_JSON_NODES",
    "MAX_MESSAGE_BLOB_BYTES",
    "MAX_MESSAGE_BYTES",
    "MAX_MESSAGE_REFERENCES",
    "MAX_META_BYTES",
    "MAX_PENDING_RECORDS",
    "MAX_PROTO_FIELDS",
    "MAX_PROTO_PAYLOAD_BYTES",
    "MAX_ROOT_BLOB_BYTES",
    "MAX_SQL_BATCH",
    "MAX_WAL_BYTES",
    "MAX_WORKSPACE_URI_BYTES",
    "SNAPSHOT_ATTEMPTS",
    "CursorChatBlobError",
    "CursorChatBusyError",
    "CursorChatError",
    "CursorChatFollower",
    "CursorChatLimitError",
    "CursorChatMetadata",
    "CursorChatMetadataError",
    "CursorChatProtobufError",
    "CursorChatSchemaError",
    "CursorChatSourceError",
    "CursorChatState",
    "decode_file_uri",
    "extract_title",
    "snapshot_cursor_chat",
]
