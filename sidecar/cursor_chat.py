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
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, fields, is_dataclass
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

from sidecar.json_limits import (
    JSONLimitError,
    JSONLimits,
    JSONSyntaxError,
    parse_json,
    validate_json,
)
from sidecar.text_utils import extract_cursor_title


SNAPSHOT_ATTEMPTS = 3
SNAPSHOT_BACKOFF_INITIAL_SECONDS = 0.002
SNAPSHOT_BACKOFF_MAX_SECONDS = 0.008
DEFAULT_SNAPSHOT_CACHE_ENTRIES = 64
DEFAULT_SNAPSHOT_CACHE_TTL_SECONDS = 2.0
DEFAULT_SNAPSHOT_HARD_ENTRIES = 128
DEFAULT_SNAPSHOT_CACHE_BYTES = 64 * 1024 * 1024
DEFAULT_SNAPSHOT_HARD_BYTES = 128 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_ENTRY_BYTES = 64 * 1024 * 1024
DEFAULT_SNAPSHOT_MAX_IN_FLIGHT = 2
DEFAULT_SNAPSHOT_MAX_IN_FLIGHT_SOURCE_BYTES = 640 * 1024 * 1024
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

_GLOBAL_SNAPSHOT_SLOTS = threading.BoundedSemaphore(
    DEFAULT_SNAPSHOT_MAX_IN_FLIGHT
)

_HEX_ID = re.compile(r"^[0-9a-f]{64}$")
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_MAX_FIELD_NUMBER = (1 << 29) - 1
_EMPTY_PREFIX_HASH = hashlib.sha256(b"").hexdigest()

FileSignature = Tuple[int, int, int, int, int]
# Existence, inode, mtime_ns, size, device, and ctime_ns.  The first four
# fields are the portable invalidation contract; device/ctime harden races.
SourceFileSignature = Tuple[bool, int, int, int, int, int]
SourceSignature = Tuple[SourceFileSignature, SourceFileSignature]
StableCopySignature = Tuple[FileSignature, Optional[FileSignature]]
SnapshotCacheKey = Tuple[str, SourceSignature]
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


def _source_signature_bytes(signature: SourceSignature) -> int:
    return sum(part[3] for part in signature if part[0])


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


_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_EMPTY_DICT_BYTES = sys.getsizeof({})
_DICT_SLOT_BYTES = max(1, sys.getsizeof({None: None}) - _EMPTY_DICT_BYTES)


def _retained_size(value: object, seen: Optional[set] = None) -> int:
    """Conservatively count recursively retained Python heap bytes."""

    visited = set() if seen is None else seen
    identity = id(value)
    if identity in visited:
        return 0
    visited.add(identity)
    size = sys.getsizeof(value)
    if isinstance(value, Mapping):
        if isinstance(value, _MAPPING_PROXY_TYPE):
            # mappingproxy retains a separate underlying dict whose capacity
            # is not included by getsizeof(mappingproxy).
            size += _EMPTY_DICT_BYTES + len(value) * _DICT_SLOT_BYTES
        for key, item in value.items():
            size += _retained_size(key, visited)
            size += _retained_size(item, visited)
        return size
    if isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            size += _retained_size(item, visited)
        return size
    if is_dataclass(value) and not isinstance(value, type):
        try:
            size += sys.getsizeof(vars(value))
        except TypeError:
            pass
        for field in fields(value):
            size += _retained_size(getattr(value, field.name), visited)
    return size


def cursor_chat_state_weight(state: CursorChatState) -> int:
    """Return conservative retained bytes for one immutable decoded state."""

    if not isinstance(state, CursorChatState):
        raise TypeError("state must be CursorChatState")
    return _retained_size(state)


def _cache_entry_weight(key: SnapshotCacheKey, state: CursorChatState) -> int:
    return (
        _retained_size(key)
        + cursor_chat_state_weight(state)
        + sys.getsizeof(_SnapshotCacheEntry)
    )


