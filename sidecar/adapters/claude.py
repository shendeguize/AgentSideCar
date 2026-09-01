"""Claude Code transcript adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Hashable, Iterable, List, Mapping, Optional, Tuple

from sidecar.adapters.base import (
    Adapter,
    DEFAULT_METADATA_CACHE_SIZE,
    MetadataCache,
    compact_json,
    content_items,
    created_at_extra,
    file_signature,
    local_timestamp,
    read_jsonl_prefix,
    snip,
    text_content,
)
from sidecar.adapters.replay import JsonlReplayMixin
from sidecar.model import Event, Session


_PARENT_SESSION_KEYS = (
    "parentSessionId",
    "parentSessionID",
    "parent_session_id",
    "parentSession",
)


def _session_identifier(value: Any) -> Optional[str]:
    """Return a conservative transcript identifier from untrusted metadata."""

    if not isinstance(value, str):
        return None
    identifier = value.strip()
    if (
        not identifier
        or len(identifier) > 512
        or "/" in identifier
        or "\\" in identifier
        or any(
            ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F
            for character in identifier
        )
    ):
        return None
    return identifier


def _record_string(record: Mapping[str, Any], *keys: str) -> str:
    candidates = [record]
    for nested_key in ("message", "metadata", "model"):
        nested = record.get(nested_key)
        if isinstance(nested, Mapping):
            candidates.append(nested)
    for candidate in candidates:
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _claude_metadata(
    path: Path,
) -> Tuple[str, str, str, bool, Optional[str], str, str, Any]:
    """Extract bounded discovery metadata; ai-title wins over prompt text."""

    cwd = ""
    session_id = ""
    ai_title = ""
    first_user = ""
    sidechain = False
    explicit_parent: Optional[str] = None
    model = ""
    model_provider = ""
    created_at: Any = None
    for record in read_jsonl_prefix(path):
        if created_at is None:
            created_at = record.get("timestamp")
        if not cwd and isinstance(record.get("cwd"), str):
            cwd = record["cwd"]
        if not session_id:
            session_id = _session_identifier(record.get("sessionId")) or ""
        model = model or _record_string(
            record,
            "model",
            "model_name",
            "modelName",
            "model_id",
            "modelId",
        )
        model_provider = model_provider or _record_string(
            record,
            "model_provider",
            "modelProvider",
            "provider",
        )
        if explicit_parent is None:
            for key in _PARENT_SESSION_KEYS:
                explicit_parent = _session_identifier(record.get(key))
                if explicit_parent is not None:
                    break
        sidechain = sidechain or record.get("isSidechain") is True
        if record.get("type") == "ai-title":
            candidate = record.get("aiTitle") or record.get("title")
            if isinstance(candidate, str) and candidate.strip():
                ai_title = candidate
        if not first_user and record.get("type") == "user":
            message = record.get("message")
            if not isinstance(message, Mapping):
                message = {}
            for item in content_items(message.get("content")):
                if item.get("type") == "text" and item.get("text"):
                    first_user = str(item["text"])
                    break
    return (
        cwd,
        session_id,
        snip(ai_title or first_user, 160),
        sidechain,
        explicit_parent,
        model,
        model_provider,
        created_at,
    )


class ClaudeAdapter(JsonlReplayMixin, Adapter):
    name = "claude"
    agent_names = ("claude",)

    def __init__(
        self,
        metadata_cache_size: int = DEFAULT_METADATA_CACHE_SIZE,
    ) -> None:
        self._metadata_cache: MetadataCache[
            Tuple[str, str, str, bool, Optional[str], str, str, Any]
        ] = MetadataCache(metadata_cache_size)

    def discover(self, home: Path) -> Iterable[Session]:
        sessions: List[Session] = []
        active_signatures: List[Hashable] = []
        projects_root = home / ".claude" / "projects"
        for project_dir in projects_root.glob("*"):
            if not project_dir.is_dir():
                continue
            for transcript in project_dir.rglob("*.jsonl"):
                signature = file_signature(transcript)
                if signature is None:
                    continue
                active_signatures.append(signature)
                updated_at = signature[1] / 1e9
                (
                    cwd,
                    stored_id,
                    title,
                    sidechain,
                    explicit_parent,
                    model,
                    model_provider,
                    created_at,
                ) = self._metadata_cache.get_or_load(
                    signature, lambda: _claude_metadata(transcript)
                )
                relative_parts = transcript.relative_to(project_dir).parts
                path_parent: Optional[str] = None
                if len(relative_parts) >= 3 and relative_parts[-2] == "subagents":
                    path_parent = _session_identifier(relative_parts[-3])
                parent_id = path_parent or explicit_parent
                child_id = transcript.stem
                # Current Claude sidechain records retain the owning main
                # session in sessionId. Only use that convention when it
                # cannot be mistaken for the child transcript's own ID.
                if (
                    parent_id is None
                    and sidechain
                    and stored_id
                    and stored_id != child_id
                ):
                    parent_id = stored_id
                if parent_id == child_id:
                    parent_id = None
                extra: dict = {"source": "transcript", "sidechain": sidechain}
                extra.update(created_at_extra(created_at))
                if model:
                    extra["model"] = model
                if model_provider:
                    extra["model_provider"] = model_provider
                sessions.append(
                    Session(
                        agent="claude",
                        session_id=(
                            child_id
                            if sidechain or parent_id
                            else stored_id or child_id
                        ),
                        project=cwd or project_dir.name,
                        transcript=str(transcript),
                        updated_at=updated_at,
                        title=title,
                        parent_id=parent_id,
                        extra=extra,
                    )
                )
        self._metadata_cache.prune(active_signatures)
        return sessions

    def normalize(self, record: Mapping[str, Any], session: Session) -> Iterable[Event]:
        record_type = str(record.get("type") or "")
        timestamp = local_timestamp(record.get("timestamp"))
        message = record.get("message")
        if not isinstance(message, Mapping):
            message = {}

        if record_type in ("thinking", "tool_use", "tool_result"):
            content: Any = record
        elif record_type in ("user", "assistant"):
            content = message.get("content")
        else:
            return []

        events: List[Event] = []
        for item in content_items(content):
            item_type = item.get("type")
            if item_type == "text":
                text = snip(item.get("text"))
                if text:
                    events.append(
                        Event(timestamp, session.agent, session.session_id, record_type, text)
                    )
            elif item_type == "thinking":
                text = snip(item.get("thinking"), 80)
                if text:
                    events.append(
                        Event(
                            timestamp,
                            session.agent,
                            session.session_id,
                            "thinking",
                            text,
                        )
                    )
            elif item_type == "tool_use":
                name = str(item.get("name") or "tool")
                details = snip(compact_json(item.get("input") or {}), 80)
                events.append(
                    Event(
                        timestamp,
                        session.agent,
                        session.session_id,
                        "tool_call",
                        snip("{} {}".format(name, details), 120),
                    )
                )
            elif item_type == "tool_result":
                events.append(
                    Event(
                        timestamp,
                        session.agent,
                        session.session_id,
                        "tool_result",
                        snip(text_content(item.get("content")), 80),
                    )
                )
        return events
