"""Immutable remote-watch items and strict event validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Mapping, Union

from sidecar.json_limits import (
    JSONLimitError,
    JSONLimits,
    JSONSyntaxError,
    validate_json,
)
from sidecar.remote_types import (
    FAILURE_CODES,
    ProtocolResourceLimitError,
    _validate_alias,
)


WATCH_FAILURE_CODES = frozenset(FAILURE_CODES)
WATCH_STARTUP_TIMEOUT_SECONDS = 15.0
WATCH_JOIN_TIMEOUT_SECONDS = 3.0
WATCH_QUEUE_POLL_SECONDS = 0.05
DEFAULT_WATCH_QUEUE_ITEMS = 256
MAX_WATCH_QUEUE_ITEMS = 4096
MAX_WATCH_LINE_BYTES = 256 * 1024
MAX_WATCH_STDERR_BYTES = 64 * 1024
MAX_WATCH_JSON_DEPTH = 32
MAX_WATCH_JSON_NODES = 8192
MAX_WATCH_EXTRA_DEPTH = 24
MAX_WATCH_EXTRA_NODES = 4096
MAX_WATCH_EXTRA_BYTES = 128 * 1024
MAX_WATCH_JSON_STRING_BYTES = 192 * 1024

_EVENT_KEYS = frozenset(("ts", "agent", "session_id", "kind", "text", "extra"))
_EVENT_STRING_LIMITS = {
    "ts": 1024,
    "agent": 256,
    "session_id": 4096,
    "kind": 1024,
    "text": MAX_WATCH_JSON_STRING_BYTES,
}


def _validated_text(
    value: object,
    key: str,
    *,
    required: bool,
) -> str:
    if not isinstance(value, str) or (required and not value):
        raise ValueError("invalid remote watch event field")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as error:
        raise ValueError("invalid remote watch event field") from error
    if len(encoded) > _EVENT_STRING_LIMITS[key]:
        raise ProtocolResourceLimitError("remote watch event field exceeds limit")
    return value


def _validate_extra(extra: object) -> Mapping[str, Any]:
    if not isinstance(extra, Mapping):
        raise ValueError("invalid remote watch event extra")
    try:
        validate_json(
            extra,
            JSONLimits(
                max_depth=MAX_WATCH_EXTRA_DEPTH,
                max_nodes=MAX_WATCH_EXTRA_NODES,
                max_string_bytes=MAX_WATCH_JSON_STRING_BYTES,
                max_integer_bits=4096,
            ),
        )
        encoded = json.dumps(
            extra,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except JSONLimitError as error:
        raise ProtocolResourceLimitError(
            "remote watch event extra exceeds limit"
        ) from error
    except (
        JSONSyntaxError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as error:
        raise ValueError("invalid remote watch event extra") from error
    if len(encoded) > MAX_WATCH_EXTRA_BYTES:
        raise ProtocolResourceLimitError("remote watch event extra exceeds limit")
    return extra


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class RemoteWatchReady:
    """A host has emitted the exact watch readiness control frame."""

    host: str

    def __post_init__(self) -> None:
        _validate_alias(self.host)

    def to_dict(self) -> Dict[str, str]:
        return {"type": "ready", "host": self.host}


@dataclass(frozen=True)
class RemoteWatchFailure:
    """A terminal host failure with no raw remote diagnostics."""

    host: str
    code: str

    def __post_init__(self) -> None:
        _validate_alias(self.host)
        if self.code not in WATCH_FAILURE_CODES:
            raise ValueError("invalid remote watch failure code")

    @property
    def terminal(self) -> bool:
        return True

    @property
    def events_may_be_missed(self) -> bool:
        return True

    def to_dict(self) -> Dict[str, object]:
        return {
            "type": "failure",
            "host": self.host,
            "code": self.code,
            "terminal": True,
            "events_may_be_missed": True,
        }


@dataclass(frozen=True)
class RemoteWatchEvent:
    """One validated event with immutable host provenance."""

    host: str
    ts: str
    agent: str
    session_id: str
    kind: str
    text: str
    extra: Mapping[str, Any]

    def __post_init__(self) -> None:
        _validate_alias(self.host)
        _validated_text(self.ts, "ts", required=True)
        _validated_text(self.agent, "agent", required=True)
        _validated_text(self.session_id, "session_id", required=True)
        _validated_text(self.kind, "kind", required=True)
        _validated_text(self.text, "text", required=False)
        validated_extra = _validate_extra(self.extra)
        object.__setattr__(self, "extra", _freeze_json(validated_extra))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "agent": self.agent,
            "session_id": self.session_id,
            "kind": self.kind,
            "text": self.text,
            "extra": _thaw_json(self.extra),
            "host": self.host,
        }


RemoteWatchItem = Union[RemoteWatchReady, RemoteWatchFailure, RemoteWatchEvent]


def validate_watch_event(
    value: object,
    host: str,
) -> RemoteWatchEvent:
    """Validate an exact wire event before adding host provenance."""

    _validate_alias(host)
    if not isinstance(value, dict) or set(value) != _EVENT_KEYS:
        raise ValueError("invalid remote watch event")
    return RemoteWatchEvent(
        host=host,
        ts=_validated_text(value["ts"], "ts", required=True),
        agent=_validated_text(value["agent"], "agent", required=True),
        session_id=_validated_text(
            value["session_id"],
            "session_id",
            required=True,
        ),
        kind=_validated_text(value["kind"], "kind", required=True),
        text=_validated_text(value["text"], "text", required=False),
        extra=_validate_extra(value["extra"]),
    )


def validate_watch_queue_items(value: object) -> int:
    if type(value) is not int or not 0 < value <= MAX_WATCH_QUEUE_ITEMS:
        raise ValueError(
            "queue_items must be between 1 and {}".format(MAX_WATCH_QUEUE_ITEMS)
        )
    return value


__all__ = [
    "DEFAULT_WATCH_QUEUE_ITEMS",
    "MAX_WATCH_EXTRA_BYTES",
    "MAX_WATCH_EXTRA_DEPTH",
    "MAX_WATCH_EXTRA_NODES",
    "MAX_WATCH_JSON_DEPTH",
    "MAX_WATCH_JSON_NODES",
    "MAX_WATCH_JSON_STRING_BYTES",
    "MAX_WATCH_LINE_BYTES",
    "MAX_WATCH_QUEUE_ITEMS",
    "MAX_WATCH_STDERR_BYTES",
    "RemoteWatchEvent",
    "RemoteWatchFailure",
    "RemoteWatchItem",
    "RemoteWatchReady",
    "WATCH_FAILURE_CODES",
    "WATCH_JOIN_TIMEOUT_SECONDS",
    "WATCH_QUEUE_POLL_SECONDS",
    "WATCH_STARTUP_TIMEOUT_SECONDS",
    "validate_watch_event",
    "validate_watch_queue_items",
]
