"""Remote protocol constants, immutable models, and primitive validation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sidecar.json_limits import (
    JSONLimitError,
    JSONLimits,
    JSONSyntaxError,
    parse_json,
    validate_json,
)


ELIGIBLE_PHASES = frozenset(("ready", "no_dsh"))
FAILURE_CODES = frozenset(
    (
        "inventory",
        "host_key",
        "auth",
        "timeout",
        "unreachable",
        "python_too_old",
        "resource_limit",
        "protocol",
        "remote",
    )
)

MAX_INVENTORY_BYTES = 2 * 1024 * 1024
MAX_PROTOCOL_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_ROW_BYTES = 256 * 1024
MAX_HOSTS = 256
MAX_ROWS = 16384
MAX_AGGREGATE_BYTES = 32 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_ITEMS = MAX_ROWS * 24 + 64
MAX_JSON_EXTRA_ITEMS = 8192
MAX_JSON_STRING_BYTES = 256 * 1024
MAX_RECENT_SECONDS = 365.0 * 24.0 * 60.0 * 60.0
PROBE_TIMEOUT_SECONDS = 5.0
HOST_TIMEOUT_SECONDS = 15.0
FLEET_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_WORKERS = 4
MAX_WORKERS = 6

MIN_SESSION_TIMESTAMP = 0
MAX_SESSION_TIMESTAMP = 32503680000
_SESSION_KEYS = frozenset(
    (
        "agent",
        "session_id",
        "project",
        "transcript",
        "updated_at",
        "title",
        "status",
        "extra",
        "parent_id",
    )
)
_SESSION_STRING_LIMITS = {
    "agent": 256,
    "session_id": 4096,
    "project": 32768,
    "transcript": 32768,
    "title": 65536,
    "parent_id": 4096,
}

EXIT_OK = 0
EXIT_INVALID_INVENTORY = 2
EXIT_NO_SUCCESS = 3

_ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_PYTHON_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")
_STATUS_VALUES = frozenset(("working", "waiting", "idle", "dead"))
_COMMAND_ARGS = {
    "status": ("status", "--json"),
}


class RemoteInventoryError(ValueError):
    """A deliberately non-diagnostic inventory error safe for CLI display."""

    code = "inventory"
    exit_code = EXIT_INVALID_INVENTORY

    def __init__(self) -> None:
        super().__init__("remote inventory is unavailable or invalid")


class ProtocolResourceLimitError(ValueError):
    """A stable protocol failure for validly framed data exceeding a bound."""

    code = "resource_limit"


@dataclass(frozen=True)
class ProbeResult:
    """One validated remote Python interpreter discovery result."""

    version: Tuple[int, int, int]
    executable: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.version, tuple)
            or len(self.version) != 3
            or any(
                type(item) is not int or item < 0 or item > 999
                for item in self.version
            )
        ):
            raise ValueError("invalid remote Python version")
        _validate_python_executable(self.executable)


@dataclass(frozen=True)
class RemoteHost:
    """One eligible DSH Center SSH alias."""

    alias: str
    phase: str

    def __post_init__(self) -> None:
        _validate_alias(self.alias)
        if self.alias.casefold() == "local":
            raise ValueError("local is reserved for the synthetic local host")
        if self.phase not in ELIGIBLE_PHASES:
            raise ValueError("remote host has an ineligible phase")


@dataclass(frozen=True)
class RemoteFailure:
    """A host-scoped failure containing no subprocess diagnostics."""

    host: str
    code: str

    def __post_init__(self) -> None:
        _validate_alias(self.host)
        if self.code not in FAILURE_CODES:
            raise ValueError("invalid remote failure code")

    def to_dict(self) -> Dict[str, str]:
        return {"host": self.host, "code": self.code}


@dataclass(frozen=True)
class RemoteAggregate:
    """Deterministic fleet result with successes retained during failures."""

    command: str
    rows: Tuple[Mapping[str, Any], ...] = ()
    failures: Tuple[RemoteFailure, ...] = ()
    hosts: Tuple[str, ...] = ()
    succeeded: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _command_arguments(self.command)
        object.__setattr__(self, "rows", tuple(dict(row) for row in self.rows))
        object.__setattr__(self, "failures", tuple(self.failures))
        object.__setattr__(self, "hosts", tuple(self.hosts))
        object.__setattr__(self, "succeeded", tuple(self.succeeded))

    @property
    def partial(self) -> bool:
        return bool(self.failures and self.succeeded)

    @property
    def exit_code(self) -> int:
        if self.hosts and not self.succeeded:
            return EXIT_NO_SUCCESS
        return EXIT_OK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "rows": [dict(row) for row in self.rows],
            "failures": [failure.to_dict() for failure in self.failures],
            "partial": self.partial,
            "hosts": list(self.hosts),
            "succeeded": list(self.succeeded),
            "exit_code": self.exit_code,
        }


def _validate_alias(alias: object) -> str:
    if (
        not isinstance(alias, str)
        or not alias
        or len(alias) > 255
        or alias.startswith("-")
        or _ALIAS_RE.fullmatch(alias) is None
    ):
        raise ValueError("invalid remote host alias")
    return alias


def _validate_python_executable(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or not value.startswith("/")
        or _PYTHON_EXECUTABLE_RE.fullmatch(value) is None
        or ".." in value.split("/")
    ):
        raise ValueError("invalid remote Python executable")
    return value


def _validate_recent_seconds(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("recent_seconds must be a finite positive number")
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("recent_seconds must be a finite positive number") from error
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_RECENT_SECONDS:
        raise ValueError(
            "recent_seconds must be positive and at most {}".format(
                int(MAX_RECENT_SECONDS)
            )
        )
    return seconds


def _command_arguments(
    command: object,
    recent_seconds: Optional[float] = None,
) -> Tuple[str, ...]:
    if not isinstance(command, str) or command not in ("list", "status"):
        raise ValueError("remote command must be list or status")
    if command == "list":
        if recent_seconds is None:
            return ("list", "--json", "--all")
        seconds = _validate_recent_seconds(recent_seconds)
        return ("list", "--json", "--recent-seconds", format(seconds, ".17g"))
    if recent_seconds is not None:
        raise ValueError("recent_seconds is supported only for list")
    return _COMMAND_ARGS[command]


def _parse_bounded_json_with_limits(
    payload: object,
    *,
    max_bytes: int,
    max_depth: int,
    max_nodes: int,
    max_string_bytes: int,
) -> Any:
    try:
        return parse_json(
            payload,
            JSONLimits(
                max_bytes=max_bytes,
                max_depth=max_depth,
                max_nodes=max_nodes,
                max_string_bytes=max_string_bytes,
                max_integer_bits=4096,
            ),
        )
    except JSONLimitError as error:
        raise ProtocolResourceLimitError(str(error)) from error
    except JSONSyntaxError as error:
        raise ValueError("invalid bounded JSON payload") from error


def parse_bounded_json(payload: object, *, max_bytes: int) -> Any:
    """Parse UTF-8 JSON with duplicate-key, size, depth, and item limits."""

    return _parse_bounded_json_with_limits(
        payload,
        max_bytes=max_bytes,
        max_depth=MAX_JSON_DEPTH,
        max_nodes=MAX_JSON_ITEMS,
        max_string_bytes=MAX_JSON_STRING_BYTES,
    )


def _validate_protocol_json(value: Any, *, max_nodes: int) -> None:
    try:
        validate_json(
            value,
            JSONLimits(
                max_depth=MAX_JSON_DEPTH,
                max_nodes=max_nodes,
                max_string_bytes=MAX_JSON_STRING_BYTES,
                max_integer_bits=4096,
            ),
        )
    except JSONLimitError as error:
        raise ProtocolResourceLimitError(str(error)) from error
    except JSONSyntaxError as error:
        raise ValueError("invalid remote JSON value") from error


def _parse_one_line_json(payload: object, *, max_bytes: int) -> Any:
    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("invalid one-line protocol payload") from error
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raise ValueError("protocol payload must be bytes or text")
    if len(raw) > max_bytes:
        raise ProtocolResourceLimitError("protocol payload exceeds limits")
    if not raw or b"\r" in raw:
        raise ValueError("invalid one-line protocol payload")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not raw or b"\n" in raw or raw != raw.strip():
        raise ValueError("invalid one-line protocol payload")
    return parse_bounded_json(raw, max_bytes=max_bytes)


def _completed_stdout(completed: object) -> object:
    return getattr(completed, "stdout", b"")


def _completed_stderr(completed: object) -> object:
    return getattr(completed, "stderr", b"")


def _completed_returncode(completed: object) -> int:
    try:
        return int(getattr(completed, "returncode", 1))
    except (TypeError, ValueError):
        return 1


def _completed_overflow(completed: object) -> Optional[str]:
    value = getattr(completed, "overflow", None)
    return value if value in ("input", "stdout", "stderr") else None


def _validated_session_string(
    source: Mapping[str, Any],
    key: str,
    *,
    required: bool = False,
) -> str:
    value = source[key]
    if not isinstance(value, str) or (required and not value):
        raise ValueError("invalid remote row text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("invalid remote row text") from error
    if len(encoded) > _SESSION_STRING_LIMITS[key]:
        raise ProtocolResourceLimitError("remote row text exceeds limit")
    return value


def _validate_updated_at(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid remote row timestamp")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("invalid remote row timestamp")
    if value < MIN_SESSION_TIMESTAMP or value > MAX_SESSION_TIMESTAMP:
        raise ProtocolResourceLimitError("remote row timestamp exceeds datetime range")


def _encoded_row(source: Mapping[str, Any]) -> bytes:
    return json.dumps(
        source,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _validate_protocol_rows(
    value: object,
    host: str,
) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise ValueError("invalid remote rows")
    if len(value) > MAX_ROWS:
        raise ProtocolResourceLimitError("remote rows exceed limit")
    rows: List[Mapping[str, Any]] = []
    for source in value:
        if not isinstance(source, dict) or set(source) != _SESSION_KEYS:
            raise ValueError("invalid remote row")
        _validate_protocol_json(source, max_nodes=MAX_JSON_ITEMS)
        _validated_session_string(source, "agent", required=True)
        _validated_session_string(source, "session_id", required=True)
        _validated_session_string(source, "project")
        _validated_session_string(source, "transcript")
        _validated_session_string(source, "title")
        _validate_updated_at(source["updated_at"])
        status_value = source["status"]
        if not isinstance(status_value, str) or status_value not in _STATUS_VALUES:
            raise ValueError("invalid remote row status")
        if not isinstance(source["extra"], dict):
            raise ValueError("invalid remote row extra")
        _validate_protocol_json(
            source["extra"],
            max_nodes=MAX_JSON_EXTRA_ITEMS,
        )
        parent_id = source["parent_id"]
        if parent_id is not None:
            _validated_session_string(source, "parent_id")
        encoded = _encoded_row(source)
        if len(encoded) > MAX_ROW_BYTES:
            raise ProtocolResourceLimitError("remote row exceeds limit")
        row = dict(source)
        row["host"] = host
        rows.append(row)
    return tuple(rows)


class _RemoteResponseFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _parse_execution_response(
    payload: object,
    host: str,
) -> Tuple[Mapping[str, Any], ...]:
    value = _parse_one_line_json(payload, max_bytes=MAX_PROTOCOL_BYTES)
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise ValueError("invalid remote response")
    if value["ok"] is False:
        if set(value) != {"ok", "code"}:
            raise ValueError("invalid remote response")
        code = value["code"]
        if code not in ("timeout", "resource_limit", "protocol", "remote"):
            raise ValueError("invalid remote response")
        raise _RemoteResponseFailure(code)
    if set(value) != {"ok", "rows"}:
        raise ValueError("invalid remote response")
    return _validate_protocol_rows(value["rows"], host)
