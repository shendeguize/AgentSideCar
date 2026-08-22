"""DeepSeek DSH projection-cache and compressed transcript adapter."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from sidecar.adapters.base import (
    Adapter,
    ContentBlockEvent,
    as_mapping,
    compact_json,
    content_block_events,
    local_timestamp,
    read_json_object,
    snip,
    text_content,
)
from sidecar.model import Event, Session, Status

_ACTIVE_WINDOW_S = 120.0
_INDEX_BYTES = 8 * 1024 * 1024
_REPLAY_BYTES = 256 * 1024 * 1024
_REPLAY_RETAINED_BYTES = 8 * 1024 * 1024
_REPLAY_RECORDS = 1024
_REPLAY_LINE_BYTES = 1024 * 1024
_REPLAY_TIMEOUT_S = 10.0


def _utf16_units(value: str) -> Iterable[int]:
    encoded = value.encode("utf-16-be", "surrogatepass")
    for (unit,) in struct.iter_unpack(">H", encoded):
        yield unit


def _encode_segment(value: str) -> str:
    """Match DSH's UTF-16 path-segment encoding."""

    if not value:
        raise ValueError("cannot encode an empty path segment")
    if value == ".":
        return "~002E"
    if value == "..":
        return "~002E~002E"
    output = []
    for unit in _utf16_units(value):
        char = chr(unit)
        if char != "~" and (
            48 <= unit <= 57
            or 65 <= unit <= 90
            or 97 <= unit <= 122
            or char in "._-"
        ):
            output.append(char)
        else:
            output.append("~{:04X}".format(unit))
    return "".join(output)


def _project_key(cwd: str) -> str:
    """Match DSH's readable, lossy cwd directory key."""

    if not cwd:
        raise ValueError("cannot encode an empty project path")
    output: List[str] = []
    separator_run = False
    for unit in _utf16_units(cwd):
        char = chr(unit)
        if char in "/\\:":
            if not separator_run:
                output.append("-")
            separator_run = True
        elif (
            char != "~"
            and (
                48 <= unit <= 57
                or 65 <= unit <= 90
                or 97 <= unit <= 122
                or char in "._-"
            )
        ):
            output.append(char)
            separator_run = False
        else:
            output.append("~{:04X}".format(unit))
            separator_run = False
    readable = "".join(output).lstrip("-") or "root"
    return "--{}--".format(readable[:251])


def _transcript_path(dsh_home: Path, cwd: str, session_id: str) -> Path:
    project = _project_key(cwd) if cwd else "_no-cwd"
    return dsh_home / "sessions" / project / _encode_segment(session_id) / "session.jsonl.zstd"


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass


