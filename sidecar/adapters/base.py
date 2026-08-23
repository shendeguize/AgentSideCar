"""Adapter contract and bounded parsing helpers."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import stat
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    Hashable,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from sidecar.model import Event, Session, Status
from sidecar.text_utils import snip

Pathish = Union[str, os.PathLike]
FileSignature = Tuple[str, int, int]
Metadata = TypeVar("Metadata")
ContentBlockEvent = Tuple[str, Any, int]
ContentBlockRenderer = Callable[
    [Mapping[str, Any], str],
    Optional[ContentBlockEvent],
]
DEFAULT_METADATA_CACHE_SIZE = 4096


def as_mapping(value: Any) -> Mapping[str, Any]:
    """Return mapping values unchanged and all other values as an empty mapping."""

    return value if isinstance(value, Mapping) else {}


def file_signature(path: Pathish) -> Optional[FileSignature]:
    """Return a regular file's signature, or ``None`` if it is unavailable."""

    try:
        stat_result = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(stat_result.st_mode):
        return None
    mtime_ns = int(
        getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1e9))
    )
    return (
        os.path.abspath(os.fsdecode(path)),
        mtime_ns,
        int(stat_result.st_size),
    )


class MetadataCache(Generic[Metadata]):
    """Small LRU cache whose stale signatures can be pruned after a scan."""

    def __init__(self, max_entries: int = DEFAULT_METADATA_CACHE_SIZE) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.max_entries = int(max_entries)
        self._entries: "OrderedDict[Hashable, Metadata]" = OrderedDict()

    def get_or_load(
        self,
        signature: Hashable,
        loader: Callable[[], Metadata],
    ) -> Metadata:
        try:
            value = self._entries.pop(signature)
        except KeyError:
            value = loader()
        self._entries[signature] = value
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return value

    def prune(self, active_signatures: Iterable[Hashable]) -> None:
        active: Set[Hashable] = set(active_signatures)
        for signature in tuple(self._entries):
            if signature not in active:
                del self._entries[signature]

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


def _consume_csi(text: str, index: int) -> int:
    """Return the index after an ECMA-48 control sequence."""

    while index < len(text):
        codepoint = ord(text[index])
        if 0x40 <= codepoint <= 0x7E:
            return index + 1
        if 0x20 <= codepoint <= 0x3F:
            index += 1
            continue
        return index
    return index


def _consume_control_string(text: str, index: int, allow_bel: bool) -> int:
    """Return the index after an OSC/DCS/SOS/PM/APC control string."""

    while index < len(text):
        codepoint = ord(text[index])
        if allow_bel and codepoint == 0x07:
            return index + 1
        if codepoint == 0x9C:
            return index + 1
        if text[index] == "\x1b" and index + 1 < len(text) and text[index + 1] == "\\":
            return index + 2
        index += 1
    return index


