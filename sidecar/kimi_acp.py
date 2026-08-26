"""Strict, bounded one-shot Kimi ACP transport.

This module intentionally stops at the ACP/process boundary.  A successful
``session/prompt`` response is not durable delivery proof; callers must bind
and inspect Kimi's native wire before reporting ``delivery="delivered"``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sidecar import __version__
from sidecar.json_limits import JSONLimitError, JSONLimits, JSONSyntaxError, parse_json
from sidecar.process_runner import (
    AcpProcessIdentity,
    BoundedDuplexLineProcess,
    BoundedDuplexLineProcessCancelledError,
    BoundedDuplexLineProcessEOFError,
    BoundedDuplexLineProcessError,
    BoundedDuplexLineProcessOverflowError,
    BoundedDuplexLineProcessTimeoutError,
    DescendantContainmentUnsupportedError,
    DuplexProcessResult,
    DuplexWriteBoundary,
    MAX_TIMEOUT_SECONDS,
)


ACP_LINE_BYTES = 256 * 1024
ACP_STDOUT_BYTES = 4 * 1024 * 1024
ACP_STDERR_BYTES = 64 * 1024
ACP_ASSISTANT_BYTES = 4 * 1024 * 1024
ACP_JSON_DEPTH = 32
ACP_JSON_ITEMS = 8192
ACP_JSON_NODES = 16384
ACP_JSON_INTEGER_BITS = 256
ACP_CANCEL_GRACE_SECONDS = 0.5
ACP_NORMAL_EXIT_GRACE_SECONDS = 0.5
ACP_CLEANUP_SECONDS = 2.0

_JSON_LIMITS = JSONLimits(
    max_bytes=ACP_LINE_BYTES,
    max_depth=ACP_JSON_DEPTH,
    max_items=ACP_JSON_ITEMS,
    max_nodes=ACP_JSON_NODES,
    max_string_bytes=ACP_LINE_BYTES,
    max_integer_bits=ACP_JSON_INTEGER_BITS,
)
_STOP_REASONS = frozenset(
    ("end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled")
)
_SESSION_UPDATE_KINDS = frozenset(
    (
        "user_message_chunk",
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
        "plan_update",
        "plan_removed",
        "available_commands_update",
        "current_mode_update",
        "config_option_update",
        "session_info_update",
        "usage_update",
    )
)
_CALLBACK_ERROR_CODES = frozenset(
    (
        "invalid_session",
        "session_busy",
        "session_changed",
        "session_unavailable",
    )
)
_KIMI_CHILD_ENV_PREFIXES = ("DYLD_", "NODE_")
_TOOL_KINDS = frozenset(
    (
        "read",
        "edit",
        "delete",
        "move",
        "search",
        "execute",
        "think",
        "fetch",
        "switch_mode",
        "other",
    )
)
_TOOL_STATUSES = frozenset(("pending", "in_progress", "completed", "failed"))
_PERMISSION_KINDS = frozenset(
    ("allow_once", "allow_always", "reject_once", "reject_always")
)
_PLAN_PRIORITIES = frozenset(("high", "medium", "low"))
_PLAN_STATUSES = frozenset(("pending", "in_progress", "completed"))


class PromptWriteBoundary(str, Enum):
    """How much of the prompt NDJSON frame crossed child stdin."""

    NONE = "none"
    PARTIAL = "partial"
    COMPLETE = "complete"


class AcpPhase(str, Enum):
    """Furthest successfully completed ACP lifecycle phase."""

    NEW = "new"
    INITIALIZED = "initialized"
    LISTED = "listed"
    RESUMED = "resumed"
    MODE_SET = "mode_set"
    PROMPT_WRITTEN = "prompt_written"
    PROMPT_SETTLED = "prompt_settled"
    CLOSED = "closed"


@dataclass(frozen=True, repr=False)
class KimiAcpRequest:
    """One private Kimi ACP attempt.

    ``message`` is deliberately omitted from repr.  The driver constructs only
    ``(executable, "acp")`` and places the decoded message solely in the
    ``session/prompt`` text block.
    """

    executable: str
    cwd: str
    session_id: str
    request_id: str
    message: bytes = field(repr=False)
    deadline: float

    def __repr__(self) -> str:
        return (
            "KimiAcpRequest(executable={!r}, cwd={!r}, session_id={!r}, "
            "request_id={!r}, deadline={!r})"
        ).format(
            self.executable,
            self.cwd,
            self.session_id,
            self.request_id,
            self.deadline,
        )


@dataclass(frozen=True)
class KimiAcpResult:
    """Redacted ACP/process result; durable wire proof is intentionally absent."""

    phase: AcpPhase
    prompt_write: PromptWriteBoundary
    stop_reason: Optional[str]
    response_text: str = field(repr=False)
    response_digest: str
    returncode: Optional[int]
    clean_exit: bool
    cleanup_complete: bool
    error_code: Optional[str]
    own_turn_started: bool
    definitive_busy_rejection: bool
    stdout_bytes: int
    stderr_bytes: int


class _DriverFailure(Exception):
    def __init__(self, code: str, *, busy: bool = False) -> None:
        super().__init__("Kimi ACP attempt failed")
        self.code = code
        self.busy = busy


def build_kimi_child_env() -> Dict[str, str]:
    """Return a fresh host environment without Node or dyld injection hooks."""

    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(_KIMI_CHILD_ENV_PREFIXES)
    }


def _prompt_boundary(value: DuplexWriteBoundary) -> PromptWriteBoundary:
    if value is DuplexWriteBoundary.COMPLETE:
        return PromptWriteBoundary.COMPLETE
    if value is DuplexWriteBoundary.PARTIAL:
        return PromptWriteBoundary.PARTIAL
    return PromptWriteBoundary.NONE


def _validate_request(request: object, monotonic: Callable[[], float]) -> KimiAcpRequest:
    if not isinstance(request, KimiAcpRequest):
        raise TypeError("request must be KimiAcpRequest")
    for name in ("executable", "cwd", "session_id", "request_id"):
        value = getattr(request, name)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("Kimi ACP request fields are invalid")
    if not os.path.isabs(request.executable) or not os.path.isabs(request.cwd):
        raise ValueError("Kimi ACP paths must be absolute")
    if os.path.realpath(request.cwd) != request.cwd:
        raise ValueError("Kimi ACP cwd must be canonical")
    if not isinstance(request.message, bytes):
        raise TypeError("Kimi ACP message must be bytes")
    try:
        message = request.message.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise ValueError("Kimi ACP message must be UTF-8") from error
    if not message.strip() or "\x00" in message:
        raise ValueError("Kimi ACP message is invalid")
    if type(request.deadline) not in (int, float):
        raise ValueError("Kimi ACP deadline must be finite")
    try:
        deadline = float(request.deadline)
    except OverflowError as error:
        raise ValueError("Kimi ACP deadline must be finite") from error
    now = monotonic()
    if not math.isfinite(deadline) or deadline <= now:
        raise ValueError("Kimi ACP deadline must be in the future")
    if deadline - now > MAX_TIMEOUT_SECONDS:
        raise ValueError("Kimi ACP deadline exceeds the maximum operation interval")
    return replace(request, deadline=deadline)


def _encode_frame(value: Mapping[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as error:
        raise _DriverFailure("protocol_error") from error
    if not payload or len(payload) > ACP_LINE_BYTES or b"\n" in payload:
        raise _DriverFailure("protocol_error")
    return payload


def _prepare_prompt_frame(request: KimiAcpRequest) -> bytes:
    message = request.message.decode("utf-8", "strict")
    try:
        frame = _encode_frame(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "session/prompt",
                "params": {
                    "sessionId": request.session_id,
                    "messageId": request.request_id,
                    "prompt": [{"type": "text", "text": message}],
                },
            }
        )
        parsed = parse_json(frame, _JSON_LIMITS)
    except (_DriverFailure, JSONLimitError, JSONSyntaxError) as error:
        raise ValueError("Kimi ACP prompt frame exceeds protocol bounds") from error
    if not isinstance(parsed, Mapping):
        raise ValueError("Kimi ACP prompt frame is invalid")
    return frame


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _DriverFailure("protocol_error")
    return value


def _rpc_id(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise _DriverFailure("protocol_error")
    return value


def _valid_meta_only(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    if set(value) - {"_meta"}:
        return False
    return "_meta" not in value or value["_meta"] is None or isinstance(
        value["_meta"], Mapping
    )


def _error_is_busy(error: Any) -> bool:
    if not isinstance(error, Mapping):
        return False
    data = error.get("data")
    if not isinstance(data, Mapping):
        return False
    if data.get("code") == "turn.agent_busy":
        return True
    nested = data.get("error")
    return isinstance(nested, Mapping) and nested.get("code") == "turn.agent_busy"


def _object(
    value: Any,
    required: Set[str],
    optional: Set[str],
) -> Mapping[str, Any]:
    item = _mapping(value)
    keys = set(item)
    if not required.issubset(keys) or keys - required - optional:
        raise _DriverFailure("protocol_error")
    if "_meta" in item and item["_meta"] is not None and not isinstance(
        item["_meta"], Mapping
    ):
        raise _DriverFailure("protocol_error")
    return item


def _string(value: Any, *, nullable: bool = False, nonempty: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or (nonempty and not value):
        raise _DriverFailure("protocol_error")


def _number(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _DriverFailure("protocol_error")
    if not math.isfinite(float(value)):
        raise _DriverFailure("protocol_error")


def _optional_string(item: Mapping[str, Any], key: str) -> None:
    if key in item:
        _string(item[key], nullable=True)


def _validate_annotations(value: Any) -> None:
    item = _object(value, set(), {"_meta", "audience", "lastModified", "priority"})
    if "audience" in item and item["audience"] is not None:
        audience = item["audience"]
        if not isinstance(audience, list) or any(
            role not in ("assistant", "user") for role in audience
        ):
            raise _DriverFailure("protocol_error")
    _optional_string(item, "lastModified")
    if "priority" in item and item["priority"] is not None:
        _number(item["priority"])


def _validate_content_block(value: Any) -> None:
    item = _mapping(value)
    kind = item.get("type")
    common = {"_meta", "annotations", "type"}
    if kind == "text":
        item = _object(item, {"type", "text"}, common)
        _string(item["text"])
    elif kind in ("image", "audio"):
        optional = common | ({"uri"} if kind == "image" else set())
        item = _object(item, {"type", "data", "mimeType"}, optional)
        _string(item["data"])
        _string(item["mimeType"])
        _optional_string(item, "uri")
    elif kind == "resource_link":
        item = _object(
            item,
            {"type", "name", "uri"},
            common | {"description", "mimeType", "size", "title"},
        )
        _string(item["name"])
        _string(item["uri"])
        for key in ("description", "mimeType", "title"):
            _optional_string(item, key)
        if "size" in item and item["size"] is not None:
            _number(item["size"])
    elif kind == "resource":
        item = _object(item, {"type", "resource"}, common)
        resource = _mapping(item["resource"])
        has_text = "text" in resource
        has_blob = "blob" in resource
        if has_text == has_blob:
            raise _DriverFailure("protocol_error")
        required = {"uri", "text"} if has_text else {"uri", "blob"}
        resource = _object(resource, required, {"_meta", "mimeType"})
        for key in required:
            _string(resource[key])
        _optional_string(resource, "mimeType")
    else:
        raise _DriverFailure("protocol_error")
    if "annotations" in item and item["annotations"] is not None:
        _validate_annotations(item["annotations"])


def _validate_tool_location(value: Any) -> None:
    item = _object(value, {"path"}, {"_meta", "line"})
    _string(item["path"])
    if "line" in item and item["line"] is not None:
        line = item["line"]
        if (
            isinstance(line, bool)
            or not isinstance(line, int)
            or line < 0
            or line > 4294967295
        ):
            raise _DriverFailure("protocol_error")


def _validate_tool_content(value: Any) -> None:
    item = _mapping(value)
    kind = item.get("type")
    if kind == "content":
        item = _object(item, {"type", "content"}, {"_meta"})
        _validate_content_block(item["content"])
    elif kind == "diff":
        item = _object(
            item,
            {"type", "newText", "path"},
            {"_meta", "oldText"},
        )
        _string(item["newText"])
        _string(item["path"])
        _optional_string(item, "oldText")
    elif kind == "terminal":
        item = _object(item, {"type", "terminalId"}, {"_meta"})
        _string(item["terminalId"])
    else:
        raise _DriverFailure("protocol_error")


def _validate_tool_call(
    value: Any,
    *,
    update: bool,
    discriminator: bool = False,
) -> None:
    required = {"toolCallId"} if update else {"toolCallId", "title"}
    optional = {
        "_meta",
        "content",
        "kind",
        "locations",
        "rawInput",
        "rawOutput",
        "status",
    }
    if update:
        optional.add("title")
    if discriminator:
        optional.add("sessionUpdate")
    item = _object(value, required, optional)
    _string(item["toolCallId"], nonempty=True)
    if "title" in item:
        _string(item["title"], nullable=update)
    if "kind" in item:
        kind = item["kind"]
        if kind is None and update:
            pass
        elif not isinstance(kind, str) or kind not in _TOOL_KINDS:
            raise _DriverFailure("protocol_error")
    if "status" in item:
        status = item["status"]
        if status is None and update:
            pass
        elif not isinstance(status, str) or status not in _TOOL_STATUSES:
            raise _DriverFailure("protocol_error")
    for key, validator in (
        ("content", _validate_tool_content),
        ("locations", _validate_tool_location),
    ):
        if key not in item or (update and item[key] is None):
            continue
        values = item[key]
        if not isinstance(values, list):
            raise _DriverFailure("protocol_error")
        for nested in values:
            validator(nested)


def _validate_plan_entry(value: Any) -> None:
    item = _object(value, {"content", "priority", "status"}, {"_meta"})
    _string(item["content"])
    if item["priority"] not in _PLAN_PRIORITIES or item["status"] not in _PLAN_STATUSES:
        raise _DriverFailure("protocol_error")


def _validate_plan_entries(value: Any) -> None:
    if not isinstance(value, list):
        raise _DriverFailure("protocol_error")
    for entry in value:
        _validate_plan_entry(entry)


def _validate_plan_update(value: Any) -> None:
    item = _object(value, {"plan"}, {"_meta", "sessionUpdate"})
    plan = _mapping(item["plan"])
    kind = plan.get("type")
    if kind == "items":
        plan = _object(plan, {"type", "id", "entries"}, {"_meta"})
        _validate_plan_entries(plan["entries"])
    elif kind == "file":
        plan = _object(plan, {"type", "id", "uri"}, {"_meta"})
        _string(plan["uri"])
    elif kind == "markdown":
        plan = _object(plan, {"type", "id", "content"}, {"_meta"})
        _string(plan["content"])
    else:
        raise _DriverFailure("protocol_error")
    _string(plan["id"])


def _validate_available_command(value: Any) -> None:
    item = _object(value, {"description", "name"}, {"_meta", "input"})
    _string(item["description"])
    _string(item["name"])
    if "input" in item and item["input"] is not None:
        nested = _object(item["input"], {"hint"}, {"_meta"})
        _string(nested["hint"])


def _validate_config_select_option(value: Any) -> None:
    item = _object(value, {"name", "value"}, {"_meta", "description"})
    _string(item["name"])
    _string(item["value"])
    _optional_string(item, "description")


def _validate_config_option(value: Any) -> None:
    item = _mapping(value)
    kind = item.get("type")
    required = {"type", "currentValue", "id", "name"}
    optional = {"_meta", "category", "description"}
    if kind == "select":
        required.add("options")
    elif kind != "boolean":
        raise _DriverFailure("protocol_error")
    item = _object(item, required, optional)
    _string(item["id"])
    _string(item["name"])
    _optional_string(item, "category")
    _optional_string(item, "description")
    if kind == "boolean":
        if type(item["currentValue"]) is not bool:
            raise _DriverFailure("protocol_error")
        return
    _string(item["currentValue"])
    options = item["options"]
    if not isinstance(options, list):
        raise _DriverFailure("protocol_error")
    grouped = bool(options) and isinstance(options[0], Mapping) and "group" in options[0]
    for option in options:
        if grouped:
            group = _object(option, {"group", "name", "options"}, {"_meta"})
            _string(group["group"])
            _string(group["name"])
            if not isinstance(group["options"], list):
                raise _DriverFailure("protocol_error")
            for nested in group["options"]:
                _validate_config_select_option(nested)
        else:
            _validate_config_select_option(option)


def _validate_config_options(value: Any) -> None:
    if not isinstance(value, list):
        raise _DriverFailure("protocol_error")
    for option in value:
        _validate_config_option(option)


def _validate_mode_state(value: Any) -> None:
    item = _object(value, {"availableModes", "currentModeId"}, {"_meta"})
    _string(item["currentModeId"])
    modes = item["availableModes"]
    if not isinstance(modes, list):
        raise _DriverFailure("protocol_error")
    for mode in modes:
        nested = _object(mode, {"id", "name"}, {"_meta", "description"})
        _string(nested["id"])
        _string(nested["name"])
        _optional_string(nested, "description")


def _validate_model_state(value: Any) -> None:
    item = _object(value, {"availableModels", "currentModelId"}, {"_meta"})
    _string(item["currentModelId"])
    models = item["availableModels"]
    if not isinstance(models, list):
        raise _DriverFailure("protocol_error")
    for model in models:
        nested = _object(model, {"modelId", "name"}, {"_meta", "description"})
        _string(nested["modelId"])
        _string(nested["name"])
        _optional_string(nested, "description")


def _validate_usage(value: Any) -> None:
    item = _object(
        value,
        {"inputTokens", "outputTokens", "totalTokens"},
        {"cachedReadTokens", "cachedWriteTokens", "thoughtTokens"},
    )
    for key in item:
        if item[key] is not None:
            _number(item[key])


def _validate_update(update: Any) -> None:
    item = _mapping(update)
    kind = item.get("sessionUpdate")
    if kind not in _SESSION_UPDATE_KINDS:
        raise _DriverFailure("protocol_error")
    if kind in ("user_message_chunk", "agent_message_chunk", "agent_thought_chunk"):
        item = _object(item, {"sessionUpdate", "content"}, {"_meta", "messageId"})
        _validate_content_block(item["content"])
        _optional_string(item, "messageId")
    elif kind in ("tool_call", "tool_call_update"):
        _validate_tool_call(
            item,
            update=kind == "tool_call_update",
            discriminator=True,
        )
    elif kind == "plan":
        item = _object(item, {"sessionUpdate", "entries"}, {"_meta"})
        _validate_plan_entries(item["entries"])
    elif kind == "plan_update":
        _validate_plan_update(item)
    elif kind == "plan_removed":
        item = _object(item, {"sessionUpdate", "id"}, {"_meta"})
        _string(item["id"])
    elif kind == "available_commands_update":
        item = _object(
            item,
            {"sessionUpdate", "availableCommands"},
            {"_meta"},
        )
        commands = item["availableCommands"]
        if not isinstance(commands, list):
            raise _DriverFailure("protocol_error")
        for command in commands:
            _validate_available_command(command)
    elif kind == "current_mode_update":
        item = _object(item, {"sessionUpdate", "currentModeId"}, {"_meta"})
        _string(item["currentModeId"])
    elif kind == "config_option_update":
        item = _object(item, {"sessionUpdate", "configOptions"}, {"_meta"})
        _validate_config_options(item["configOptions"])
    elif kind == "session_info_update":
        item = _object(
            item,
            {"sessionUpdate"},
            {"_meta", "title", "updatedAt"},
        )
        _optional_string(item, "title")
        _optional_string(item, "updatedAt")
    elif kind == "usage_update":
        item = _object(item, {"sessionUpdate", "size", "used"}, {"_meta", "cost"})
        _number(item["size"])
        _number(item["used"])
        if "cost" in item and item["cost"] is not None:
            cost = _object(item["cost"], {"amount", "currency"}, set())
            _number(cost["amount"])
            _string(cost["currency"])


class _Protocol:
    def __init__(
        self,
        process: BoundedDuplexLineProcess,
        request: KimiAcpRequest,
    ) -> None:
        self.process = process
        self.request = request
        self.pending: Set[Any] = set()
        self.completed: Set[Any] = set()
        self.reverse_ids: Set[Any] = set()
        self.response_parts: List[str] = []
        self.response_bytes = 0
        self.stop_reason: Optional[str] = None
        self.own_turn_started = False

    @property
    def response_text(self) -> str:
        return "".join(self.response_parts)

    def write(self, value: Mapping[str, Any], deadline: float) -> DuplexWriteBoundary:
        try:
            result = self.process.write_line(_encode_frame(value), deadline=deadline)
        except RuntimeError as error:
            raise _DriverFailure("protocol_error") from error
        return result.boundary

    def request_rpc(
        self,
        request_id: int,
        method: str,
        params: Mapping[str, Any],
    ) -> Any:
        if request_id in self.pending or request_id in self.completed:
            raise _DriverFailure("protocol_error")
        self.pending.add(request_id)
        boundary = self.write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
            self.request.deadline,
        )
        if boundary is not DuplexWriteBoundary.COMPLETE:
            self.pending.discard(request_id)
            if boundary is DuplexWriteBoundary.NONE:
                raise _DriverFailure("timeout")
            raise _DriverFailure("protocol_error")
        while True:
            envelope = self.read_envelope()
            response = self.consume(envelope, expected_id=request_id)
            if response is not None:
                return response

    def read_envelope(self) -> Mapping[str, Any]:
        line = self.process.read_line(deadline=self.request.deadline)
        if line is None or not line:
            raise _DriverFailure("protocol_error")
        try:
            value = parse_json(line, _JSON_LIMITS)
        except (JSONLimitError, JSONSyntaxError) as error:
            raise _DriverFailure("protocol_error") from error
        return _mapping(value)

    def consume(
        self,
        envelope: Mapping[str, Any],
        *,
        expected_id: Optional[int],
    ) -> Optional[Mapping[str, Any]]:
        if envelope.get("jsonrpc") != "2.0":
            raise _DriverFailure("protocol_error")
        has_method = "method" in envelope
        has_id = "id" in envelope
        if has_method:
            if set(envelope) - {"jsonrpc", "id", "method", "params"}:
                raise _DriverFailure("protocol_error")
            if "params" not in envelope:
                raise _DriverFailure("protocol_error")
            method = envelope.get("method")
            if not isinstance(method, str) or not method:
                raise _DriverFailure("protocol_error")
            if has_id:
                self._reverse_request(envelope, method)
            else:
                self._notification(envelope, method)
            return None
        if (
            not has_id
            or "params" in envelope
            or set(envelope) - {"jsonrpc", "id", "result", "error"}
        ):
            raise _DriverFailure("protocol_error")
        response_id = _rpc_id(envelope.get("id"))
        if response_id in self.completed or response_id not in self.pending:
            raise _DriverFailure("protocol_error")
        if expected_id is None or response_id != expected_id:
            raise _DriverFailure("protocol_error")
        has_result = "result" in envelope
        has_error = "error" in envelope
        if has_result == has_error:
            raise _DriverFailure("protocol_error")
        self.pending.remove(response_id)
        self.completed.add(response_id)
        if has_error:
            error = envelope.get("error")
            self._validate_error(error)
            if response_id == 5 and _error_is_busy(error):
                raise _DriverFailure("session_busy", busy=True)
            raise _DriverFailure("protocol_error")
        return _mapping(envelope)

    @staticmethod
    def _validate_error(value: Any) -> None:
        error = _mapping(value)
        code = error.get("code")
        if (
            type(code) is not int
            or code < -2147483648
            or code > 2147483647
        ):
            raise _DriverFailure("protocol_error")
        if not isinstance(error.get("message"), str):
            raise _DriverFailure("protocol_error")
        if set(error) - {"code", "message", "data"}:
            raise _DriverFailure("protocol_error")

    def _notification(self, envelope: Mapping[str, Any], method: str) -> None:
        if method != "session/update":
            raise _DriverFailure("protocol_error")
        params = _object(
            envelope.get("params"),
            {"sessionId", "update"},
            {"_meta"},
        )
        if params.get("sessionId") != self.request.session_id:
            raise _DriverFailure("protocol_error")
        update = _mapping(params.get("update"))
        kind = update.get("sessionUpdate")
        _validate_update(update)
        if kind != "agent_message_chunk":
            return
        content = _mapping(update["content"])
        text = content.get("text")
        if content.get("type") != "text":
            return
        assert isinstance(text, str)
        try:
            encoded = text.encode("utf-8", "strict")
        except UnicodeEncodeError as error:
            raise _DriverFailure("protocol_error") from error
        if self.response_bytes + len(encoded) > ACP_ASSISTANT_BYTES:
            raise _DriverFailure("output_overflow")
        self.response_parts.append(text)
        self.response_bytes += len(encoded)
        self.own_turn_started = True

    def _reverse_request(
        self,
        envelope: Mapping[str, Any],
        method: str,
    ) -> None:
        reverse_id = _rpc_id(envelope.get("id"))
        if reverse_id in self.reverse_ids:
            raise _DriverFailure("protocol_error")
        self.reverse_ids.add(reverse_id)
        if method != "session/request_permission":
            self.write(
                {
                    "jsonrpc": "2.0",
                    "id": reverse_id,
                    "error": {
                        "code": -32601,
                        "message": "Method not found",
                    },
                },
                self.request.deadline,
            )
            raise _DriverFailure("protocol_error")
        self._validate_permission_params(envelope.get("params"))
        boundary = self.write(
            {
                "jsonrpc": "2.0",
                "id": reverse_id,
                "result": {"outcome": {"outcome": "cancelled"}},
            },
            self.request.deadline,
        )
        if boundary is not DuplexWriteBoundary.COMPLETE:
            raise _DriverFailure("protocol_error")

    def _validate_permission_params(self, value: Any) -> None:
        params = _object(
            value,
            {"sessionId", "toolCall", "options"},
            {"_meta"},
        )
        if params.get("sessionId") != self.request.session_id:
            raise _DriverFailure("protocol_error")
        _validate_tool_call(params.get("toolCall"), update=True)
        options = params.get("options")
        if not isinstance(options, list) or not options:
            raise _DriverFailure("protocol_error")
        option_ids = set()
        for option in options:
            item = _object(
                option,
                {"optionId", "name", "kind"},
                {"_meta"},
            )
            _string(item["optionId"], nonempty=True)
            _string(item["name"], nonempty=True)
            if item["kind"] not in _PERMISSION_KINDS:
                raise _DriverFailure("protocol_error")
            if item["optionId"] in option_ids:
                raise _DriverFailure("protocol_error")
            option_ids.add(item["optionId"])


def _validate_bool_capabilities(value: Any, fields: Set[str]) -> None:
    item = _object(value, set(), fields | {"_meta"})
    for key in fields:
        if key in item and type(item[key]) is not bool:
            raise _DriverFailure("protocol_error")


def _validate_nes_capabilities(value: Any) -> None:
    item = _object(value, set(), {"_meta", "context", "events"})
    if "context" in item and item["context"] is not None:
        context = _object(
            item["context"],
            set(),
            {
                "_meta",
                "diagnostics",
                "editHistory",
                "openFiles",
                "recentFiles",
                "relatedSnippets",
                "userActions",
            },
        )
        for key in (
            "diagnostics",
            "openFiles",
            "relatedSnippets",
        ):
            if key in context and context[key] is not None:
                if not _valid_meta_only(context[key]):
                    raise _DriverFailure("protocol_error")
        for key in ("editHistory", "recentFiles", "userActions"):
            if key not in context or context[key] is None:
                continue
            capability = _object(context[key], set(), {"_meta", "maxCount"})
            if "maxCount" in capability and capability["maxCount"] is not None:
                count = capability["maxCount"]
                if (
                    isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 0
                    or count > 4294967295
                ):
                    raise _DriverFailure("protocol_error")
    if "events" in item and item["events"] is not None:
        events = _object(item["events"], set(), {"_meta", "document"})
        if "document" in events and events["document"] is not None:
            document = _object(
                events["document"],
                set(),
                {
                    "_meta",
                    "didChange",
                    "didClose",
                    "didFocus",
                    "didOpen",
                    "didSave",
                },
            )
            for key in ("didClose", "didFocus", "didOpen", "didSave"):
                if key in document and document[key] is not None:
                    if not _valid_meta_only(document[key]):
                        raise _DriverFailure("protocol_error")
            if "didChange" in document and document["didChange"] is not None:
                changed = _object(
                    document["didChange"],
                    {"syncKind"},
                    {"_meta"},
                )
                if changed["syncKind"] not in ("full", "incremental"):
                    raise _DriverFailure("protocol_error")


def _validate_auth_method(value: Any) -> None:
    item = _mapping(value)
    kind = item.get("type")
    if kind == "env_var":
        item = _object(
            item,
            {"type", "id", "name", "vars"},
            {"_meta", "description", "link"},
        )
        variables = item["vars"]
        if not isinstance(variables, list):
            raise _DriverFailure("protocol_error")
        for variable in variables:
            nested = _object(
                variable,
                {"name"},
                {"_meta", "label", "optional", "secret"},
            )
            _string(nested["name"])
            _optional_string(nested, "label")
            for key in ("optional", "secret"):
                if key in nested and type(nested[key]) is not bool:
                    raise _DriverFailure("protocol_error")
        _optional_string(item, "link")
    elif kind == "terminal":
        item = _object(
            item,
            {"type", "id", "name"},
            {"_meta", "args", "description", "env"},
        )
        if "args" in item:
            if not isinstance(item["args"], list) or any(
                not isinstance(argument, str) for argument in item["args"]
            ):
                raise _DriverFailure("protocol_error")
        if "env" in item:
            environment = item["env"]
            if not isinstance(environment, Mapping) or any(
                not isinstance(key, str) or not isinstance(entry, str)
                for key, entry in environment.items()
            ):
                raise _DriverFailure("protocol_error")
    elif kind is None:
        item = _object(
            item,
            {"id", "name"},
            {"_meta", "description"},
        )
    else:
        raise _DriverFailure("protocol_error")
    _string(item["id"])
    _string(item["name"])
    _optional_string(item, "description")


def _validate_agent_capabilities(value: Any) -> None:
    capabilities = _object(
        value,
        {"loadSession", "sessionCapabilities"},
        {
            "_meta",
            "auth",
            "mcpCapabilities",
            "nes",
            "positionEncoding",
            "promptCapabilities",
            "providers",
        },
    )
    if capabilities["loadSession"] is not True:
        raise _DriverFailure("protocol_error")
    if "auth" in capabilities:
        auth = _object(capabilities["auth"], set(), {"_meta", "logout"})
        if "logout" in auth and auth["logout"] is not None:
            if not _valid_meta_only(auth["logout"]):
                raise _DriverFailure("protocol_error")
    if "mcpCapabilities" in capabilities:
        _validate_bool_capabilities(
            capabilities["mcpCapabilities"],
            {"acp", "http", "sse"},
        )
    if "promptCapabilities" in capabilities:
        _validate_bool_capabilities(
            capabilities["promptCapabilities"],
            {"audio", "embeddedContext", "image"},
        )
    if "nes" in capabilities and capabilities["nes"] is not None:
        _validate_nes_capabilities(capabilities["nes"])
    if "positionEncoding" in capabilities and capabilities["positionEncoding"] not in (
        None,
        "utf-16",
        "utf-32",
        "utf-8",
    ):
        raise _DriverFailure("protocol_error")
    if "providers" in capabilities and capabilities["providers"] is not None:
        if not _valid_meta_only(capabilities["providers"]):
            raise _DriverFailure("protocol_error")
    session_capabilities = _object(
        capabilities["sessionCapabilities"],
        {"list", "resume"},
        {
            "_meta",
            "additionalDirectories",
            "close",
            "delete",
            "fork",
        },
    )
    for key in (
        "additionalDirectories",
        "close",
        "delete",
        "fork",
        "list",
        "resume",
    ):
        if key in session_capabilities and session_capabilities[key] is not None:
            if not _valid_meta_only(session_capabilities[key]):
                raise _DriverFailure("protocol_error")
    if session_capabilities["list"] is None or session_capabilities["resume"] is None:
        raise _DriverFailure("protocol_error")


def _initialize_result(envelope: Mapping[str, Any]) -> None:
    result = _object(
        envelope.get("result"),
        {"protocolVersion", "agentCapabilities"},
        {"_meta", "agentInfo", "authMethods"},
    )
    version = result["protocolVersion"]
    if type(version) is not int or version != 1:
        raise _DriverFailure("protocol_error")
    _validate_agent_capabilities(result["agentCapabilities"])
    if "agentInfo" in result and result["agentInfo"] is not None:
        info = _object(
            result["agentInfo"],
            {"name", "version"},
            {"_meta", "title"},
        )
        _string(info["name"])
        _string(info["version"])
        _optional_string(info, "title")
    if "authMethods" in result:
        methods = result["authMethods"]
        if not isinstance(methods, list):
            raise _DriverFailure("protocol_error")
        for method in methods:
            _validate_auth_method(method)


def _list_result(
    envelope: Mapping[str, Any],
    request: KimiAcpRequest,
    validate_session_cwd: Callable[[str], bool],
) -> None:
    result = _object(
        envelope.get("result"),
        {"sessions"},
        {"_meta", "nextCursor"},
    )
    _optional_string(result, "nextCursor")
    sessions = result.get("sessions")
    if not isinstance(sessions, list):
        raise _DriverFailure("protocol_error")
    matches = []
    for row in sessions:
        item = _object(
            row,
            {"sessionId", "cwd"},
            {
                "_meta",
                "additionalDirectories",
                "title",
                "updatedAt",
            },
        )
        _string(item["sessionId"])
        _string(item["cwd"])
        _optional_string(item, "title")
        _optional_string(item, "updatedAt")
        if "additionalDirectories" in item:
            directories = item["additionalDirectories"]
            if not isinstance(directories, list) or any(
                not isinstance(path, str) for path in directories
            ):
                raise _DriverFailure("protocol_error")
        if item.get("sessionId") == request.session_id:
            matches.append(item)
    if not matches:
        raise _DriverFailure("session_unavailable")
    if len(matches) != 1:
        raise _DriverFailure("invalid_session")
    listed_cwd = matches[0].get("cwd")
    assert isinstance(listed_cwd, str)
    try:
        matches_project = validate_session_cwd(listed_cwd)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise _DriverFailure("invalid_session") from error
    if matches_project is not True:
        raise _DriverFailure("invalid_session")


def _resume_result(envelope: Mapping[str, Any]) -> None:
    result = _object(
        envelope.get("result"),
        set(),
        {"_meta", "configOptions", "models", "modes"},
    )
    if "configOptions" in result and result["configOptions"] is not None:
        _validate_config_options(result["configOptions"])
    if "models" in result and result["models"] is not None:
        _validate_model_state(result["models"])
    if "modes" in result and result["modes"] is not None:
        _validate_mode_state(result["modes"])


def _mode_result(envelope: Mapping[str, Any]) -> None:
    if not _valid_meta_only(envelope.get("result")):
        raise _DriverFailure("protocol_error")


def _prompt_result(protocol: _Protocol, envelope: Mapping[str, Any]) -> None:
    result = _object(
        envelope.get("result"),
        {"stopReason"},
        {"_meta", "usage", "userMessageId"},
    )
    stop_reason = result.get("stopReason")
    if stop_reason not in _STOP_REASONS:
        raise _DriverFailure("protocol_error")
    if "usage" in result and result["usage"] is not None:
        _validate_usage(result["usage"])
    _optional_string(result, "userMessageId")
    protocol.stop_reason = stop_reason
    protocol.own_turn_started = True


def _default_cwd_validator(expected: str) -> Callable[[str], bool]:
    def validate(actual: str) -> bool:
        if (
            not isinstance(actual, str)
            or not actual
            or "\x00" in actual
            or not os.path.isabs(actual)
        ):
            return False
        try:
            return os.path.samefile(expected, actual)
        except (OSError, ValueError):
            return False

    return validate


def _callback_failure_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    return code if code in _CALLBACK_ERROR_CODES else "session_changed"


def _process_snapshot(
    process: Optional[BoundedDuplexLineProcess],
    observed: Optional[DuplexProcessResult],
) -> Tuple[Optional[int], bool, bool, int, int]:
    result = observed
    if result is None and process is not None:
        result = process.result
    if result is None:
        return None, False, False, 0, 0 if process is None else len(process.stderr)
    return (
        result.returncode,
        result.clean_exit,
        result.cleanup_complete,
        result.stdout_bytes_read,
        len(result.stderr),
    )


def _bounded_cleanup(
    process: BoundedDuplexLineProcess,
    prompt_write: PromptWriteBoundary,
    session_id: str,
    monotonic: Callable[[], float],
) -> DuplexProcessResult:
    observed: Optional[DuplexProcessResult] = None
    if prompt_write is not PromptWriteBoundary.NONE:
        cancel_deadline = monotonic() + ACP_CANCEL_GRACE_SECONDS
        try:
            process.write_line(
                _encode_frame(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/cancel",
                        "params": {"sessionId": session_id},
                    }
                ),
                deadline=cancel_deadline,
            )
        except (BoundedDuplexLineProcessError, RuntimeError):
            pass
        try:
            observed = process.wait_clean(deadline=cancel_deadline)
        except BoundedDuplexLineProcessError:
            pass
    process.close_stdin()
    normal_deadline = monotonic() + ACP_NORMAL_EXIT_GRACE_SECONDS
    try:
        observed = process.wait_clean(deadline=normal_deadline)
    except BoundedDuplexLineProcessError:
        pass
    if observed is None or not observed.cleanup_complete:
        try:
            observed = process.terminate_tree(
                deadline=monotonic() + ACP_CLEANUP_SECONDS
            )
        except BoundedDuplexLineProcessError:
            observed = process.result
    if observed is None:
        process.close()
        observed = process.result
    assert observed is not None
    return observed


def run_kimi_acp(
    request: KimiAcpRequest,
    *,
    before_prompt: Callable[[AcpProcessIdentity], None],
    validate_session_cwd: Optional[Callable[[str], bool]] = None,
    confirm_no_own_prompt: Optional[Callable[[], bool]] = None,
    process_factory: Callable[..., BoundedDuplexLineProcess] = (
        BoundedDuplexLineProcess
    ),
    monotonic: Callable[[], float] = time.monotonic,
) -> KimiAcpResult:
    """Run the exact Kimi ACP lifecycle and return a conservative result.

    ``confirm_no_own_prompt`` is consulted only for a structured
    ``turn.agent_busy`` response.  The driver never performs wire proof itself;
    without an explicit positive callback, a fully written prompt remains
    ambiguous and ``definitive_busy_rejection`` is false.
    """

    if not callable(monotonic):
        raise TypeError("monotonic must be callable")
    validated = _validate_request(request, monotonic)
    prompt_frame = _prepare_prompt_frame(validated)
    if not callable(before_prompt):
        raise TypeError("before_prompt must be callable")
    if validate_session_cwd is None:
        cwd_validator = _default_cwd_validator(validated.cwd)
    elif callable(validate_session_cwd):
        cwd_validator = validate_session_cwd
    else:
        raise TypeError("validate_session_cwd must be callable")
    if confirm_no_own_prompt is not None and not callable(confirm_no_own_prompt):
        raise TypeError("confirm_no_own_prompt must be callable")
    if not callable(process_factory):
        raise TypeError("process_factory must be callable")

    process: Optional[BoundedDuplexLineProcess] = None
    protocol: Optional[_Protocol] = None
    phase = AcpPhase.NEW
    prompt_write = PromptWriteBoundary.NONE
    error_code: Optional[str] = None
    busy_response = False
    definitive_busy = False
    observed: Optional[DuplexProcessResult] = None
    prompt_write_snapshot_taken = False
    prompt_write_result_before = None

    try:
        process = process_factory(
            (validated.executable, "acp"),
            line_limit=ACP_LINE_BYTES,
            stdout_limit=ACP_STDOUT_BYTES,
            stderr_limit=ACP_STDERR_BYTES,
            cwd=validated.cwd,
            env=build_kimi_child_env(),
            require_descendant_containment=True,
            monotonic=monotonic,
        )
        protocol = _Protocol(process, validated)
        initialize = protocol.request_rpc(
            1,
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {
                    "name": "agent-sidecar",
                    "version": __version__,
                },
            },
        )
        _initialize_result(initialize)
        phase = AcpPhase.INITIALIZED

        listed = protocol.request_rpc(
            2,
            "session/list",
            {"cwd": validated.cwd},
        )
        _list_result(listed, validated, cwd_validator)
        phase = AcpPhase.LISTED

        resumed = protocol.request_rpc(
            3,
            "session/resume",
            {
                "sessionId": validated.session_id,
                "cwd": validated.cwd,
                "mcpServers": [],
            },
        )
        _resume_result(resumed)
        phase = AcpPhase.RESUMED

        mode = protocol.request_rpc(
            4,
            "session/set_mode",
            {
                "sessionId": validated.session_id,
                "modeId": "default",
            },
        )
        _mode_result(mode)
        phase = AcpPhase.MODE_SET

        try:
            before_prompt(process.identity)
        except BaseException as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise _DriverFailure(_callback_failure_code(error)) from error

        protocol.pending.add(5)
        prompt_write_result_before = process.write_result
        prompt_write_snapshot_taken = True
        try:
            write_result = process.write_line(
                prompt_frame,
                deadline=validated.deadline,
            )
        except BoundedDuplexLineProcessError as error:
            if error.write_result is not None:
                prompt_write = _prompt_boundary(error.write_result.boundary)
                if prompt_write is PromptWriteBoundary.COMPLETE:
                    phase = AcpPhase.PROMPT_WRITTEN
            raise
        prompt_write = _prompt_boundary(write_result.boundary)
        if prompt_write is PromptWriteBoundary.NONE:
            protocol.pending.discard(5)
            raise _DriverFailure("timeout")
        if prompt_write is PromptWriteBoundary.PARTIAL:
            protocol.pending.discard(5)
            raise _DriverFailure("timeout")
        phase = AcpPhase.PROMPT_WRITTEN

        while True:
            envelope = protocol.read_envelope()
            prompt_response = protocol.consume(envelope, expected_id=5)
            if prompt_response is not None:
                _prompt_result(protocol, prompt_response)
                phase = AcpPhase.PROMPT_SETTLED
                break

        process.close_stdin()
        while True:
            line = process.read_line(deadline=validated.deadline)
            if line is None:
                break
            if not line:
                raise _DriverFailure("protocol_error")
            try:
                trailing = _mapping(parse_json(line, _JSON_LIMITS))
            except (JSONLimitError, JSONSyntaxError) as error:
                raise _DriverFailure("protocol_error") from error
            protocol.consume(trailing, expected_id=None)
        observed = process.wait_clean(deadline=validated.deadline)
        if not observed.cleanup_complete:
            error_code = "cleanup_incomplete"
        elif observed.returncode != 0 or not observed.clean_exit:
            error_code = "native_exit"
    except _DriverFailure as error:
        error_code = error.code
        busy_response = error.busy
        if busy_response and confirm_no_own_prompt is not None:
            try:
                definitive_busy = confirm_no_own_prompt() is True
            except BaseException as proof_error:
                if isinstance(proof_error, (KeyboardInterrupt, SystemExit)):
                    raise
                definitive_busy = False
    except BoundedDuplexLineProcessTimeoutError:
        error_code = "timeout"
    except BoundedDuplexLineProcessOverflowError:
        error_code = "output_overflow"
    except BoundedDuplexLineProcessEOFError:
        error_code = "protocol_error"
    except BoundedDuplexLineProcessCancelledError:
        error_code = "cancelled"
    except DescendantContainmentUnsupportedError:
        error_code = "containment_unsupported"
    except BoundedDuplexLineProcessError:
        error_code = "protocol_error"
    except OSError:
        error_code = "spawn_error"
    finally:
        preserve_prompt_exception = sys.exc_info()[1] is not None
        if process is not None and prompt_write_snapshot_taken:
            try:
                last_write_result = process.write_result
                if (
                    last_write_result is not None
                    and last_write_result is not prompt_write_result_before
                    and phase is AcpPhase.MODE_SET
                ):
                    prompt_write = _prompt_boundary(last_write_result.boundary)
                    if prompt_write is PromptWriteBoundary.COMPLETE:
                        phase = AcpPhase.PROMPT_WRITTEN
            except BaseException:
                if not preserve_prompt_exception:
                    raise
        if process is not None and (
            observed is None or not observed.cleanup_complete
        ):
            try:
                observed = _bounded_cleanup(
                    process,
                    prompt_write,
                    validated.session_id,
                    monotonic,
                )
            except BaseException:
                if not preserve_prompt_exception:
                    raise
        if process is not None:
            try:
                process.close()
            except BaseException:
                if not preserve_prompt_exception:
                    raise

    if observed is not None and not observed.cleanup_complete:
        error_code = "cleanup_incomplete"
    returncode, clean_exit, cleanup_complete, stdout_bytes, stderr_bytes = (
        _process_snapshot(process, observed)
    )
    response_text = "" if protocol is None else protocol.response_text
    response_digest = hashlib.sha256(
        response_text.encode("utf-8", "strict")
    ).hexdigest()
    return KimiAcpResult(
        phase=phase,
        prompt_write=prompt_write,
        stop_reason=None if protocol is None else protocol.stop_reason,
        response_text=response_text,
        response_digest=response_digest,
        returncode=returncode,
        clean_exit=clean_exit,
        cleanup_complete=cleanup_complete,
        error_code=error_code,
        own_turn_started=False if protocol is None else protocol.own_turn_started,
        definitive_busy_rejection=busy_response and definitive_busy,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
    )


__all__ = [
    "ACP_ASSISTANT_BYTES",
    "ACP_JSON_DEPTH",
    "ACP_JSON_ITEMS",
    "ACP_LINE_BYTES",
    "ACP_STDERR_BYTES",
    "ACP_STDOUT_BYTES",
    "AcpPhase",
    "KimiAcpRequest",
    "KimiAcpResult",
    "PromptWriteBoundary",
    "build_kimi_child_env",
    "run_kimi_acp",
]
