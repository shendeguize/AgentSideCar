"""GitHub Copilot CLI workspace metadata adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from sidecar.adapters.base import Adapter, created_at_extra, snip, timestamp_epoch
from sidecar.model import Event, Session, Status

_WORKSPACE_BYTES = 64 * 1024


def _strip_scalar_comment(value: str) -> str:
    quote = ""
    index = 0
    while index < len(value):
        character = value[index]
        if quote == '"':
            if character == "\\":
                index += 2
                continue
            if character == '"':
                quote = ""
        elif quote == "'":
            if (
                character == "'"
                and index + 1 < len(value)
                and value[index + 1] == "'"
            ):
                index += 2
                continue
            if character == "'":
                quote = ""
        elif character in ("'", '"'):
            quote = character
        elif character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
        index += 1
    return value


def _yaml_scalar(value: str) -> Any:
    text = _strip_scalar_comment(value.strip())
    if not text:
        return ""
    if text.startswith('"') and text.endswith('"'):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text[1:-1]
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")

    lowered = text.lower()
    if lowered in ("null", "~"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        return text


def _workspace_metadata(path: Path) -> Dict[str, Any]:
    try:
        with path.open("rb") as stream:
            data = stream.read(_WORKSPACE_BYTES + 1)
    except OSError:
        return {}

    if len(data) > _WORKSPACE_BYTES:
        data = data[:_WORKSPACE_BYTES]
        if not data.endswith((b"\n", b"\r")):
            complete, separator, _ = data.rpartition(b"\n")
            data = complete + separator if separator else b""

    metadata: Dict[str, Any] = {}
    for raw_line in data.decode("utf-8", "replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key or key.startswith("-") or raw_line[:1].isspace():
            continue
        metadata[key] = _yaml_scalar(value)
    return metadata


def _metadata_string(metadata: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _metadata_timestamp(metadata: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        parsed = timestamp_epoch(metadata.get(key))
        if parsed is not None:
            return parsed
    return None


def _metadata_string(metadata: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class CopilotAdapter(Adapter):
    name = "copilot"
    agent_names = ("copilot",)

    def discover(self, home: Path) -> Iterable[Session]:
        sessions = []
        state_root = home / ".copilot" / "session-state"
        for workspace in state_root.glob("*/workspace.yaml"):
            try:
                file_updated_at = workspace.stat().st_mtime
            except OSError:
                continue
            metadata = _workspace_metadata(workspace)
            session_id = _metadata_string(metadata, "id", "session_id") or workspace.parent.name
            project = _metadata_string(metadata, "cwd", "git_root", "workspace")
            title = _metadata_string(metadata, "title", "name", "summary")
            stored_updated_at = _metadata_timestamp(
                metadata,
                "updated_at",
                "updatedAt",
                "created_at",
                "createdAt",
            )
            updated_at = (
                file_updated_at if stored_updated_at is None else stored_updated_at
            )
            extra = dict(metadata)
            extra["source"] = "workspace"
            extra["workspace"] = str(workspace)
            extra.update(
                created_at_extra(metadata.get("created_at"), metadata.get("createdAt"))
            )
            model = _metadata_string(
                metadata,
                "model",
                "model_name",
                "modelName",
                "model_id",
                "modelId",
            )
            model_provider = _metadata_string(
                metadata,
                "model_provider",
                "modelProvider",
                "provider",
            )
            if model:
                extra["model"] = model
            if model_provider:
                extra["model_provider"] = model_provider
            sessions.append(
                Session(
                    agent="copilot",
                    session_id=session_id,
                    project=project,
                    transcript="",
                    updated_at=updated_at,
                    title=snip(title, 160),
                    extra=extra,
                )
            )
        return sessions

    def normalize(self, record: Mapping[str, Any], session: Session) -> Iterable[Event]:
        del record, session
        return []

    def infer_status(self, session: Session, now: Optional[float] = None) -> Optional[Status]:
        del session, now
        return Status.IDLE


__all__ = ["CopilotAdapter"]
