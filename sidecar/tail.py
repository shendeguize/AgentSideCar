"""Canonical session tailing APIs for normalized local agent events."""

from __future__ import annotations

import json
import stat as stat_module
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple, cast

from sidecar.adapters import get_adapter
from sidecar.adapters.base import Adapter
from sidecar.model import Event, Session

DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_READ_BYTES = 64 * 1024
DEFAULT_LINE_BYTES = 1024 * 1024
DEFAULT_RECORDS = 256

DSHSignature = Tuple[int, int, int, int, int, int]


class JSONLFollower:
    """Follow a JSONL file by byte offset with bounded reads and line storage."""

    def __init__(
        self,
        path: Path,
        from_start: bool = False,
        max_read_bytes: int = DEFAULT_READ_BYTES,
        max_line_bytes: int = DEFAULT_LINE_BYTES,
        max_records: int = DEFAULT_RECORDS,
    ) -> None:
        if max_read_bytes <= 0 or max_line_bytes <= 0 or max_records <= 0:
            raise ValueError("follower bounds must be positive")
        self.path = Path(path).expanduser()
        self.from_start = bool(from_start)
        self.max_read_bytes = int(max_read_bytes)
        self.max_line_bytes = int(max_line_bytes)
        self.max_records = int(max_records)
        self.offset = 0
        self._identity: Optional[Tuple[int, int]] = None
        self._anchor = b""
        self._pending = bytearray()
        self._dropping_line = False
        self._records: Deque[Dict[str, object]] = deque()
        self._missing_at_start = False
        self._initialize()

    def _initialize(self) -> None:
        try:
            stat = self.path.stat()
        except OSError:
            self._missing_at_start = True
            return
        if not self.path.is_file():
            self._missing_at_start = True
            return
        self._identity = (stat.st_dev, stat.st_ino)
        self.offset = 0 if self.from_start else stat.st_size
        if self.offset:
            self._anchor = self._read_anchor(self.offset)
            if not self.from_start:
                self._capture_trailing_partial(self.offset)

    def _capture_trailing_partial(self, end: int) -> None:
        """Keep only the unterminated row at an initial EOF boundary."""

        amount = min(self.max_line_bytes + 1, end)
        try:
            with self.path.open("rb") as stream:
                stream.seek(end - amount)
                suffix = stream.read(amount)
        except OSError:
            return

        newline = suffix.rfind(b"\n")
        if newline >= 0:
            suffix = suffix[newline + 1 :]
        elif end > len(suffix):
            self._dropping_line = True
            return
        if len(suffix) > self.max_line_bytes:
            self._dropping_line = True
            return
        self._pending.extend(suffix)

    def _read_anchor(self, end: int) -> bytes:
        amount = min(64, end)
        if amount <= 0:
            return b""
        try:
            with self.path.open("rb") as stream:
                stream.seek(end - amount)
                return stream.read(amount)
        except OSError:
            return b""

    def _reset(self) -> None:
        self.offset = 0
        self._anchor = b""
        self._pending.clear()
        self._dropping_line = False
        self._records.clear()

    def _consume_line(self, raw: bytes) -> None:
        if not raw.strip() or len(raw) > self.max_line_bytes:
            return
        try:
            value = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeError):
            return
        if isinstance(value, dict):
            self._records.append(value)

    def _consume_chunk(self, chunk: bytes) -> None:
        remaining = chunk
        while remaining:
            newline = remaining.find(b"\n")
            if newline < 0:
                if self._dropping_line:
                    return
                if len(self._pending) + len(remaining) > self.max_line_bytes:
                    self._pending.clear()
                    self._dropping_line = True
                    return
                self._pending.extend(remaining)
                return

            segment = remaining[:newline]
            remaining = remaining[newline + 1 :]
            if self._dropping_line:
                self._dropping_line = False
                self._pending.clear()
                continue
            if len(self._pending) + len(segment) <= self.max_line_bytes:
                raw = bytes(self._pending) + segment
                self._consume_line(raw)
            self._pending.clear()

    @property
    def has_pending_records(self) -> bool:
        """Whether parsed records remain queued for a later bounded poll."""

        return bool(self._records)

    def export_checkpoint(self) -> Dict[str, object]:
        """Return bounded state that can resume this follower without history replay."""

        return {
            "version": 1,
            "kind": "jsonl",
            "path": str(self.path),
            "offset": self.offset,
            "identity": self._identity,
            "anchor": bytes(self._anchor),
            "pending": bytes(self._pending),
            "dropping_line": self._dropping_line,
            "records": tuple(dict(record) for record in self._records),
            "missing_at_start": self._missing_at_start,
        }

    def restore_checkpoint(self, checkpoint: Mapping[str, object]) -> bool:
        """Restore a compatible checkpoint, leaving current state intact on failure."""

        if (
            not isinstance(checkpoint, Mapping)
            or checkpoint.get("version") != 1
            or checkpoint.get("kind") != "jsonl"
            or checkpoint.get("path") != str(self.path)
        ):
            return False

        offset = checkpoint.get("offset")
        identity = checkpoint.get("identity")
        anchor = checkpoint.get("anchor")
        pending = checkpoint.get("pending")
        dropping_line = checkpoint.get("dropping_line")
        records = checkpoint.get("records")
        missing_at_start = checkpoint.get("missing_at_start")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(anchor, (bytes, bytearray))
            or len(anchor) > min(64, offset)
            or not isinstance(pending, (bytes, bytearray))
            or len(pending) > min(self.max_line_bytes, offset)
            or not isinstance(dropping_line, bool)
            or not isinstance(records, (list, tuple))
            or len(records) > self.max_read_bytes
            or not isinstance(missing_at_start, bool)
        ):
            return False
        if identity is not None:
            if (
                not isinstance(identity, (list, tuple))
                or len(identity) != 2
                or any(
                    not isinstance(value, int) or isinstance(value, bool)
                    for value in identity
                )
            ):
                return False
            restored_identity: Optional[Tuple[int, int]] = (
                int(identity[0]),
                int(identity[1]),
            )
        else:
            restored_identity = None

        restored_records: Deque[Dict[str, object]] = deque()
        for record in records:
            if not isinstance(record, Mapping):
                return False
            restored_records.append(dict(record))

        self.offset = offset
        self._identity = restored_identity
        self._anchor = bytes(anchor)
        self._pending = bytearray(pending)
        self._dropping_line = dropping_line
        self._records = restored_records
        self._missing_at_start = missing_at_start
        return True

    def poll(self) -> List[Dict[str, object]]:
        """Return newly completed records, or an empty list when unavailable."""

        if self._records:
            return [self._records.popleft() for _ in range(min(self.max_records, len(self._records)))]

        try:
            stat = self.path.stat()
            is_file = self.path.is_file()
        except OSError:
            return []
        if not is_file:
            return []

        identity = (stat.st_dev, stat.st_ino)
        if self._identity is None:
            self._identity = identity
            # A file created after the follower started contains new data.
            self.offset = 0 if self._missing_at_start or self.from_start else stat.st_size
            self._missing_at_start = False
        elif identity != self._identity or stat.st_size < self.offset:
            self._identity = identity
            self._reset()
        elif self.offset and self._anchor != self._read_anchor(self.offset):
            # Detect truncate-and-regrow cycles that happen between polls.
            self._reset()

        available = stat.st_size - self.offset
        if available <= 0:
            return []
        amount = min(self.max_read_bytes, available)
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read(amount)
        except OSError:
            return []
        if not chunk:
            return []
        self.offset += len(chunk)
        self._anchor = (self._anchor + chunk)[-64:]
        self._consume_chunk(chunk)
        return [self._records.popleft() for _ in range(min(self.max_records, len(self._records)))]