@dataclass(frozen=True)
class CursorChatSnapshotStats:
    """Immutable performance counters for one bounded snapshot broker."""

    signature_checks: int
    cache_hits: int
    cache_misses: int
    snapshot_loads: int
    coalesced_waits: int
    evictions: int
    expirations: int
    errors: int
    entries: int
    in_flight: int
    cache_bytes: int
    peak_cache_bytes: int
    oversized_states: int
    in_flight_source_bytes: int
    peak_in_flight_source_bytes: int
    budget_waits: int
    generation: int


@dataclass(frozen=True)
class _SnapshotCacheEntry:
    state: CursorChatState
    expires_at: float
    generation: int
    weight_bytes: int


class _SnapshotFlight:
    def __init__(self, generation: int, source_bytes: int) -> None:
        self.generation = generation
        self.source_bytes = source_bytes
        self.event = threading.Event()
        self.state: Optional[CursorChatState] = None
        self.error: Optional[BaseException] = None


class _ScanGenerationToken:
    def __init__(self, broker: "CursorChatSnapshotBroker") -> None:
        self._broker = broker
        self._entered = False

    def __enter__(self) -> int:
        if self._entered:
            raise RuntimeError("Cursor chat scan generation token was reused")
        self._entered = True
        return self._broker._enter_scan_generation()

    def __exit__(
        self,
        exception_type: Optional[type],
        _exception: Optional[BaseException],
        _traceback: object,
    ) -> bool:
        if not self._entered:
            return False
        self._entered = False
        try:
            self._broker._exit_scan_generation()
        except BaseException:
            if exception_type is None:
                raise
        return False


