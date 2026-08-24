"""Codex CLI rollout adapter."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Hashable, Iterable, List, Mapping, Optional, Sequence, Tuple

from sidecar.adapters.base import (
    Adapter,
    DEFAULT_METADATA_CACHE_SIZE,
    MetadataCache,
    as_mapping,
    compact_json,
    file_signature,
    local_timestamp,
    read_jsonl_prefix,
    read_jsonl_tail,
    snip,
    text_content,
)
from sidecar.model import Event, Session, Status

_DISCOVERY_BYTES = 512 * 1024
_DISCOVERY_RECORDS = 256
_STATUS_TAIL_BYTES = 128 * 1024
_STATUS_TAIL_RECORDS = 256
_SNAPSHOT_ATTEMPTS = 3
_SNAPSHOT_BACKOFF_SECONDS = 0.002
_SNAPSHOT_COPY_BYTES = 1024 * 1024
_SNAPSHOT_DB_BYTES = 64 * 1024 * 1024
_SNAPSHOT_WAL_BYTES = 256 * 1024 * 1024
# Native DB and rollout-tail statuses are snapshots, not durable leases.
# Keep this aligned with the state engine's default 15-minute idle threshold.
CODEX_STATUS_MAX_AGE_SECONDS = 15.0 * 60.0

_WORKING_STATUSES = {
    "active",
    "in_progress",
    "inprogress",
    "running",
    "started",
    "working",
}
_WAITING_STATUSES = {
    "complete",
    "completed",
    "done",
    "success",
    "succeeded",
    "waiting",
}
_IDLE_STATUSES = {
    "aborted",
    "cancelled",
    "canceled",
    "failed",
    "interrupted",
    "stopped",
}
_INTERNAL_USER_PREFIXES = (
    "<environment_context",
    "<permissions instructions",
    "<skills_instructions",
)


def _string(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        return text_content(content)
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts = []
        for item in content:
            if isinstance(item, Mapping):
                text = text_content(item)
                if text:
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    return ""


def _first_user_text(record: Mapping[str, Any]) -> str:
    record_type = str(record.get("type") or "")
    payload = as_mapping(record.get("payload"))
    payload_type = str(payload.get("type") or "")
    candidate = ""

    if record_type == "event_msg" and payload_type == "user_message":
        candidate = _string(payload, "message", "text")
        if not candidate:
            candidate = _content_text(payload.get("content"))
    elif record_type == "event_msg" and payload_type == "item_completed":
        item = as_mapping(payload.get("item"))
        if str(item.get("type") or "").lower() == "usermessage":
            candidate = _content_text(item.get("content"))
    elif record_type == "response_item":
        if payload_type == "message" and str(payload.get("role") or "") == "user":
            candidate = _content_text(payload.get("content"))
        elif payload_type in ("user_message", "user"):
            candidate = _string(payload, "message", "text")
            if not candidate:
                candidate = _content_text(payload.get("content"))

    stripped = candidate.strip()
    if stripped.lower().startswith(_INTERNAL_USER_PREFIXES):
        return ""
    return stripped


def _rollout_id(path: Path) -> str:
    parts = path.stem.split("-", 7)
    return parts[-1] if len(parts) == 8 else path.stem


def _codex_metadata(path: Path) -> Tuple[str, str, str, Dict[str, Any]]:
    session_id = ""
    cwd = ""
    title = ""
    originator = ""
    model = ""
    model_provider = ""
    cli_version = ""

    for record in read_jsonl_prefix(
        path,
        max_bytes=_DISCOVERY_BYTES,
        max_records=_DISCOVERY_RECORDS,
    ):
        record_type = str(record.get("type") or "")
        payload = as_mapping(record.get("payload"))
        if record_type == "session_meta":
            session_id = session_id or _string(payload, "session_id", "id")
            cwd = cwd or _string(payload, "cwd", "project", "workspace")
            originator = originator or _string(payload, "originator")
            model = model or _string(payload, "model", "model_name")
            model_provider = model_provider or _string(payload, "model_provider")
            cli_version = cli_version or _string(payload, "cli_version")
        elif record_type == "turn_context":
            model = model or _string(payload, "model")

        if not title:
            title = _first_user_text(record)
        if session_id and cwd and title and model:
            break

    extra: Dict[str, Any] = {"source": "rollout"}
    for key, value in (
        ("originator", originator),
        ("model", model),
        ("model_provider", model_provider),
        ("cli_version", cli_version),
    ):
        if value:
            extra[key] = value
    return session_id or _rollout_id(path), cwd, snip(title, 160), extra


def _status_from_native(value: Any) -> Optional[Status]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[\s-]+", "_", value.strip().lower())
    if normalized in _WORKING_STATUSES:
        return Status.WORKING
    if normalized in _WAITING_STATUSES:
        return Status.WAITING
    if normalized in _IDLE_STATUSES:
        return Status.IDLE
    return None


def _quoted_identifier(identifier: str) -> str:
    return '"{}"'.format(identifier.replace('"', '""'))


_FileSignature = Tuple[int, int, int, int, int]


def _signature_from_stat(
    details: os.stat_result,
    maximum: int,
) -> _FileSignature:
    if not stat.S_ISREG(details.st_mode):
        raise OSError("Codex status source is not a regular file")
    size = int(details.st_size)
    if size < 0 or size > maximum:
        raise OSError("Codex status source exceeds snapshot limit")
    return (
        int(details.st_dev),
        int(details.st_ino),
        size,
        int(getattr(details, "st_mtime_ns", int(details.st_mtime * 1e9))),
        int(getattr(details, "st_ctime_ns", int(details.st_ctime * 1e9))),
    )


def _regular_signature(
    path: Path,
    maximum: int,
    *,
    required: bool,
) -> Optional[_FileSignature]:
    try:
        # Follow symlinks only when their current target is a regular file.
        # The opened descriptor is checked against this signature before copy.
        details = os.stat(str(path))
    except FileNotFoundError:
        if required:
            raise
        return None
    return _signature_from_stat(details, maximum)


def _copy_regular_file(
    source: Path,
    destination: Path,
    expected_signature: _FileSignature,
) -> int:
    copied = 0
    descriptor: Optional[int] = None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(source), flags)
        opened_signature = _signature_from_stat(
            os.fstat(descriptor),
            expected_signature[2],
        )
        if opened_signature != expected_signature:
            raise OSError("Codex status source changed before snapshot copy")

        with os.fdopen(descriptor, "rb") as reader:
            descriptor = None
            with destination.open("xb") as writer:
                while copied < expected_signature[2]:
                    chunk = reader.read(
                        min(
                            _SNAPSHOT_COPY_BYTES,
                            expected_signature[2] - copied,
                        )
                    )
                    if not chunk:
                        break
                    writer.write(chunk)
                    copied += len(chunk)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return copied


def _status_from_connection(
    connection: sqlite3.Connection,
    session_id: str,
) -> Optional[Status]:
    connection.execute("PRAGMA query_only = ON")
    columns = [
        str(row[1])
        for row in connection.execute("PRAGMA table_info(thread_turns)")
        if len(row) > 1
    ]
    column_names = {name.lower(): name for name in columns}
    status_column = next(
        (column_names[name] for name in ("status", "state") if name in column_names),
        None,
    )
    thread_column = next(
        (
            column_names[name]
            for name in (
                "thread_id",
                "threadid",
                "session_id",
                "sessionid",
                "conversation_id",
            )
            if name in column_names
        ),
        None,
    )
    if status_column is None or thread_column is None:
        return None

    order_column = next(
        (
            column_names[name]
            for name in (
                "updated_at",
                "started_at",
                "created_at",
                "turn_id",
                "completed_at",
            )
            if name in column_names
        ),
        None,
    )
    order_options = ["rowid DESC"]
    if order_column:
        order_options.append("{} DESC".format(_quoted_identifier(order_column)))
    for order_sql in order_options:
        query = (
            "SELECT {status} FROM thread_turns "
            "WHERE {thread} = ? ORDER BY {order} LIMIT 1"
        ).format(
            status=_quoted_identifier(status_column),
            thread=_quoted_identifier(thread_column),
            order=order_sql,
        )
        try:
            row = connection.execute(query, (session_id,)).fetchone()
        except sqlite3.DatabaseError:
            continue
        return _status_from_native(row[0]) if row else None
    return None


def _read_copied_status(path: Path, session_id: str) -> Optional[Status]:
    source_wal = Path(str(path) + "-wal")
    for attempt in range(_SNAPSHOT_ATTEMPTS):
        try:
            before_db = _regular_signature(
                path,
                _SNAPSHOT_DB_BYTES,
                required=True,
            )
            before_wal = _regular_signature(
                source_wal,
                _SNAPSHOT_WAL_BYTES,
                required=False,
            )
        except OSError:
            break
        if before_db is None:
            break

        copy_error: Optional[OSError] = None
        with tempfile.TemporaryDirectory(prefix="agent-sidecar-codex-") as temporary:
            copied_db = Path(temporary) / "snapshot.sqlite"
            copied_wal = Path(str(copied_db) + "-wal")
            try:
                db_bytes = _copy_regular_file(path, copied_db, before_db)
                wal_bytes = (
                    _copy_regular_file(source_wal, copied_wal, before_wal)
                    if before_wal is not None
                    else 0
                )
            except OSError as error:
                copy_error = error
                db_bytes = -1
                wal_bytes = -1

            try:
                after_db = _regular_signature(
                    path,
                    _SNAPSHOT_DB_BYTES,
                    required=False,
                )
                after_wal = _regular_signature(
                    source_wal,
                    _SNAPSHOT_WAL_BYTES,
                    required=False,
                )
            except OSError:
                after_db = None
                after_wal = None
            signature_race = before_db != after_db or before_wal != after_wal
            incomplete_copy = (
                db_bytes != before_db[2]
                or (before_wal is not None and wal_bytes != before_wal[2])
            )
            if signature_race:
                if attempt + 1 < _SNAPSHOT_ATTEMPTS:
                    time.sleep(_SNAPSHOT_BACKOFF_SECONDS * (2**attempt))
                continue
            if copy_error is not None or incomplete_copy:
                break

            connection: Optional[sqlite3.Connection] = None
            try:
                connection = sqlite3.connect(
                    copied_db.as_uri() + "?mode=ro",
                    uri=True,
                    timeout=0.1,
                )
                return _status_from_connection(connection, session_id)
            except sqlite3.DatabaseError:
                break
            finally:
                if connection is not None:
                    connection.close()
    return _read_immutable_status(path, session_id)


def _read_immutable_status(path: Path, session_id: str) -> Optional[Status]:
    try:
        before = _regular_signature(
            path,
            _SNAPSHOT_DB_BYTES,
            required=True,
        )
    except OSError:
        return None
    if before is None:
        return None

    with tempfile.TemporaryDirectory(prefix="agent-sidecar-codex-main-") as temporary:
        copied_db = Path(temporary) / "snapshot.sqlite"
        try:
            copied_bytes = _copy_regular_file(path, copied_db, before)
            after = _regular_signature(
                path,
                _SNAPSHOT_DB_BYTES,
                required=False,
            )
        except OSError:
            return None
        if copied_bytes != before[2] or after != before:
            return None

        connection: Optional[sqlite3.Connection] = None
        try:
            # Open only the verified private snapshot. This main-DB fallback may
            # omit WAL-only rows, but never asks SQLite to inspect the source.
            connection = sqlite3.connect(
                copied_db.as_uri() + "?mode=ro&immutable=1",
                uri=True,
                timeout=0.1,
            )
            return _status_from_connection(connection, session_id)
        except sqlite3.DatabaseError:
            return None
        finally:
            if connection is not None:
                connection.close()


def _latest_thread_status(path: Path, session_id: str) -> Optional[Status]:
    if not session_id:
        return None
    try:
        return _read_copied_status(path, session_id)
    except (OSError, sqlite3.DatabaseError):
        return None


def _status_db_path(session: Session) -> Optional[Path]:
    configured = session.extra.get("status_db")
    if isinstance(configured, (str, Path)) and str(configured):
        return Path(configured)

    if session.transcript:
        transcript = Path(session.transcript)
        for parent in transcript.parents:
            if parent.name == ".codex":
                return parent / "thread_history_1.sqlite"
    return None


def _tail_status(path: Path) -> Optional[Status]:
    latest: Optional[Status] = None
    for record in read_jsonl_tail(
        path,
        max_bytes=_STATUS_TAIL_BYTES,
        max_records=_STATUS_TAIL_RECORDS,
    ):
        if record.get("type") != "event_msg":
            continue
        payload = as_mapping(record.get("payload"))
        payload_type = str(payload.get("type") or "")
        if payload_type == "task_started":
            latest = Status.WORKING
        elif payload_type == "task_complete":
            latest = Status.WAITING
    return latest


def _event(
    timestamp: str,
    session: Session,
    kind: str,
    text: Any,
    limit: int = 120,
    extra: Optional[Dict[str, Any]] = None,
) -> List[Event]:
    rendered = snip(text, limit)
    if not rendered:
        return []
    return [
        Event(
            timestamp,
            session.agent,
            session.session_id,
            kind,
            rendered,
            extra or {},
        )
    ]


def _token_event(
    timestamp: str,
    session: Session,
    payload: Mapping[str, Any],
) -> List[Event]:
    info = as_mapping(payload.get("info"))
    total_usage = as_mapping(info.get("total_token_usage"))
    last_usage = as_mapping(info.get("last_token_usage"))
    total = total_usage.get("total_tokens")
    if total is None:
        total = info.get("total_tokens")
    if total is None:
        total = last_usage.get("total_tokens")
    if total is None:
        return []
    return _event(
        timestamp,
        session,
        "token",
        "total_tokens={}".format(total),
        extra={"usage": dict(info)},
    )


class CodexAdapter(Adapter):
    name = "codex"
    agent_names = ("codex",)

    def __init__(
        self,
        metadata_cache_size: int = DEFAULT_METADATA_CACHE_SIZE,
    ) -> None:
        self._metadata_cache: MetadataCache[
            Tuple[str, str, str, Dict[str, Any]]
        ] = MetadataCache(metadata_cache_size)

    def discover(self, home: Path) -> Iterable[Session]:
        sessions: List[Session] = []
        active_signatures: List[Hashable] = []
        codex_home = home / ".codex"
        status_db = codex_home / "thread_history_1.sqlite"
        for transcript in (codex_home / "sessions").glob("*/*/*/rollout-*.jsonl"):
            signature = file_signature(transcript)
            if signature is None:
                continue
            active_signatures.append(signature)
            updated_at = signature[1] / 1e9
            session_id, cwd, title, cached_extra = self._metadata_cache.get_or_load(
                signature,
                lambda: _codex_metadata(transcript),
            )
            extra = dict(cached_extra)
            extra["status_db"] = str(status_db)
            sessions.append(
                Session(
                    agent="codex",
                    session_id=session_id,
                    project=cwd,
                    transcript=str(transcript),
                    updated_at=updated_at,
                    title=title,
                    extra=extra,
                )
            )
        self._metadata_cache.prune(active_signatures)
        return sessions

    def normalize(self, record: Mapping[str, Any], session: Session) -> Iterable[Event]:
        timestamp = local_timestamp(record.get("timestamp"), fallback=session.updated_at)
        record_type = str(record.get("type") or "")
        payload = as_mapping(record.get("payload"))
        payload_type = str(payload.get("type") or "")

        if record_type == "event_msg":
            if payload_type == "task_started":
                return _event(timestamp, session, "task", "task_started")
            if payload_type == "task_complete":
                summary = _string(payload, "last_agent_message")
                text = "task_complete"
                if summary:
                    text += ": " + snip(summary, 80)
                return _event(timestamp, session, "task", text)
            if payload_type == "token_count":
                return _token_event(timestamp, session, payload)
            if payload_type == "agent_message":
                return _event(
                    timestamp,
                    session,
                    "assistant",
                    _string(payload, "message", "text"),
                )
            if payload_type == "user_message":
                return _event(
                    timestamp,
                    session,
                    "user",
                    _string(payload, "message", "text"),
                )
            return []

        if record_type != "response_item":
            return []
        if payload_type == "function_call":
            name = _string(payload, "name") or "tool"
            arguments = payload.get("arguments")
            if not isinstance(arguments, str):
                arguments = compact_json(arguments or {})
            return _event(
                timestamp,
                session,
                "tool_call",
                "{} {}".format(name, snip(arguments, 80)),
            )
        if payload_type == "function_call_output":
            return _event(
                timestamp,
                session,
                "tool_result",
                text_content(payload.get("output")),
                limit=80,
            )
        if payload_type == "reasoning":
            summary = payload.get("summary")
            if not summary:
                summary = payload.get("summary_text") or payload.get("content")
            return _event(
                timestamp,
                session,
                "thinking",
                _content_text(summary),
                limit=80,
            )
        if payload_type == "message":
            role = str(payload.get("role") or "")
            if role not in ("user", "assistant"):
                return []
            return _event(
                timestamp,
                session,
                role,
                _content_text(payload.get("content")),
            )
        return []

    def infer_status(self, session: Session, now: Optional[float] = None) -> Optional[Status]:
        if session.age_seconds(now) > CODEX_STATUS_MAX_AGE_SECONDS:
            return Status.IDLE
        status_db = _status_db_path(session)
        if status_db is not None:
            status = _latest_thread_status(status_db, session.session_id)
            if status is not None:
                return status
        if not session.transcript:
            return None
        return _tail_status(Path(session.transcript))


__all__ = ["CODEX_STATUS_MAX_AGE_SECONDS", "CodexAdapter"]