class SessionTailer:
    """Map one session's native records through its registered adapter."""

    def __init__(
        self,
        session: Session,
        adapter: Optional[Adapter] = None,
        from_start: bool = False,
        max_read_bytes: int = DEFAULT_READ_BYTES,
        max_line_bytes: int = DEFAULT_LINE_BYTES,
        max_records: int = DEFAULT_RECORDS,
    ) -> None:
        self.session = session
        self.adapter = adapter if adapter is not None else get_adapter(session.agent)
        self.from_start = bool(from_start)
        self.max_records = int(max_records)
        self.errors: List[str] = []
        self._replay = getattr(self.adapter, "replay", None)
        self._is_dsh = self.adapter.name == "dsh" and callable(self._replay)
        self._dsh_initialized = self.from_start
        self._last_seq: Optional[int] = None
        self._dsh_page_pending = False
        self._dsh_force_replay = True
        self._dsh_signature: Optional[DSHSignature] = None

        if self._is_dsh:
            self._dsh_signature = self._dsh_transcript_signature()
            if not self.from_start:
                seq = session.extra.get("seq")
                if isinstance(seq, int) and not isinstance(seq, bool):
                    self._last_seq = seq
                    self._dsh_initialized = True
            self.follower: Optional[JSONLFollower] = None
        else:
            self.follower = JSONLFollower(
                Path(session.transcript) if session.transcript else Path("__missing_transcript__"),
                from_start=from_start,
                max_read_bytes=max_read_bytes,
                max_line_bytes=max_line_bytes,
                max_records=max_records,
            )

    def _normalize(self, record: Mapping[str, object]) -> List[Event]:
        try:
            return [
                event
                for event in self.adapter.normalize(record, self.session)
                if isinstance(event, Event)
            ]
        except Exception as error:
            self.errors.append("{}: {}".format(error.__class__.__name__, error))
            return []

    @property
    def has_pending_records(self) -> bool:
        """Whether another bounded poll may return already-present records."""

        if self._is_dsh:
            return self._dsh_page_pending
        return bool(self.follower and self.follower.has_pending_records)

    @property
    def is_dsh(self) -> bool:
        """Whether this tailer uses bounded compressed DSH replay."""

        return self._is_dsh

    def export_checkpoint(self) -> Dict[str, object]:
        """Return the minimal bounded cursor needed to resume this session."""

        if self._is_dsh:
            return {
                "version": 1,
                "kind": "dsh",
                "path": self.session.transcript,
                "last_seq": self._last_seq,
                "initialized": self._dsh_initialized,
                "page_pending": self._dsh_page_pending,
                "signature": self._dsh_signature,
                "force_replay": self._dsh_force_replay,
            }
        return {
            "version": 1,
            "kind": "jsonl_session",
            "path": self.session.transcript,
            "follower": (
                self.follower.export_checkpoint()
                if self.follower is not None
                else None
            ),
        }

    def restore_checkpoint(self, checkpoint: Mapping[str, object]) -> bool:
        """Restore a checkpoint compatible with this session and tailer kind."""

        if (
            not isinstance(checkpoint, Mapping)
            or checkpoint.get("version") != 1
            or checkpoint.get("path") != self.session.transcript
        ):
            return False
        if self._is_dsh:
            last_seq = checkpoint.get("last_seq")
            initialized = checkpoint.get("initialized")
            page_pending = checkpoint.get("page_pending")
            signature = checkpoint.get("signature")
            force_replay = checkpoint.get("force_replay", True)
            if (
                checkpoint.get("kind") != "dsh"
                or (
                    last_seq is not None
                    and (
                        not isinstance(last_seq, int)
                        or isinstance(last_seq, bool)
                    )
                )
                or not isinstance(initialized, bool)
                or not isinstance(page_pending, bool)
                or (
                    signature is not None
                    and (
                        not isinstance(signature, (list, tuple))
                        or len(signature) != 6
                        or any(
                            not isinstance(value, int) or isinstance(value, bool)
                            for value in signature
                        )
                    )
                )
                or not isinstance(force_replay, bool)
            ):
                return False
            self._last_seq = last_seq
            self._dsh_initialized = initialized
            self._dsh_page_pending = page_pending
            self._dsh_signature = (
                cast(DSHSignature, tuple(signature))
                if signature is not None
                else None
            )
            self._dsh_force_replay = force_replay
            return True

        follower_checkpoint = checkpoint.get("follower")
        return bool(
            checkpoint.get("kind") == "jsonl_session"
            and self.follower is not None
            and isinstance(follower_checkpoint, Mapping)
            and self.follower.restore_checkpoint(follower_checkpoint)
        )

    @staticmethod
    def _sequence(record: Mapping[str, object]) -> Optional[int]:
        value = record.get("seq")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _dsh_transcript_signature(self) -> Optional[DSHSignature]:
        """Return a cheap identity that catches compressed-file rewrites."""

        try:
            stat_result = Path(self.session.transcript).expanduser().stat()
        except OSError:
            return None
        if not stat_module.S_ISREG(stat_result.st_mode):
            return None
        return (
            int(stat_result.st_dev),
            int(stat_result.st_ino),
            int(stat_result.st_mode),
            int(stat_result.st_size),
            int(
                getattr(
                    stat_result,
                    "st_mtime_ns",
                    int(stat_result.st_mtime * 1_000_000_000),
                )
            ),
            int(
                getattr(
                    stat_result,
                    "st_ctime_ns",
                    int(stat_result.st_ctime * 1_000_000_000),
                )
            ),
        )

    def _poll_dsh(self) -> List[Event]:
        signature = self._dsh_transcript_signature()
        if (
            not self._dsh_force_replay
            and not self._dsh_page_pending
            and signature is not None
            and signature == self._dsh_signature
        ):
            return []

        self._dsh_page_pending = False
        try:
            records = self._replay(  # type: ignore[misc]
                self.session,
                after_seq=self._last_seq,
                max_records=self.max_records,
            )
        except Exception as error:
            self.errors.append("{}: {}".format(error.__class__.__name__, error))
            self._dsh_force_replay = True
            return []

        self._dsh_signature = signature
        self._dsh_force_replay = False
        events: List[Event] = []
        previous = self._last_seq
        newest = previous
        record_count = 0
        for record in records:
            record_count += 1
            if not isinstance(record, Mapping):
                continue
            seq = self._sequence(record)
            if seq is None or (newest is not None and seq <= newest):
                continue
            newest = seq
            if self._dsh_initialized:
                events.extend(self._normalize(record))
        self._last_seq = newest
        self._dsh_initialized = True
        self._dsh_page_pending = (
            record_count >= self.max_records and newest != previous
        )
        return events

    def poll(self) -> List[Event]:
        if self._is_dsh:
            return self._poll_dsh()
        if self.follower is None:
            return []
        events: List[Event] = []
        for record in self.follower.poll():
            events.extend(self._normalize(record))
        return events


def watch_sessions(
    sessions: Iterable[Session],
    from_start: bool = False,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> Iterator[Event]:
    """Poll several direct followers forever, yielding normalized events."""

    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    tailers = [SessionTailer(session, from_start=from_start) for session in sessions]
    while tailers:
        for tailer in tailers:
            yield from tailer.poll()
        time.sleep(poll_interval)


def follow_session(
    session: Session,
    from_start: bool = False,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> Iterator[Event]:
    return watch_sessions(
        (session,),
        from_start=from_start,
        poll_interval=poll_interval,
    )


__all__ = [
    "JSONLFollower",
    "SessionTailer",
    "follow_session",
    "watch_sessions",
]
