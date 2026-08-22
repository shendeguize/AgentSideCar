"""Bounded, read-only inference of agent session lifecycle state."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from sidecar.adapters.base import (
    DEFAULT_METADATA_CACHE_SIZE,
    Adapter,
    MetadataCache,
    content_items,
    file_signature,
    read_jsonl_tail,
    timestamp_epoch,
)
from sidecar.model import Session, Status

DEFAULT_FRESH_SECONDS = 90.0
DEFAULT_IDLE_SECONDS = 15.0 * 60.0
DEFAULT_TERMINAL_FRESH_SECONDS = 20.0
DEFAULT_TAIL_BYTES = 64 * 1024
DEFAULT_TAIL_RECORDS = 512
DEFAULT_TERMINAL_BYTES = 4096
DEFAULT_TERMINAL_FILES = 64

_TRUE_VALUES = {"1", "true", "yes", "on"}
_NULL_VALUES = {"", "null", "none", "~"}
_RUNNING_VALUES = {"busy", "running", "working"}
_FINISHED_VALUES = {
    "cancelled",
    "completed",
    "done",
    "exited",
    "failed",
    "stopped",
    "succeeded",
    "success",
}
_KEY_NORMALIZER = re.compile(r"[^a-z0-9]+")

StateErrorHandler = Callable[[str, Exception], None]


def _normalized_key(value: str) -> str:
    return _KEY_NORMALIZER.sub("_", value.strip().lower()).strip("_")


def _bounded_text(path: Path, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    try:
        with path.open("rb") as stream:
            return stream.read(max_bytes).decode("utf-8", "replace")
    except OSError:
        return ""


def parse_terminal_metadata(text: str, max_lines: int = 64) -> Dict[str, str]:
    """Parse only the small metadata header of a Cursor terminal snapshot."""

    metadata: Dict[str, str] = {}
    lines = text.splitlines()
    in_delimited_header = bool(lines and lines[0].strip() == "---")
    start = 1 if in_delimited_header else 0
    for line in lines[start : start + max(0, max_lines)]:
        if in_delimited_header and line.strip() == "---":
            break
        key, separator, value = line.partition(":")
        if not separator:
            continue
        normalized = _normalized_key(key)
        if normalized:
            metadata[normalized] = value.strip().strip("\"'")
    return metadata


def terminal_metadata_is_active(metadata: Mapping[str, str]) -> bool:
    """Return true only when metadata indicates a command is currently running."""

    normalized = {
        _normalized_key(str(key)): str(value).strip().strip("\"'").lower()
        for key, value in metadata.items()
    }
    states = [
        normalized.get(key)
        for key in ("status", "state", "process_status", "command_status")
        if normalized.get(key)
    ]
    if any(value in _FINISHED_VALUES for value in states):
        return False
    if any(value in _RUNNING_VALUES for value in states):
        return True

    for key in (
        "running",
        "is_running",
        "command_running",
        "process_running",
    ):
        if normalized.get(key) in _TRUE_VALUES:
            return True

    running_for = normalized.get("running_for_ms")
    command = (
        normalized.get("command")
        or normalized.get("current_command")
        or normalized.get("active_command")
    )
    if running_for is not None and command and command not in _NULL_VALUES:
        try:
            if float(running_for) >= 0:
                return True
        except ValueError:
            pass

    current_command = normalized.get("current_command") or normalized.get("active_command")
    if current_command and current_command not in _NULL_VALUES:
        return True

    last_command = normalized.get("last_command")
    exit_code = normalized.get("last_exit_code", normalized.get("exit_code"))
    return bool(
        last_command
        and last_command not in _NULL_VALUES
        and exit_code is not None
        and exit_code in _NULL_VALUES
    )


def terminal_file_is_active(
    path: Path,
    now: Optional[float] = None,
    fresh_seconds: float = DEFAULT_TERMINAL_FRESH_SECONDS,
    max_bytes: int = DEFAULT_TERMINAL_BYTES,
) -> bool:
    """Inspect one terminal snapshot without reading its unbounded output body."""

    current = time.time() if now is None else float(now)
    try:
        modified = path.stat().st_mtime
    except OSError:
        return False
    if max(0.0, current - modified) > fresh_seconds:
        return False
    return terminal_metadata_is_active(parse_terminal_metadata(_bounded_text(path, max_bytes)))


def _cursor_terminal_roots(
    session: Session,
    home: Optional[Path],
    terminals_root: Optional[Path],
) -> Tuple[Path, ...]:
    if terminals_root is not None:
        return (Path(terminals_root),)

    roots: List[Path] = []
    if session.transcript:
        transcript = Path(session.transcript).expanduser()
        for parent in transcript.parents:
            if parent.name == "agent-transcripts":
                roots.append(parent.parent / "terminals")
                break

    configured = session.extra.get("terminals_root")
    if isinstance(configured, (str, os.PathLike)):
        roots.append(Path(configured).expanduser())

    # A project slug supplied by an adapter is a safe fallback that avoids
    # scanning unrelated Cursor projects.
    project_slug = session.extra.get("project_slug")
    if project_slug and home is not None:
        roots.append(Path(home).expanduser() / ".cursor" / "projects" / str(project_slug) / "terminals")

    unique: List[Path] = []
    seen = set()
    for root in roots:
        text = str(root)
        if text not in seen:
            seen.add(text)
            unique.append(root)
    return tuple(unique)


def cursor_terminal_active(
    session: Session,
    now: Optional[float] = None,
    home: Optional[Path] = None,
    fresh_seconds: float = DEFAULT_TERMINAL_FRESH_SECONDS,
    terminals_root: Optional[Path] = None,
    max_files: int = DEFAULT_TERMINAL_FILES,
    max_bytes: int = DEFAULT_TERMINAL_BYTES,
) -> bool:
    """Check only terminal snapshots associated with a Cursor IDE session."""

    agent = session.agent.lower()
    if agent not in {"cursor", "cursor-ide", "cursor_ide"}:
        return False
    if str(session.extra.get("source", "")).lower() == "cli":
        return False
    if max_files <= 0:
        return False

    inspected = 0
    for root in _cursor_terminal_roots(session, home, terminals_root):
        try:
            entries = os.scandir(str(root))
        except OSError:
            continue
        with entries:
            for entry in entries:
                if inspected >= max_files:
                    return False
                inspected += 1
                if not entry.name.endswith(".txt"):
                    continue
                try:
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                if not is_file:
                    continue
                if terminal_file_is_active(
                    Path(entry.path),
                    now=now,
                    fresh_seconds=fresh_seconds,
                    max_bytes=max_bytes,
                ):
                    return True
    return False


def _record_timestamp(record: Mapping[str, Any]) -> Optional[float]:
    message = record.get("message")
    if not isinstance(message, Mapping):
        message = {}
    for value in (
        record.get("timestamp"),
        record.get("time"),
        record.get("createdAt"),
        record.get("updatedAt"),
        message.get("timestamp"),
    ):
        timestamp = timestamp_epoch(value)
        if timestamp is not None:
            return timestamp
    return None


def _tool_identifier(item: Mapping[str, Any], result: bool = False) -> str:
    keys: Sequence[str]
    if result:
        keys = ("tool_use_id", "toolUseId", "tool_call_id", "toolCallId", "id")
    else:
        keys = ("id", "tool_use_id", "toolUseId", "tool_call_id", "toolCallId")
    for key in keys:
        value = item.get(key)
        if value is not None and str(value):
            return str(value)
    return ""


@dataclass
class _TailState:
    last_kind: str = ""
    activity_at: Optional[float] = None
    unmatched_tools: int = 0


def _tail_state(records: Sequence[Mapping[str, Any]]) -> _TailState:
    unmatched: Dict[str, int] = {}
    anonymous: List[int] = []
    last_kind = ""
    activity_at: Optional[float] = None
    ordinal = 0

    def mark(kind: str, timestamp: Optional[float]) -> int:
        nonlocal ordinal, last_kind, activity_at
        ordinal += 1
        last_kind = kind
        activity_at = timestamp
        return ordinal

    for record in records:
        record_type = str(record.get("type") or "").lower()
        message = record.get("message")
        if not isinstance(message, Mapping):
            message = {}
        role = str(record.get("role") or message.get("role") or record_type).lower()
        timestamp = _record_timestamp(record)
        content = message.get("content", record.get("content"))

        if record_type in {"tool_use", "tool_call", "tool_result"}:
            items: List[Mapping[str, Any]] = [record]
        else:
            items = content_items(content)

        for item in items:
            item_type = str(item.get("type") or "").lower()
            if item_type in {"tool_use", "tool_call"}:
                position = mark("tool_use", timestamp)
                identifier = _tool_identifier(item)
                if identifier:
                    unmatched[identifier] = position
                else:
                    anonymous.append(position)
            elif item_type == "tool_result":
                identifier = _tool_identifier(item, result=True)
                if identifier:
                    unmatched.pop(identifier, None)
                elif anonymous:
                    anonymous.pop()
                mark("tool_result", timestamp)
            elif item_type == "thinking":
                mark("thinking", timestamp)
            elif item_type == "text" and str(item.get("text") or "").strip():
                if role == "user":
                    mark("user", timestamp)
                elif role == "assistant":
                    mark("assistant", timestamp)

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes, bytearray)):
            for call in tool_calls:
                if not isinstance(call, Mapping):
                    continue
                position = mark("tool_use", timestamp)
                identifier = _tool_identifier(call)
                if identifier:
                    unmatched[identifier] = position
                else:
                    anonymous.append(position)

        if not items:
            if record_type in {"thinking", "tool_call_started"}:
                mark("thinking", timestamp)
            elif role == "tool":
                mark("tool_result", timestamp)

    return _TailState(last_kind, activity_at, len(unmatched) + len(anonymous))


class StateEngine:
    """Infer state from adapter hints and a bounded transcript tail.

    Instances own a bounded, non-thread-safe parsed-tail cache and are intended
    for the scanner's serialized calls. Time and terminal evidence are never
    cached.
    """

    def __init__(
        self,
        fresh_seconds: float = DEFAULT_FRESH_SECONDS,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        tail_bytes: int = DEFAULT_TAIL_BYTES,
        tail_records: int = DEFAULT_TAIL_RECORDS,
        terminal_fresh_seconds: float = DEFAULT_TERMINAL_FRESH_SECONDS,
        terminal_bytes: int = DEFAULT_TERMINAL_BYTES,
        terminal_files: int = DEFAULT_TERMINAL_FILES,
        home: Optional[Path] = None,
        fresh_threshold: Optional[float] = None,
        idle_threshold: Optional[float] = None,
        tail_cache_size: int = DEFAULT_METADATA_CACHE_SIZE,
    ) -> None:
        if fresh_threshold is not None:
            fresh_seconds = fresh_threshold
        if idle_threshold is not None:
            idle_seconds = idle_threshold
        if fresh_seconds < 0 or idle_seconds < 0:
            raise ValueError("state thresholds must be non-negative")
        if fresh_seconds > idle_seconds:
            raise ValueError("fresh_seconds must not exceed idle_seconds")
        if tail_bytes <= 0 or tail_records <= 0:
            raise ValueError("tail bounds must be positive")
        if terminal_fresh_seconds < 0 or terminal_bytes <= 0 or terminal_files <= 0:
            raise ValueError("terminal bounds must be positive")

        self.fresh_seconds = float(fresh_seconds)
        self.idle_seconds = float(idle_seconds)
        self.tail_bytes = int(tail_bytes)
        self.tail_records = int(tail_records)
        self.terminal_fresh_seconds = float(terminal_fresh_seconds)
        self.terminal_bytes = int(terminal_bytes)
        self.terminal_files = int(terminal_files)
        self.home = Path.home() if home is None else Path(home).expanduser()
        self._tail_cache: MetadataCache[_TailState] = MetadataCache(tail_cache_size)

    def infer_status(
        self,
        session: Session,
        adapter: Optional[Adapter] = None,
        now: Optional[float] = None,
        on_error: Optional[StateErrorHandler] = None,
    ) -> Status:
        current = time.time() if now is None else float(now)

        if adapter is not None:
            try:
                inferred = adapter.infer_status(session, current)
                if inferred is not None:
                    return inferred if isinstance(inferred, Status) else Status(inferred)
            except Exception as error:
                if on_error is not None:
                    on_error("infer_status", error)

        if not session.transcript:
            return Status.DEAD
        transcript = Path(session.transcript).expanduser()
        if not transcript.is_file():
            return Status.DEAD

        signature = file_signature(transcript)
        if signature is None:
            return Status.DEAD
        tail = self._tail_cache.get_or_load(
            signature,
            lambda: _tail_state(
                read_jsonl_tail(
                    transcript,
                    max_bytes=self.tail_bytes,
                    max_records=self.tail_records,
                )
            ),
        )
        activity_at = session.updated_at if tail.activity_at is None else tail.activity_at
        age = max(0.0, current - activity_at)

        if age > self.idle_seconds:
            status = Status.IDLE
        elif age <= self.fresh_seconds and (
            tail.last_kind in {"user", "thinking"}
            or (tail.last_kind == "tool_use" and tail.unmatched_tools > 0)
        ):
            status = Status.WORKING
        else:
            status = Status.WAITING

        # Project terminals are shared by every Cursor transcript in that
        # project.  Treat them as corroboration only for a still-plausible
        # session; otherwise one live terminal revives all historical chats.
        terminal_may_promote = (
            status is not Status.IDLE
            and session.age_seconds(current) <= self.idle_seconds
        )
        if terminal_may_promote and cursor_terminal_active(
            session,
            now=current,
            home=self.home,
            fresh_seconds=self.terminal_fresh_seconds,
            max_files=self.terminal_files,
            max_bytes=self.terminal_bytes,
        ):
            return Status.WORKING
        return status

    def apply(
        self,
        session: Session,
        adapter: Optional[Adapter] = None,
        now: Optional[float] = None,
        on_error: Optional[StateErrorHandler] = None,
    ) -> Session:
        """Set and return ``session`` for scanner pipelines."""

        session.status = self.infer_status(
            session,
            adapter=adapter,
            now=now,
            on_error=on_error,
        )
        return session


__all__ = [
    "StateEngine",
    "cursor_terminal_active",
    "parse_terminal_metadata",
    "terminal_file_is_active",
    "terminal_metadata_is_active",
]
