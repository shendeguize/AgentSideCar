"""Kimi Code session-index, state, and wire transcript adapter."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sidecar.adapters.base import (
    Adapter,
    ContentBlockEvent,
    as_mapping,
    compact_json,
    content_block_events,
    epoch_seconds,
    local_timestamp,
    read_json_object,
    snip,
    text_content,
)
from sidecar.kimi_identity import read_kimi_index_metadata
from sidecar.model import Event, Session, Status

_ACTIVE_WINDOW_S = 120.0
_INDEX_BYTES = 4 * 1024 * 1024
_JSON_BYTES = 512 * 1024
_TITLE_PREFIX_BYTES = 256 * 1024
_TITLE_LINE_BYTES = 64 * 1024
_CONTENT_DEDUP_WINDOW_S = 1.0
_CONTENT_DEDUP_KEYS = 512


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _configured_home(home: Path) -> Path:
    configured = os.environ.get("KIMI_CODE_HOME")
    if configured:
        expanded = os.path.expandvars(os.path.expanduser(configured))
        return Path(expanded)
    return home / ".kimi-code"


def _session_dir(root: Path, value: Any) -> Optional[Path]:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(os.path.expanduser(os.path.expandvars(value.strip())))
    return candidate if candidate.is_absolute() else root / candidate


def _first_turn_prompt(path: Path) -> str:
    """Extract a title without decoding unrelated, potentially huge wire rows."""

    try:
        stream = path.open("rb")
    except OSError:
        return ""
    consumed = 0
    line = bytearray()
    oversized = False
    try:
        while consumed < _TITLE_PREFIX_BYTES:
            piece = stream.readline(
                min(_TITLE_LINE_BYTES, _TITLE_PREFIX_BYTES - consumed)
            )
            if not piece:
                break
            consumed += len(piece)
            complete = piece.endswith(b"\n")
            if oversized:
                if complete:
                    oversized = False
                continue
            line.extend(piece)
            if len(line) > _TITLE_LINE_BYTES:
                line.clear()
                oversized = not complete
                continue
            if not complete and consumed < _TITLE_PREFIX_BYTES:
                continue
            raw = bytes(line)
            line.clear()
            if b"turn.prompt" not in raw:
                continue
            try:
                record = json.loads(raw.decode("utf-8", "replace"))
            except (json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(record, Mapping) or record.get("type") != "turn.prompt":
                continue
            inputs = record.get("input")
            if not isinstance(inputs, Sequence) or isinstance(
                inputs, (str, bytes, bytearray)
            ):
                return ""
            for item in inputs:
                if isinstance(item, Mapping) and item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        return snip(text, 160)
            return ""
    finally:
        stream.close()
    return ""


def _agent_home(session_dir: Path, agent_id: str, metadata: Mapping[str, Any]) -> Path:
    configured = metadata.get("homedir")
    if isinstance(configured, str) and configured.strip():
        candidate = Path(
            os.path.expandvars(os.path.expanduser(configured.strip()))
        )
        if not candidate.is_absolute():
            candidate = session_dir / candidate
        return candidate
    return session_dir / "agents" / agent_id


def _child_session_id(session_id: str, agent_id: str) -> str:
    return "{}:{}".format(session_id, agent_id)


def _parent_session_id(
    session_id: str, agent_id: str, metadata: Mapping[str, Any]
) -> str:
    parent = (
        metadata.get("parent_id")
        or metadata.get("parentId")
        or metadata.get("parentAgentId")
        or metadata.get("parent")
    )
    if isinstance(parent, str) and parent and parent != "main" and parent != agent_id:
        return _child_session_id(session_id, parent)
    return session_id


def _event_extra(record: Mapping[str, Any], **values: Any) -> Dict[str, Any]:
    extra: Dict[str, Any] = {
        "native_type": str(record.get("type") or ""),
        "agent_id": str(record.get("agentId") or ""),
    }
    extra.update({key: value for key, value in values.items() if value is not None})
    return extra


def _metadata_string(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _render_content_block(
    item: Mapping[str, Any],
    role: str,
) -> Optional[ContentBlockEvent]:
    item_type = str(item.get("type") or "")
    if item_type == "text":
        kind = "tool_result" if role in ("tool", "tool_result") else role
        return kind, item.get("text"), 120
    if item_type in ("reasoning", "thinking"):
        return "thinking", item.get("text") or item.get("thinking"), 80
    if item_type in ("tool-call", "tool_call", "tool_use"):
        name = item.get("name") or item.get("toolName") or "tool"
        arguments = item.get("arguments", item.get("input", {}))
        if not isinstance(arguments, str):
            arguments = compact_json(arguments)
        return "tool_call", "{} {}".format(name, arguments), 120
    if item_type in ("tool-result", "tool_result"):
        return "tool_result", text_content(item.get("content")), 80
    return None


def _tool_call_events(
    record: Mapping[str, Any], session: Session, calls: Any
) -> List[Event]:
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes, bytearray)):
        return []
    timestamp = local_timestamp(record.get("time"), fallback=session.updated_at)
    events: List[Event] = []
    for call in calls:
        if not isinstance(call, Mapping):
            continue
        function = as_mapping(call.get("function"))
        name = call.get("name") or call.get("toolName") or function.get("name") or "tool"
        arguments = call.get("arguments", call.get("input", function.get("arguments", {})))
        if not isinstance(arguments, str):
            arguments = compact_json(arguments)
        events.append(
            Event(
                timestamp,
                session.agent,
                session.session_id,
                "tool_call",
                snip("{} {}".format(name, arguments), 120),
                _event_extra(record, call_id=call.get("id")),
            )
        )
    return events


def _loop_event_extra(
    record: Mapping[str, Any],
    loop_event: Mapping[str, Any],
    **values: Any,
) -> Dict[str, Any]:
    metadata = {
        "loop_event_type": loop_event.get("type"),
        "event_uuid": loop_event.get("uuid"),
        "parent_uuid": loop_event.get("parentUuid"),
        "turn_id": loop_event.get("turnId"),
        "step": loop_event.get("step"),
        "step_uuid": loop_event.get("stepUuid"),
        "call_id": loop_event.get("toolCallId"),
    }
    metadata.update(values)
    return _event_extra(record, **metadata)


def _loop_events(record: Mapping[str, Any], session: Session) -> List[Event]:
    """Normalize the compact, durable events written by Kimi's agent loop."""

    loop_event = as_mapping(record.get("event"))
    loop_type = str(loop_event.get("type") or "")
    timestamp = local_timestamp(record.get("time"), fallback=session.updated_at)

    if loop_type == "content.part":
        part = as_mapping(loop_event.get("part"))
        part_type = str(part.get("type") or "")
        if part_type == "text":
            kind = "assistant"
            text = snip(part.get("text"))
        elif part_type in ("think", "thinking", "reasoning"):
            kind = "thinking"
            text = snip(
                part.get("think") or part.get("thinking") or part.get("text"),
                80,
            )
        else:
            return []
        if not text:
            return []
        return [
            Event(
                timestamp,
                session.agent,
                session.session_id,
                kind,
                text,
                _loop_event_extra(record, loop_event, part_type=part_type),
            )
        ]

    if loop_type == "tool.call":
        name = loop_event.get("name") or loop_event.get("toolName") or "tool"
        arguments = loop_event.get(
            "args",
            loop_event.get("arguments", loop_event.get("input", {})),
        )
        if not isinstance(arguments, str):
            arguments = compact_json(arguments)
        return [
            Event(
                timestamp,
                session.agent,
                session.session_id,
                "tool_call",
                snip("{} {}".format(name, arguments), 120),
                _loop_event_extra(record, loop_event, tool_name=name),
            )
        ]

    if loop_type == "tool.result":
        return [
            Event(
                timestamp,
                session.agent,
                session.session_id,
                "tool_result",
                snip(text_content(loop_event.get("result")), 80),
                _loop_event_extra(record, loop_event),
            )
        ]

    # step.begin/step.end are lifecycle envelopes. Their usage is already
    # represented by usage.record, and message-shaped loop events are left to
    # context.append_message so user/assistant content is not doubled.
    return []