def replay_dsh_events(
    path: Path,
    after_seq: Optional[int] = None,
    max_output_bytes: int = _REPLAY_BYTES,
    max_records: int = _REPLAY_RECORDS,
    timeout: float = _REPLAY_TIMEOUT_S,
    zstd_binary: str = "zstd",
    max_retained_bytes: int = _REPLAY_RETAINED_BYTES,
) -> List[Dict[str, Any]]:
    """Stream a bounded zstd replay for scanners and seq-based watchers.

    ``max_output_bytes`` bounds the total decompressed stream scan, while
    ``max_retained_bytes`` and ``max_records`` independently bound memory used
    by matching records. Missing ``zstd``, incomplete live frames, malformed
    rows, and timeouts all degrade to the complete records decoded so far.
    """

    if (
        max_output_bytes <= 0
        or max_retained_bytes <= 0
        or max_records <= 0
        or timeout <= 0
    ):
        return []
    try:
        if not path.is_file():
            return []
    except OSError:
        return []
    if os.path.sep not in zstd_binary and shutil.which(zstd_binary) is None:
        return []

    try:
        process = subprocess.Popen(
            [zstd_binary, "-dc", "--", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError:
        return []
    if process.stdout is None:
        _stop_process(process)
        return []

    records: List[Dict[str, Any]] = []
    pending = bytearray()
    dropping_line = False
    retained = 0
    scanned = 0
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()

    def consume(raw: bytes) -> bool:
        nonlocal retained
        if not raw.strip() or len(raw) > _REPLAY_LINE_BYTES:
            return False
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeError):
            return False
        if not isinstance(record, dict):
            return False
        seq = record.get("seq")
        if after_seq is not None:
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= after_seq:
                return False
        if len(raw) > max_retained_bytes - retained:
            # Return an already useful page; if this one record is too large,
            # skip it and continue looking for a smaller later record.
            return bool(records)
        records.append(record)
        retained += len(raw)
        return len(records) >= max_records or retained >= max_retained_bytes

    def consume_chunk(chunk: bytes) -> bool:
        nonlocal dropping_line
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            if newline < 0:
                segment = chunk[start:]
                if not dropping_line:
                    if len(pending) + len(segment) <= _REPLAY_LINE_BYTES:
                        pending.extend(segment)
                    else:
                        pending.clear()
                        dropping_line = True
                return False

            segment = chunk[start:newline]
            start = newline + 1
            if dropping_line:
                dropping_line = False
                pending.clear()
                continue
            if len(pending) + len(segment) > _REPLAY_LINE_BYTES:
                pending.clear()
                continue
            raw = bytes(pending) + segment
            pending.clear()
            if consume(raw):
                return True
        return False

    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        stop = False
        eof = False
        while not stop and scanned < max_output_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready = selector.select(min(0.1, remaining))
            if not ready:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(
                    process.stdout.fileno(),
                    min(64 * 1024, max_output_bytes - scanned),
                )
            except OSError:
                break
            if not chunk:
                eof = True
                break
            scanned += len(chunk)
            if consume_chunk(chunk):
                stop = True
        if eof and pending and not dropping_line and not stop:
            consume(bytes(pending))
    finally:
        selector.close()
        process.stdout.close()
        _stop_process(process)
    return records


def _sequence(record: Mapping[str, Any]) -> Optional[int]:
    value = record.get("seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _event_extra(record: Mapping[str, Any], **values: Any) -> Dict[str, Any]:
    extra: Dict[str, Any] = {"native_type": str(record.get("type") or "")}
    seq = _sequence(record)
    if seq is not None:
        extra["seq"] = seq
    extra.update({key: value for key, value in values.items() if value is not None})
    return extra


def _render_content_block(
    item: Mapping[str, Any],
    default_kind: str,
) -> Optional[ContentBlockEvent]:
    item_type = str(item.get("type") or "")
    if item_type == "text":
        return default_kind, item.get("text"), 120
    if item_type == "reasoning":
        return "thinking", item.get("text"), 80
    if item_type == "tool-call":
        return (
            "tool_call",
            "{} {}".format(item.get("name") or "tool", item.get("arguments") or ""),
            120,
        )
    if item_type == "tool-result":
        return "tool_result", text_content(item.get("content")), 80
    return None


class DSHAdapter(Adapter):
    name = "dsh"
    agent_names = ("dsh",)

    def __init__(self) -> None:
        self._seen_seq: Dict[str, int] = {}

    def discover(self, home: Path) -> Iterable[Session]:
        dsh_home = home / ".dsh"
        index_path = dsh_home / "storages" / "session_projcache.json"
        index = read_json_object(index_path, _INDEX_BYTES)
        tables = as_mapping(index.get("tables"))
        entries = as_mapping(as_mapping(tables.get("sessions")))
        try:
            index_mtime = index_path.stat().st_mtime
        except OSError:
            index_mtime = 0.0

        sessions: List[Session] = []
        for raw_session_id, raw_entry in entries.items():
            if not isinstance(raw_session_id, str) or not raw_session_id:
                continue
            entry = as_mapping(raw_entry)
            identity = as_mapping(entry.get("identity"))
            cwd = str(identity.get("cwd") or "")
            created_at = identity.get("createdAt")
            created_epoch = (
                float(created_at) / 1000.0
                if isinstance(created_at, (int, float)) and not isinstance(created_at, bool)
                else 0.0
            )
            rows = as_mapping(entry.get("rows"))
            stats_row = as_mapping(rows.get("sessionStats"))
            stats = dict(as_mapping(stats_row.get("val")))
            seq_value = stats_row.get("seq")
            seq = (
                seq_value
                if isinstance(seq_value, int) and not isinstance(seq_value, bool)
                else 0
            )
            previous_seq = self._seen_seq.get(raw_session_id)
            seq_grew = previous_seq is not None and seq > previous_seq
            self._seen_seq[raw_session_id] = seq

            try:
                transcript = _transcript_path(dsh_home, cwd, raw_session_id)
            except ValueError:
                continue
            try:
                transcript_mtime = transcript.stat().st_mtime
            except OSError:
                transcript_mtime = 0.0
            updated_at = transcript_mtime or created_epoch or index_mtime
            if seq_grew:
                updated_at = max(updated_at, index_mtime)

            title_row = as_mapping(rows.get("title"))
            title_value = title_row.get("val")
            title = title_value if isinstance(title_value, str) else ""
            plan = dict(as_mapping(as_mapping(rows.get("plan")).get("val")))
            pending = as_mapping(stats.get("pendingCalls"))
            open_evidence = (
                stats.get("openStep") is not None
                or bool(pending)
                or plan.get("running") is not None
            )
            extra: Dict[str, Any] = {
                "source": "session_projcache",
                "index": str(index_path),
                "created_at": created_at,
                "seq": seq,
                "seq_grew": seq_grew,
                "stats": stats,
                "plan": plan,
                "open_evidence": open_evidence,
                "transcript_exists": transcript_mtime > 0,
                "replay_available": transcript_mtime > 0 and shutil.which("zstd") is not None,
            }
            session = Session(
                agent="dsh",
                session_id=raw_session_id,
                project=cwd,
                transcript=str(transcript),
                updated_at=updated_at,
                title=snip(title or Path(cwd).name or cwd, 160),
                extra=extra,
            )
            session.status = self.infer_status(session) or Status.IDLE
            sessions.append(session)
        return sessions

    def normalize(self, record: Mapping[str, Any], session: Session) -> Iterable[Event]:
        native_type = str(record.get("type") or "")
        data = as_mapping(record.get("data"))
        timestamp = local_timestamp(
            record.get("createdAt") if native_type == "session" else record.get("time"),
            fallback=session.updated_at,
        )

        if native_type == "session":
            text = snip(record.get("cwd") or record.get("id") or "session")
            return [
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    "session",
                    text,
                    _event_extra(
                        record,
                        version=record.get("version"),
                        parent_id=record.get("parentSession"),
                    ),
                )
            ]
        if native_type in ("turn/start", "turn/end", "step/start", "step/end"):
            kind = native_type.replace("/", "_")
            number = data.get("turn") if native_type.startswith("turn/") else data.get("step")
            reason = as_mapping(data.get("reason")).get("kind")
            text = "{} {}".format(kind, number if number is not None else "").strip()
            if reason:
                text = "{}: {}".format(text, reason)
            return [
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    kind,
                    text,
                    _event_extra(record, turn=data.get("turn"), step=data.get("step")),
                )
            ]
        if native_type == "agent/inbox/spliced":
            events: List[Event] = []
            inserted = data.get("inserted")
            if isinstance(inserted, Sequence) and not isinstance(
                inserted, (str, bytes, bytearray)
            ):
                for message in inserted:
                    if isinstance(message, Mapping):
                        events.extend(
                            content_block_events(
                                message.get("content"),
                                session,
                                timestamp,
                                "user",
                                _event_extra(record),
                                _render_content_block,
                            )
                        )
            return events
        if native_type in ("user/message", "assistant/message"):
            message = (
                data
                if native_type == "user/message"
                else as_mapping(data.get("message"))
            )
            default_kind = "user" if native_type == "user/message" else "assistant"
            return content_block_events(
                message.get("content"),
                session,
                timestamp,
                default_kind,
                _event_extra(record),
                _render_content_block,
            )
        if native_type == "tool/call":
            name = str(data.get("name") or "tool")
            arguments = data.get("arguments")
            if not isinstance(arguments, str):
                arguments = compact_json(arguments or {})
            return [
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    "tool_call",
                    snip("{} {}".format(name, arguments), 120),
                    _event_extra(
                        record,
                        call_id=data.get("callId"),
                        turn=data.get("turn"),
                        step=data.get("step"),
                    ),
                )
            ]
        if native_type == "tool/result":
            message = as_mapping(data.get("message"))
            return [
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    "tool_result",
                    snip(text_content(message.get("content")), 80),
                    _event_extra(
                        record,
                        turn=data.get("turn"),
                        step=data.get("step"),
                        error=data.get("error"),
                    ),
                )
            ]
        return []

    def infer_status(self, session: Session, now: Optional[float] = None) -> Optional[Status]:
        current = time.time() if now is None else now
        if current - session.updated_at >= _ACTIVE_WINDOW_S:
            return Status.IDLE
        if session.extra.get("open_evidence") or session.extra.get("seq_grew"):
            return Status.WORKING
        return Status.WAITING

    def replay(
        self,
        session: Session,
        after_seq: Optional[int] = None,
        max_output_bytes: int = _REPLAY_BYTES,
        max_records: int = _REPLAY_RECORDS,
        timeout: float = _REPLAY_TIMEOUT_S,
        max_retained_bytes: int = _REPLAY_RETAINED_BYTES,
    ) -> List[Dict[str, Any]]:
        if not session.transcript:
            return []
        return replay_dsh_events(
            Path(session.transcript),
            after_seq=after_seq,
            max_output_bytes=max_output_bytes,
            max_records=max_records,
            timeout=timeout,
            max_retained_bytes=max_retained_bytes,
        )


__all__ = ["DSHAdapter", "replay_dsh_events"]