def _strip_terminal_sequences(text: str) -> str:
    rendered: List[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        codepoint = ord(character)
        if character == "\x1b":
            if index + 1 >= len(text):
                break
            introducer = text[index + 1]
            if introducer == "[":
                index = _consume_csi(text, index + 2)
                continue
            if introducer in "]PX^_":
                index = _consume_control_string(
                    text,
                    index + 2,
                    allow_bel=introducer == "]",
                )
                continue

            # Other ESC sequences consist of zero or more intermediate bytes
            # followed by one final byte.
            cursor = index + 1
            while cursor < len(text) and 0x20 <= ord(text[cursor]) <= 0x2F:
                cursor += 1
            if cursor < len(text) and 0x30 <= ord(text[cursor]) <= 0x7E:
                cursor += 1
            index = cursor
            continue
        if codepoint == 0x9B:
            index = _consume_csi(text, index + 1)
            continue
        if codepoint in (0x90, 0x98, 0x9D, 0x9E, 0x9F):
            index = _consume_control_string(
                text,
                index + 1,
                allow_bel=codepoint == 0x9D,
            )
            continue
        rendered.append(character)
        index += 1
    return "".join(rendered)


def sanitize_terminal_text(value: Any, collapse_whitespace: bool = True) -> str:
    """Return inert terminal text while preserving ordinary Unicode.

    ECMA-48 escape/control strings and all C0/C1 control characters are
    removed. By default, whitespace runs are collapsed to one ASCII space so
    untrusted values remain on one terminal line.
    """

    text = _strip_terminal_sequences(str(value or ""))
    if collapse_whitespace:
        text = " ".join(text.split())
    return "".join(
        character
        for character in text
        if not (ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F)
    )


def read_json_object(path: Pathish, max_bytes: int) -> Dict[str, Any]:
    """Read one JSON object only when the complete file fits within ``max_bytes``."""

    if max_bytes <= 0:
        return {}
    try:
        with open(path, "rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError:
        return {}
    if len(raw) > max_bytes:
        return {}
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _jsonl_records(data: bytes, max_records: Optional[int]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for raw in data.splitlines():
        if not raw.strip():
            continue
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
            if max_records is not None and len(records) >= max_records:
                break
    return records


def read_jsonl_prefix(
    path: Pathish,
    max_bytes: int = 256 * 1024,
    max_records: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Read complete JSONL records from a bounded file prefix."""

    if max_bytes <= 0:
        return []
    try:
        with open(path, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            data = stream.read(max_bytes)
    except OSError:
        return []
    if size > len(data) and not data.endswith(b"\n"):
        complete, separator, _ = data.rpartition(b"\n")
        data = complete + separator if separator else b""
    return _jsonl_records(data, max_records)


def read_jsonl_tail(
    path: Pathish,
    max_bytes: int = 64 * 1024,
    max_records: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Read complete JSONL records from a bounded file tail."""

    if max_bytes <= 0:
        return []
    try:
        with open(path, "rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            start = max(0, size - max_bytes)
            previous = b"\n"
            if start:
                stream.seek(start - 1)
                previous = stream.read(1)
            stream.seek(start)
            data = stream.read(max_bytes)
    except OSError:
        return []
    if start and previous != b"\n":
        _, separator, data = data.partition(b"\n")
        if not separator:
            return []
    records = _jsonl_records(data, None)
    return records[-max_records:] if max_records is not None else records


def epoch_seconds(value: Any) -> Optional[float]:
    """Normalize numeric epoch seconds or milliseconds; reject booleans/non-numbers."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    epoch = float(value)
    return epoch / 1000.0 if abs(epoch) >= 100_000_000_000 else epoch


def timestamp_epoch(value: Any) -> Optional[float]:
    """Parse a supported timestamp as finite epoch seconds.

    Numeric values use seconds below the millisecond threshold and milliseconds
    at or above it. Numeric strings follow the same rule. ISO strings and
    datetimes retain explicit offsets; naive values are interpreted as UTC.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, dt.datetime):
        moment = value
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.timezone.utc)
        try:
            return moment.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, (int, float)):
        try:
            epoch = epoch_seconds(value)
        except (OverflowError, ValueError):
            return None
        return epoch if epoch is not None and math.isfinite(epoch) else None
    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip()
    try:
        number = float(raw)
    except ValueError:
        try:
            normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            moment = dt.datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return timestamp_epoch(moment)
    return timestamp_epoch(number)


def local_timestamp(value: Any = None, fallback: Optional[float] = None) -> str:
    """Convert epoch seconds/milliseconds or an ISO UTC value to local ISO time."""

    moment: Optional[dt.datetime] = None
    epoch = timestamp_epoch(value)
    if epoch is not None:
        try:
            moment = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            moment = None

    if moment is None:
        epoch = time.time() if fallback is None else fallback
        moment = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
    return moment.astimezone().isoformat(timespec="seconds")


def content_items(content: Any) -> List[Mapping[str, Any]]:
    """Normalize Anthropic-style string/list content into mapping items."""

    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, Mapping):
        return [content]
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        return [item for item in content if isinstance(item, Mapping)]
    return []


def content_block_events(
    content: Any,
    session: Session,
    timestamp: str,
    default_kind: str,
    extra: Mapping[str, Any],
    renderer: ContentBlockRenderer,
) -> List[Event]:
    """Build events from string/list content while delegating native block semantics."""

    if isinstance(content, str):
        blocks: Sequence[Any] = [{"type": "text", "text": content}]
    elif isinstance(content, Sequence) and not isinstance(
        content, (str, bytes, bytearray)
    ):
        blocks = content
    else:
        return []

    events: List[Event] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        rendered = renderer(block, default_kind)
        if rendered is None:
            continue
        kind, value, limit = rendered
        text = snip(value, limit)
        if text:
            events.append(
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    kind,
                    text,
                    dict(extra),
                )
            )
    return events


def text_content(value: Any) -> str:
    """Best-effort text extraction for nested tool results."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "content", "message", "output"):
            if key in value:
                text = text_content(value[key])
                if text:
                    return text
        return json.dumps(dict(value), ensure_ascii=False, default=str)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return " ".join(filter(None, (text_content(item) for item in value)))
    return str(value)


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class Adapter(ABC):
    """Pluggable reader for one or more on-disk agent formats."""

    name: str
    agent_names: Tuple[str, ...]

    @abstractmethod
    def discover(self, home: Path) -> Iterable[Session]:
        """Return sessions found below ``home`` without mutating their stores."""

    @abstractmethod
    def normalize(self, record: Mapping[str, Any], session: Session) -> Iterable[Event]:
        """Convert one native record into zero or more normalized events."""

    def infer_status(self, session: Session, now: Optional[float] = None) -> Optional[Status]:
        """Optionally infer status; the later state machine may provide it instead."""

        return None


__all__ = [
    "Adapter",
    "ContentBlockEvent",
    "ContentBlockRenderer",
    "DEFAULT_METADATA_CACHE_SIZE",
    "FileSignature",
    "MetadataCache",
    "Pathish",
    "as_mapping",
    "compact_json",
    "content_block_events",
    "content_items",
    "epoch_seconds",
    "file_signature",
    "local_timestamp",
    "read_json_object",
    "read_jsonl_prefix",
    "read_jsonl_tail",
    "sanitize_terminal_text",
    "snip",
    "text_content",
    "timestamp_epoch",
]