class KimiAdapter(Adapter):
    name = "kimi"
    agent_names = ("kimi",)

    def __init__(self) -> None:
        self._recent_content: Dict[
            Tuple[str, str], Tuple[str, str, float]
        ] = {}

    def _deduplicate_content(
        self,
        record: Mapping[str, Any],
        session: Session,
        events: Sequence[Event],
    ) -> List[Event]:
        """Drop only adjacent cross-record message mirrors, never tool events."""

        source = str(record.get("type") or "")
        normalized_time = epoch_seconds(record.get("time"))
        record_time = normalized_time if normalized_time is not None else 0.0
        output: List[Event] = []
        for event in events:
            if event.kind not in ("user", "assistant") or not record_time:
                output.append(event)
                continue

            key = (session.session_id, event.kind)
            previous = self._recent_content.pop(key, None)
            duplicate = (
                previous is not None
                and previous[0] == event.text
                and previous[1] != source
                and "context.append_message" in (previous[1], source)
                and abs(previous[2] - record_time) <= _CONTENT_DEDUP_WINDOW_S
            )
            self._recent_content[key] = (event.text, source, record_time)
            if not duplicate:
                output.append(event)

        while len(self._recent_content) > _CONTENT_DEDUP_KEYS:
            del self._recent_content[next(iter(self._recent_content))]
        return output

    def discover(self, home: Path) -> Iterable[Session]:
        root = _configured_home(home)
        index_path = root / "session_index.jsonl"
        index_records, _index_valid = read_kimi_index_metadata(
            index_path,
            max_bytes=_INDEX_BYTES,
        )
        latest: Dict[str, Mapping[str, Any]] = {}
        for record in index_records:
            session_id = record.get("sessionId")
            if isinstance(session_id, str) and session_id:
                latest[session_id] = record

        workspaces_doc = read_json_object(root / "workspaces.json", _JSON_BYTES)
        workspaces = as_mapping(workspaces_doc.get("workspaces"))
        sessions: List[Session] = []
        for indexed_id, index_record in latest.items():
            directory = _session_dir(root, index_record.get("sessionDir"))
            if directory is None:
                continue
            state_path = directory / "state.json"
            state = read_json_object(state_path, _JSON_BYTES)
            state_id = state.get("id")
            session_id = state_id if isinstance(state_id, str) and state_id else indexed_id
            state_agents = as_mapping(state.get("agents"))

            agent_ids: Set[str] = {
                str(agent_id)
                for agent_id in state_agents
                if isinstance(agent_id, str) and agent_id
            }
            agents_root = directory / "agents"
            try:
                agent_ids.update(
                    child.name for child in agents_root.iterdir() if child.is_dir()
                )
            except OSError:
                pass
            agent_ids.add("main")
            subagents = sorted(agent_id for agent_id in agent_ids if agent_id != "main")

            workspace_id = directory.parent.name
            workspace = as_mapping(workspaces.get(workspace_id))
            project = ""
            for candidate in (
                index_record.get("workDir"),
                state.get("cwd"),
                workspace.get("root"),
            ):
                if isinstance(candidate, str) and candidate.strip():
                    project = candidate.strip()
                    break

            main_meta = as_mapping(state_agents.get("main"))
            main_home = _agent_home(directory, "main", main_meta)
            transcript = main_home / "wire.jsonl"
            normalized_updated = epoch_seconds(state.get("updatedAt"))
            state_updated = (
                normalized_updated if normalized_updated is not None else 0.0
            )
            updated_at = state_updated or _mtime(state_path) or _mtime(transcript)
            reason_present = "lastTurnReason" in state
            model = _metadata_string(
                state,
                "model",
                "model_name",
                "modelName",
                "model_id",
                "modelId",
            ) or _metadata_string(
                main_meta,
                "model",
                "model_name",
                "modelName",
                "model_id",
                "modelId",
            )
            model_provider = _metadata_string(
                state,
                "model_provider",
                "modelProvider",
                "provider",
            ) or _metadata_string(
                main_meta,
                "model_provider",
                "modelProvider",
                "provider",
            )
            common_extra: Dict[str, Any] = {
                "source": "session_index",
                "session_dir": str(directory),
                "state": str(state_path),
                "created_at": state.get("createdAt"),
                "last_turn_reason": state.get("lastTurnReason"),
                "last_turn_reason_present": reason_present,
                "archived": state.get("archived") is True,
                "is_custom_title": state.get("isCustomTitle") is True,
                "workspace_id": workspace_id,
                "workspace": dict(workspace),
                "agent_id": "main",
                "subagents": subagents,
            }
            if model:
                common_extra["model"] = model
            if model_provider:
                common_extra["model_provider"] = model_provider
            main = Session(
                agent="kimi",
                session_id=session_id,
                project=project,
                transcript=str(transcript),
                updated_at=updated_at,
                title=_first_turn_prompt(transcript),
                extra=common_extra,
            )
            main.status = self.infer_status(main) or Status.IDLE
            sessions.append(main)

            for agent_id in subagents:
                metadata = as_mapping(state_agents.get(agent_id))
                child_home = _agent_home(directory, agent_id, metadata)
                child_transcript = child_home / "wire.jsonl"
                child_updated = _mtime(child_transcript) or updated_at
                child_extra = dict(common_extra)
                child_extra.update(
                    {
                        "agent_id": agent_id,
                        "agent_type": metadata.get("type"),
                        "agent_home": str(child_home),
                        "subagent": True,
                        "parent_agent_id": metadata.get("parent_id")
                        or metadata.get("parentId")
                        or metadata.get("parentAgentId")
                        or "main",
                    }
                )
                child_model = _metadata_string(
                    metadata,
                    "model",
                    "model_name",
                    "modelName",
                    "model_id",
                    "modelId",
                )
                child_provider = _metadata_string(
                    metadata,
                    "model_provider",
                    "modelProvider",
                    "provider",
                )
                if child_model:
                    child_extra["model"] = child_model
                if child_provider:
                    child_extra["model_provider"] = child_provider
                child = Session(
                    agent="kimi",
                    session_id=_child_session_id(session_id, agent_id),
                    project=project,
                    transcript=str(child_transcript),
                    updated_at=child_updated,
                    title=_first_turn_prompt(child_transcript),
                    parent_id=_parent_session_id(session_id, agent_id, metadata),
                    extra=child_extra,
                )
                child.status = self.infer_status(child) or Status.IDLE
                sessions.append(child)
        return sessions

    def normalize(self, record: Mapping[str, Any], session: Session) -> Iterable[Event]:
        native_type = str(record.get("type") or "")
        timestamp = local_timestamp(record.get("time"), fallback=session.updated_at)

        if native_type == "turn.prompt":
            origin = as_mapping(record.get("origin"))
            events = content_block_events(
                record.get("input"),
                session,
                timestamp,
                "user",
                _event_extra(record),
                _render_content_block,
            )
            for event in events:
                event.extra["origin"] = origin.get("kind")
            return self._deduplicate_content(record, session, events)
        if native_type == "context.append_message":
            message = as_mapping(record.get("message"))
            role = str(message.get("role") or "message")
            events = content_block_events(
                message.get("content"),
                session,
                timestamp,
                role,
                _event_extra(record),
                _render_content_block,
            )
            events.extend(
                _tool_call_events(record, session, message.get("toolCalls"))
            )
            return self._deduplicate_content(record, session, events)
        if native_type == "context.append_loop_event":
            return self._deduplicate_content(
                record, session, _loop_events(record, session)
            )
        if native_type == "llm.request":
            # Request rows can contain full system prompts and tool snapshots.
            return []
        if native_type == "usage.record":
            usage = dict(as_mapping(record.get("usage")))
            model = str(record.get("model") or "")
            text = "{} {}".format(model, compact_json(usage)).strip()
            return [
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    "token",
                    snip(text, 160),
                    _event_extra(
                        record,
                        model=model,
                        usage=usage,
                        scope=record.get("usageScope"),
                    ),
                )
            ]
        if native_type == "turn.ended":
            reason = str(record.get("reason") or "ended")
            return [
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    "turn_end",
                    reason,
                    _event_extra(
                        record,
                        turn_id=record.get("turnId"),
                        duration_ms=record.get("durationMs"),
                        reason=reason,
                    ),
                )
            ]
        return []

    def infer_status(self, session: Session, now: Optional[float] = None) -> Optional[Status]:
        current = time.time() if now is None else now
        if session.extra.get("archived") or current - session.updated_at >= _ACTIVE_WINDOW_S:
            return Status.IDLE
        if not session.extra.get("last_turn_reason_present"):
            return Status.WORKING
        return Status.WAITING


__all__ = ["KimiAdapter"]
