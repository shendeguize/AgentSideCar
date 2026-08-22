"""Shared, dependency-free data model for agent sessions and events."""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from sidecar.presentation import format_age_seconds


class Status(str, Enum):
    """Lifecycle state exposed by the sidecar."""

    WORKING = "working"
    WAITING = "waiting"
    IDLE = "idle"
    DEAD = "dead"


def _json_value(value: Any) -> Any:
    """Convert common stdlib values into JSON-compatible structures."""

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


@dataclass
class Session:
    """One locally persisted agent session."""

    agent: str
    session_id: str
    project: str
    transcript: str
    updated_at: float
    title: str = ""
    status: Status = Status.IDLE
    extra: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, Status):
            self.status = Status(self.status)

    def age_seconds(self, now: Optional[float] = None) -> float:
        """Return a dynamic age; callers may inject ``now`` for determinism."""

        current = time.time() if now is None else now
        return max(0.0, current - self.updated_at)

    def age_str(self, now: Optional[float] = None) -> str:
        return format_age_seconds(self.age_seconds(now))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent,
            "session_id": self.session_id,
            "project": self.project,
            "transcript": self.transcript,
            "updated_at": self.updated_at,
            "title": self.title,
            "status": self.status.value,
            "extra": _json_value(self.extra),
            "parent_id": self.parent_id,
        }


@dataclass
class Event:
    """A normalized transcript event."""

    ts: str
    agent: str
    session_id: str
    kind: str
    text: str
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "agent": self.agent,
            "session_id": self.session_id,
            "kind": self.kind,
            "text": self.text,
            "extra": _json_value(self.extra),
        }