class CursorChatSnapshotBroker:
    """Bounded, thread-safe cache and single-flight broker for decoded chats."""

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_SNAPSHOT_CACHE_ENTRIES,
        hard_max_entries: Optional[int] = None,
        max_cache_bytes: int = DEFAULT_SNAPSHOT_CACHE_BYTES,
        hard_max_cache_bytes: int = DEFAULT_SNAPSHOT_HARD_BYTES,
        max_entry_bytes: int = DEFAULT_SNAPSHOT_MAX_ENTRY_BYTES,
        ttl_seconds: float = DEFAULT_SNAPSHOT_CACHE_TTL_SECONDS,
        max_in_flight: int = DEFAULT_SNAPSHOT_MAX_IN_FLIGHT,
        max_in_flight_source_bytes: int = (
            DEFAULT_SNAPSHOT_MAX_IN_FLIGHT_SOURCE_BYTES
        ),
        clock: Callable[[], float] = time.monotonic,
        publication_probe: Optional[Callable[[], None]] = None,
    ) -> None:
        if (
            not isinstance(max_entries, int)
            or isinstance(max_entries, bool)
            or max_entries <= 0
        ):
            raise ValueError("snapshot cache size must be positive")
        if not math.isfinite(float(ttl_seconds)) or ttl_seconds <= 0:
            raise ValueError("snapshot cache TTL must be positive")
        hard_bound = (
            max(DEFAULT_SNAPSHOT_HARD_ENTRIES, max_entries)
            if hard_max_entries is None
            else hard_max_entries
        )
        if (
            not isinstance(hard_bound, int)
            or isinstance(hard_bound, bool)
            or hard_bound < max_entries
        ):
            raise ValueError("snapshot hard bound must cover soft capacity")
        if (
            not isinstance(max_in_flight, int)
            or isinstance(max_in_flight, bool)
            or max_in_flight <= 0
        ):
            raise ValueError("snapshot in-flight bound must be positive")
        byte_bounds = (
            max_cache_bytes,
            hard_max_cache_bytes,
            max_entry_bytes,
            max_in_flight_source_bytes,
        )
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            for value in byte_bounds
        ):
            raise ValueError("snapshot byte bounds must be positive integers")
        if hard_max_cache_bytes < max_cache_bytes:
            raise ValueError("snapshot hard bytes must cover soft bytes")
        if max_entry_bytes > hard_max_cache_bytes:
            raise ValueError("snapshot entry bytes exceed hard cache bytes")
        self.max_entries = max_entries
        self.hard_max_entries = hard_bound
        self.max_cache_bytes = max_cache_bytes
        self.hard_max_cache_bytes = hard_max_cache_bytes
        self.max_entry_bytes = max_entry_bytes
        self.ttl_seconds = float(ttl_seconds)
        self.max_in_flight = max_in_flight
        self.max_in_flight_source_bytes = max_in_flight_source_bytes
        self.source_reservation_bytes = min(
            MAX_DB_BYTES + MAX_WAL_BYTES,
            max_in_flight_source_bytes,
        )
        self._clock = clock
        self._publication_probe = publication_probe
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._entries: OrderedDict[SnapshotCacheKey, _SnapshotCacheEntry] = (
            OrderedDict()
        )
        self._flights: Dict[Tuple[int, SnapshotCacheKey], _SnapshotFlight] = {}
        self._generation = 0
        self._scan_depth = 0
        self._signature_checks = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._snapshot_loads = 0
        self._coalesced_waits = 0
        self._evictions = 0
        self._expirations = 0
        self._errors = 0
        self._cache_bytes = 0
        self._peak_cache_bytes = 0
        self._oversized_states = 0
        self._in_flight_source_bytes = 0
        self._peak_in_flight_source_bytes = 0
        self._budget_waits = 0

    def _expire_locked(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._entries.items()
            if (
                entry.expires_at <= now
                and not (
                    self._scan_depth > 0
                    and entry.generation == self._generation
                )
            )
        ]
        for key in expired:
            self._remove_entry_locked(key)
        self._expirations += len(expired)

    def _remove_entry_locked(
        self,
        key: SnapshotCacheKey,
    ) -> Optional[_SnapshotCacheEntry]:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._cache_bytes = max(
                0,
                self._cache_bytes - entry.weight_bytes,
            )
        return entry

    def _over_soft_locked(self) -> bool:
        return (
            len(self._entries) > self.max_entries
            or self._cache_bytes > self.max_cache_bytes
        )

    def _store_locked(
        self,
        key: SnapshotCacheKey,
        state: CursorChatState,
        weight_bytes: int,
        now: float,
        generation: int,
    ) -> bool:
        if generation != self._generation:
            return False
        self._remove_entry_locked(key)
        if (
            weight_bytes > self.max_entry_bytes
            or weight_bytes > self.hard_max_cache_bytes
        ):
            self._oversized_states += 1
            return False
        while (
            len(self._entries) + 1 > self.hard_max_entries
            or self._cache_bytes + weight_bytes > self.hard_max_cache_bytes
        ):
            oldest = next(iter(self._entries))
            self._remove_entry_locked(oldest)
            self._evictions += 1
        self._entries[key] = _SnapshotCacheEntry(
            state=state,
            expires_at=now + self.ttl_seconds,
            generation=generation,
            weight_bytes=weight_bytes,
        )
        self._cache_bytes += weight_bytes
        self._peak_cache_bytes = max(
            self._peak_cache_bytes,
            self._cache_bytes,
        )
        self._entries.move_to_end(key)
        while self._over_soft_locked():
            evictable = next(
                (
                    candidate
                    for candidate, entry in self._entries.items()
                    if not (
                        self._scan_depth > 0
                        and entry.generation == self._generation
                    )
                ),
                None,
            )
            if evictable is None:
                break
            self._remove_entry_locked(evictable)
            self._evictions += 1
        # An active generation pins its entries above the soft capacity.  The
        # unconditional hard ceiling was enforced before insertion.
        return key in self._entries

    def _trim_soft_locked(self) -> None:
        while self._over_soft_locked():
            oldest = next(iter(self._entries))
            self._remove_entry_locked(oldest)
            self._evictions += 1

    def _clock_value(self) -> float:
        try:
            value = float(self._clock())
        except BaseException:
            raise CursorChatSourceError("Cursor chat snapshot clock failed")
        if not math.isfinite(value):
            raise CursorChatSourceError("Cursor chat snapshot clock failed")
        return value

    @staticmethod
    def _publication_error(error: BaseException) -> CursorChatError:
        if isinstance(error, CursorChatError):
            return error
        return CursorChatSourceError("Cursor chat snapshot publication failed")

    def _fail_flight_locked(
        self,
        flight_key: Tuple[int, SnapshotCacheKey],
        flight: _SnapshotFlight,
        error: BaseException,
    ) -> None:
        self._errors += 1
        flight.state = None
        flight.error = error
        self._release_flight_locked(flight_key, flight)
        flight.event.set()
        self._condition.notify_all()

    def _release_flight_locked(
        self,
        flight_key: Tuple[int, SnapshotCacheKey],
        flight: _SnapshotFlight,
    ) -> None:
        current = self._flights.get(flight_key)
        if current is not flight:
            return
        self._flights.pop(flight_key, None)
        self._in_flight_source_bytes = max(
            0,
            self._in_flight_source_bytes - flight.source_bytes,
        )

    def _pin_entry_locked(
        self,
        key: SnapshotCacheKey,
        entry: _SnapshotCacheEntry,
        now: float,
    ) -> CursorChatState:
        pinned = _SnapshotCacheEntry(
            state=entry.state,
            expires_at=now + self.ttl_seconds,
            generation=self._generation,
            weight_bytes=entry.weight_bytes,
        )
        self._entries[key] = pinned
        self._entries.move_to_end(key)
        return pinned.state

    def _cache_hit_locked(
        self,
        key: SnapshotCacheKey,
        entry: _SnapshotCacheEntry,
        now: float,
    ) -> CursorChatState:
        self._cache_hits += 1
        return self._pin_entry_locked(key, entry, now)

    def _enter_scan_generation(self) -> int:
        now = self._clock_value()
        with self._lock:
            outermost = self._scan_depth == 0
            if outermost:
                self._generation += 1
            self._scan_depth += 1
            try:
                self._expire_locked(now)
            except BaseException:
                self._scan_depth -= 1
                if self._scan_depth == 0:
                    self._trim_soft_locked()
                raise
            return self._generation

    def _exit_scan_generation(self) -> None:
        clock_error: Optional[BaseException] = None
        try:
            now = self._clock_value()
        except BaseException as error:
            clock_error = error
            now = None
        with self._lock:
            if self._scan_depth <= 0:
                return
            self._scan_depth -= 1
            if self._scan_depth == 0:
                if now is not None:
                    self._expire_locked(now)
                self._trim_soft_locked()
        if clock_error is not None:
            raise clock_error

    def scan_generation(self) -> _ScanGenerationToken:
        """Return a nestable scope that pins this scan until final exit."""

        return _ScanGenerationToken(self)

    @property
    def scan_depth(self) -> int:
        with self._lock:
            return self._scan_depth

    def pin(self, key: SnapshotCacheKey) -> bool:
        """Pin an already-decoded store while discovery reuses its metadata."""

        now = self._clock_value()
        with self._lock:
            self._expire_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                return False
            self._pin_entry_locked(key, entry, now)
            return True

    @staticmethod
    def _raise_flight_error(error: BaseException) -> None:
        try:
            replacement = error.__class__(*error.args)
        except Exception:
            raise RuntimeError("Cursor chat snapshot flight failed")
        raise replacement

    def snapshot(
        self,
        path: Union[str, os.PathLike],
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> CursorChatState:
        """Return shared immutable state for the current DB/WAL signature."""

        source_path = _normalize_path(path)
        with self._lock:
            self._signature_checks += 1
        cache_key = self.cache_key(source_path)
        source_bytes = _source_signature_bytes(cache_key[1])
        now = self._clock_value()

        with self._lock:
            self._expire_locked(now)
            cached = self._entries.get(cache_key)
            if cached is not None:
                return self._cache_hit_locked(cache_key, cached, now)

            while True:
                generation = self._generation
                flight_key = (generation, cache_key)
                flight = self._flights.get(flight_key)
                if flight is not None:
                    self._coalesced_waits += 1
                    leader = False
                    break
                if source_bytes > self.source_reservation_bytes:
                    raise CursorChatLimitError(
                        "Cursor chat source exceeds snapshot in-flight budget"
                    )
                if (
                    len(self._flights) < self.max_in_flight
                    and self._in_flight_source_bytes
                    + self.source_reservation_bytes
                    <= self.max_in_flight_source_bytes
                ):
                    flight = _SnapshotFlight(
                        generation,
                        self.source_reservation_bytes,
                    )
                    self._flights[flight_key] = flight
                    self._in_flight_source_bytes += flight.source_bytes
                    self._peak_in_flight_source_bytes = max(
                        self._peak_in_flight_source_bytes,
                        self._in_flight_source_bytes,
                    )
                    self._cache_misses += 1
                    self._snapshot_loads += 1
                    leader = True
                    break
                self._budget_waits += 1
                self._condition.wait()
                current = self._clock_value()
                self._expire_locked(current)
                cached = self._entries.get(cache_key)
                if cached is not None:
                    return self._cache_hit_locked(
                        cache_key,
                        cached,
                        current,
                    )

        if not leader:
            flight.event.wait()
            if flight.error is not None:
                self._raise_flight_error(flight.error)
            if flight.state is None:
                raise RuntimeError("Cursor chat snapshot flight returned no state")
            return flight.state

        try:
            state, stable_signature = _snapshot_cursor_chat_uncached(
                source_path,
                sleeper=sleeper,
                source_byte_limit=flight.source_bytes,
            )
        except BaseException as error:
            with self._lock:
                self._fail_flight_locked(flight_key, flight, error)
            raise

        publication_key = (cache_key[0], stable_signature)
        try:
            publication_weight = _cache_entry_weight(
                publication_key,
                state,
            )
            publication_now = self._clock_value()
            with self._lock:
                if self._publication_probe is not None:
                    self._publication_probe()
                self._store_locked(
                    publication_key,
                    state,
                    publication_weight,
                    publication_now,
                    generation,
                )
                flight.state = state
                self._release_flight_locked(flight_key, flight)
                flight.event.set()
                self._condition.notify_all()
        except BaseException as error:
            typed_error = self._publication_error(error)
            with self._lock:
                published = self._entries.get(publication_key)
                if published is not None and published.state is state:
                    self._remove_entry_locked(publication_key)
                self._fail_flight_locked(
                    flight_key,
                    flight,
                    typed_error,
                )
            raise typed_error
        return state

    def cache_key(
        self,
        path: Union[str, os.PathLike],
    ) -> SnapshotCacheKey:
        """Return the canonical path and complete current DB/WAL signature."""

        source_path = _normalize_path(path)
        return (
            str(_canonical_store_path(source_path)),
            _source_signature(source_path),
        )

    @property
    def stats(self) -> CursorChatSnapshotStats:
        with self._lock:
            return CursorChatSnapshotStats(
                signature_checks=self._signature_checks,
                cache_hits=self._cache_hits,
                cache_misses=self._cache_misses,
                snapshot_loads=self._snapshot_loads,
                coalesced_waits=self._coalesced_waits,
                evictions=self._evictions,
                expirations=self._expirations,
                errors=self._errors,
                entries=len(self._entries),
                in_flight=len(self._flights),
                cache_bytes=self._cache_bytes,
                peak_cache_bytes=self._peak_cache_bytes,
                oversized_states=self._oversized_states,
                in_flight_source_bytes=self._in_flight_source_bytes,
                peak_in_flight_source_bytes=(
                    self._peak_in_flight_source_bytes
                ),
                budget_waits=self._budget_waits,
                generation=self._generation,
            )

    def reset(self) -> None:
        """Drop cached state and counters without disrupting active callers."""

        with self._lock:
            self._generation += 1
            self._scan_depth = 0
            self._entries.clear()
            self._cache_bytes = 0
            self._peak_cache_bytes = 0
            self._oversized_states = 0
            self._signature_checks = 0
            self._cache_hits = 0
            self._cache_misses = 0
            self._snapshot_loads = 0
            self._coalesced_waits = 0
            self._evictions = 0
            self._expirations = 0
            self._errors = 0
            self._peak_in_flight_source_bytes = self._in_flight_source_bytes
            self._budget_waits = 0
            self._condition.notify_all()


def _safe_message(error_type: type, message: str) -> CursorChatError:
    return error_type(message)


def _normalize_path(path: Union[str, os.PathLike]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _canonical_store_path(path: Union[str, os.PathLike]) -> Path:
    return Path(os.path.realpath(str(_normalize_path(path))))


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


def _strict_json(raw: bytes, error_type: type) -> Any:
    try:
        return parse_json(
            raw,
            JSONLimits(
                max_depth=MAX_JSON_DEPTH - 1,
                max_nodes=MAX_JSON_NODES,
            ),
        )
    except JSONLimitError as error:
        raise _cursor_json_limit(error) from error
    except JSONSyntaxError as error:
        if error.reason == "unicode":
            raise _safe_message(
                error_type,
                "Cursor chat JSON text is invalid",
            ) from error
        raise _safe_message(error_type, "Cursor chat JSON is malformed") from error


def _cursor_json_limit(error: JSONLimitError) -> CursorChatLimitError:
    if error.reason == "depth":
        return CursorChatLimitError("Cursor chat JSON is nested too deeply")
    if error.reason in ("items", "nodes"):
        return CursorChatLimitError("Cursor chat JSON has too many nodes")
    if error.reason == "string":
        return CursorChatLimitError("Cursor chat JSON text is too large")
    if error.reason == "integer":
        return CursorChatLimitError("Cursor chat JSON integer is too large")
    return CursorChatLimitError("Cursor chat JSON exceeds its size limit")


def _cursor_json_syntax(
    error: JSONSyntaxError,
    error_type: type,
) -> CursorChatError:
    if error.reason == "nonfinite":
        return _safe_message(error_type, "Cursor chat JSON number is invalid")
    if error.reason == "unicode":
        return _safe_message(error_type, "Cursor chat JSON text is invalid")
    if error.reason == "key":
        return _safe_message(error_type, "Cursor chat JSON key is invalid")
    return _safe_message(error_type, "Cursor chat JSON value is unsupported")


def _freeze_json(value: Any) -> FrozenJSON:
    try:
        validate_json(
            value,
            JSONLimits(
                max_depth=MAX_JSON_DEPTH - 1,
                max_nodes=MAX_JSON_NODES,
            ),
        )
    except JSONLimitError as error:
        raise _cursor_json_limit(error) from error
    except JSONSyntaxError as error:
        raise _cursor_json_syntax(error, CursorChatBlobError) from error

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


def extract_title(
    messages: Sequence[Mapping[str, FrozenJSON]],
    fallback: str = "",
) -> str:
    """Preserve the public decoder API while delegating title policy."""

    return extract_cursor_title(
        (
            _message_text(message)
            for message in messages
            if message.get("role") == "user"
        ),
        fallback or "Cursor chat",
    )


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


def _snapshot_retry_delay(attempt: int) -> float:
    return min(
        SNAPSHOT_BACKOFF_INITIAL_SECONDS * (2**attempt),
        SNAPSHOT_BACKOFF_MAX_SECONDS,
    )


def _read_stable_copy_with_signature_unlimited(
    path: Union[str, os.PathLike],
    reader: Callable[[sqlite3.Connection], ReadResult],
    *,
    sleeper: Callable[[float], None] = time.sleep,
    source_byte_limit: Optional[int] = None,
) -> Tuple[ReadResult, StableCopySignature]:
    source_db = _normalize_path(path)
    source_wal = Path(str(source_db) + "-wal")
    for attempt in range(SNAPSHOT_ATTEMPTS):
        before_db = _regular_signature(source_db, MAX_DB_BYTES, required=True)
        before_wal = _regular_signature(source_wal, MAX_WAL_BYTES, required=False)
        if before_db is None:
            raise _safe_message(CursorChatSourceError, "Cursor chat database is missing")
        source_bytes = before_db[2] + (
            before_wal[2] if before_wal is not None else 0
        )
        if source_byte_limit is not None and source_bytes > source_byte_limit:
            raise _safe_message(
                CursorChatLimitError,
                "Cursor chat source exceeds snapshot in-flight budget",
            )
        copy_error: Optional[OSError] = None
        with tempfile.TemporaryDirectory(prefix="cursor-chat-") as temporary:
            try:
                os.chmod(temporary, 0o700)
            except OSError:
                raise _safe_message(
                    CursorChatSourceError,
                    "Cursor chat snapshot cannot be prepared",
                )
            copied_db = Path(temporary) / "snapshot.db"
            copied_wal = Path(str(copied_db) + "-wal")
            try:
                db_bytes = _copy_regular_file(source_db, copied_db, before_db[2])
                wal_bytes = (
                    _copy_regular_file(source_wal, copied_wal, before_wal[2])
                    if before_wal is not None
                    else 0
                )
            except OSError as error:
                copy_error = error
                db_bytes = -1
                wal_bytes = -1

            # Missing/replaced source files are consistency races.  Other stat
            # failures and copy/read errors against unchanged signatures are
            # permanent for this call and must not be retried or delayed.
            after_db = _regular_signature(
                source_db,
                MAX_DB_BYTES,
                required=False,
            )
            after_wal = _regular_signature(
                source_wal,
                MAX_WAL_BYTES,
                required=False,
            )
            signature_race = before_db != after_db or before_wal != after_wal
            incomplete_copy = (
                db_bytes != before_db[2]
                or (
                    before_wal is not None
                    and wal_bytes != before_wal[2]
                )
            )
            if not signature_race and (copy_error is not None or incomplete_copy):
                raise _safe_message(
                    CursorChatSourceError,
                    "Cursor chat snapshot cannot be copied",
                )
            if signature_race:
                if attempt + 1 < SNAPSHOT_ATTEMPTS:
                    sleeper(_snapshot_retry_delay(attempt))
                continue

            connection: Optional[sqlite3.Connection] = None
            try:
                uri = copied_db.as_uri() + "?mode=ro"
                connection = sqlite3.connect(uri, uri=True)
                connection.execute("PRAGMA query_only = ON")
                connection.execute("PRAGMA trusted_schema = OFF")
                return reader(connection), (before_db, before_wal)
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


def _read_stable_copy_with_signature(
    path: Union[str, os.PathLike],
    reader: Callable[[sqlite3.Connection], ReadResult],
    *,
    sleeper: Callable[[float], None] = time.sleep,
    source_byte_limit: Optional[int] = None,
) -> Tuple[ReadResult, StableCopySignature]:
    """Serialize all copy/decode paths through the process hard slot count."""

    with _GLOBAL_SNAPSHOT_SLOTS:
        return _read_stable_copy_with_signature_unlimited(
            path,
            reader,
            sleeper=sleeper,
            source_byte_limit=source_byte_limit,
        )


def _read_stable_copy(
    path: Union[str, os.PathLike],
    reader: Callable[[sqlite3.Connection], ReadResult],
    *,
    sleeper: Callable[[float], None] = time.sleep,
) -> ReadResult:
    result, _signature = _read_stable_copy_with_signature(
        path,
        reader,
        sleeper=sleeper,
    )
    return result


def _source_file_signature(
    signature: Optional[FileSignature],
) -> SourceFileSignature:
    if signature is None:
        return (False, 0, 0, 0, 0, 0)
    device, inode, size, mtime_ns, ctime_ns = signature
    return (True, inode, mtime_ns, size, device, ctime_ns)


def _source_signature(path: Union[str, os.PathLike]) -> SourceSignature:
    source_db = _normalize_path(path)
    source_wal = Path(str(source_db) + "-wal")
    database = _regular_signature(source_db, MAX_DB_BYTES, required=True)
    if database is None:
        raise _safe_message(CursorChatSourceError, "Cursor chat database is missing")
    return (
        _source_file_signature(database),
        _source_file_signature(
            _regular_signature(source_wal, MAX_WAL_BYTES, required=False)
        ),
    )


def _snapshot_cursor_chat_uncached(
    path: Union[str, os.PathLike],
    *,
    sleeper: Callable[[float], None] = time.sleep,
    source_byte_limit: Optional[int] = None,
) -> Tuple[CursorChatState, SourceSignature]:
    state, stable_signature = _read_stable_copy_with_signature(
        path,
        lambda connection: _read_state(connection, None),
        sleeper=sleeper,
        source_byte_limit=source_byte_limit,
    )
    database, wal = stable_signature
    return state, (
        _source_file_signature(database),
        _source_file_signature(wal),
    )


_DEFAULT_SNAPSHOT_BROKER = CursorChatSnapshotBroker()


def default_snapshot_broker() -> CursorChatSnapshotBroker:
    """Return the process-local bounded broker shared by Cursor consumers."""

    return _DEFAULT_SNAPSHOT_BROKER


def reset_default_snapshot_broker() -> None:
    """Reset the shared broker for daemon lifecycle and isolated tests."""

    _DEFAULT_SNAPSHOT_BROKER.reset()


def snapshot_cursor_chat(
    path: Union[str, os.PathLike],
    *,
    root_blob_id: Optional[str] = None,
    broker: Optional[CursorChatSnapshotBroker] = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> CursorChatState:
    """Return a decoded stable snapshot without opening any source SQLite file."""

    if root_blob_id is None:
        selected_broker = broker if broker is not None else _DEFAULT_SNAPSHOT_BROKER
        return selected_broker.snapshot(path, sleeper=sleeper)
    return _read_stable_copy(
        path,
        lambda connection: _read_state(connection, root_blob_id),
        sleeper=sleeper,
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
        snapshot_broker: Optional[CursorChatSnapshotBroker] = None,
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
        self._snapshot_broker = (
            snapshot_broker
            if snapshot_broker is not None
            else _DEFAULT_SNAPSHOT_BROKER
        )
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
            state = snapshot_cursor_chat(
                self.path,
                broker=self._snapshot_broker,
            )
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
    "DEFAULT_SNAPSHOT_CACHE_BYTES",
    "DEFAULT_SNAPSHOT_CACHE_ENTRIES",
    "DEFAULT_SNAPSHOT_CACHE_TTL_SECONDS",
    "DEFAULT_SNAPSHOT_HARD_BYTES",
    "DEFAULT_SNAPSHOT_HARD_ENTRIES",
    "DEFAULT_SNAPSHOT_MAX_ENTRY_BYTES",
    "DEFAULT_SNAPSHOT_MAX_IN_FLIGHT",
    "DEFAULT_SNAPSHOT_MAX_IN_FLIGHT_SOURCE_BYTES",
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
    "SNAPSHOT_BACKOFF_INITIAL_SECONDS",
    "SNAPSHOT_BACKOFF_MAX_SECONDS",
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
    "CursorChatSnapshotBroker",
    "CursorChatSnapshotStats",
    "cursor_chat_state_weight",
    "decode_file_uri",
    "default_snapshot_broker",
    "extract_title",
    "reset_default_snapshot_broker",
    "snapshot_cursor_chat",
]
