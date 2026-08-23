"""Fail-closed local headless-resume planning and execution."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from sidecar.model import Session, Status
from sidecar.process_runner import BoundedProcessResult, run_bounded


MAX_MESSAGE_BYTES = 16 * 1024
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
DEFAULT_SEND_TIMEOUT_SECONDS = 300.0
MAX_SEND_TIMEOUT_SECONDS = 900.0
MAX_SESSION_ID_BYTES = 512

SUPPORTED_AGENTS = frozenset(("claude", "codex", "cursor-cli"))
SEND_OUTCOMES = frozenset(("completed", "failed", "timed_out", "overflow"))
DELIVERY_STATES = frozenset(("delivered", "unknown"))

_EXECUTABLE_NAMES = {
    "claude": "claude",
    "codex": "codex",
    "cursor-cli": "cursor-agent",
}
_PROMPT_TRANSPORTS = {
    "claude": "stdin",
    "codex": "stdin",
    "cursor-cli": "argv",
}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_UNSUPPORTED_AGENT_CODES = {
    "cursor-ide": "unsupported_cursor_ide",
    "copilot": "unsupported_copilot",
    "kimi": "unsupported_kimi",
    "dsh": "unsupported_dsh",
}
_ERROR_MESSAGES = {
    "invalid_message_type": "message must be text",
    "invalid_message_utf8": "message must contain valid Unicode scalars",
    "blank_message": "message must not be blank",
    "message_nul": "message must not contain NUL",
    "message_too_large": "message exceeds the size limit",
    "invalid_session": "session is invalid",
    "unsupported_agent": "session agent is unsupported",
    "unsupported_cursor_ide": "Cursor IDE sessions cannot be resumed by cursor-agent",
    "unsupported_copilot": "Copilot headless resume is unsupported",
    "unsupported_kimi": "Kimi print resume is unsafe in the verified version",
    "unsupported_dsh": "DSH has no supported stock headless resume",
    "working_session": "working sessions cannot be resumed",
    "dead_session": "dead sessions cannot be resumed",
    "child_session": "child and sidechain sessions cannot be resumed",
    "remote_session": "remote sessions cannot be resumed",
    "invalid_session_id": "session identifier is invalid",
    "invalid_project": "session project directory is invalid",
    "executable_not_found": "required agent executable was not found",
    "invalid_executable": "resolved agent executable is invalid",
    "write_not_allowed": "headless resume requires explicit write permission",
    "invalid_timeout": "timeout must be finite and between 1 and 900 seconds",
    "invalid_plan": "send plan failed preflight validation",
}


def _utf8_scalar_text(text: str) -> str:
    """Replace only surrogate code points, preserving all valid text exactly."""

    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "".join(
            "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character
            for character in text
        )
    return text


class SendError(ValueError):
    """A pre-spawn failure carrying only a stable, display-safe code."""

    def __init__(self, code: str) -> None:
        if code not in _ERROR_MESSAGES:
            raise ValueError("invalid send error code")
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code}


@dataclass(frozen=True, repr=False)
class SendPlan:
    """An immutable, shell-free native resume invocation."""

    agent: str
    session_id: str
    executable: str
    argv: Tuple[str, ...] = field(repr=False)
    cwd: str
    input_data: Optional[bytes] = field(default=None, repr=False)
    prompt_transport: str = "stdin"

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        if isinstance(self.input_data, bytearray):
            object.__setattr__(self, "input_data", bytes(self.input_data))

    def __repr__(self) -> str:
        return (
            "SendPlan(agent={!r}, session_id={!r}, executable={!r}, "
            "cwd={!r}, prompt_transport={!r})"
        ).format(
            self.agent,
            self.session_id,
            self.executable,
            self.cwd,
            self.prompt_transport,
        )


@dataclass(frozen=True)
class SendResult:
    """Bounded native outcome without the submitted message or invocation."""

    agent: str
    session_id: str
    outcome: str
    delivery: str
    returncode: Optional[int] = None
    response: str = ""
    stderr: str = ""
    error_code: Optional[str] = None

    def __post_init__(self) -> None:
        if self.returncode is not None and type(self.returncode) is not int:
            raise ValueError("send result return code must be an integer")
        if self.outcome not in SEND_OUTCOMES:
            raise ValueError("invalid send outcome")
        if self.delivery not in DELIVERY_STATES:
            raise ValueError("invalid delivery state")
        if self.delivery == "delivered" and (
            self.outcome != "completed" or self.returncode != 0
        ):
            raise ValueError("delivered results require native success")
        if self.outcome == "completed" and self.delivery != "delivered":
            raise ValueError("completed results must be delivered")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent": self.agent,
            "session_id": self.session_id,
            "outcome": self.outcome,
            "delivery": self.delivery,
            "returncode": self.returncode,
            "response": self.response,
            "stderr": self.stderr,
            "error_code": self.error_code,
        }


def validate_message(message: object) -> bytes:
    """Validate a prompt and return its exact UTF-8 representation."""

    if not isinstance(message, str):
        raise SendError("invalid_message_type")
    try:
        encoded = message.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SendError("invalid_message_utf8") from error
    if "\x00" in message:
        raise SendError("message_nul")
    if not message.strip():
        raise SendError("blank_message")
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise SendError("message_too_large")
    return encoded


def _validate_session_id(value: object) -> str:
    if not isinstance(value, str):
        raise SendError("invalid_session_id")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SendError("invalid_session_id") from error
    if (
        not encoded
        or len(encoded) > MAX_SESSION_ID_BYTES
        or _SESSION_ID_RE.fullmatch(value) is None
    ):
        raise SendError("invalid_session_id")
    return value


def _resolve_project(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SendError("invalid_project")
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise SendError("invalid_project")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir():
            raise SendError("invalid_project")
    except SendError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise SendError("invalid_project") from error
    return str(resolved)


def _resolve_executable(
    executable_name: str,
    resolver: Callable[[str], Optional[str]],
) -> str:
    try:
        value = resolver(executable_name)
    except OSError as error:
        raise SendError("executable_not_found") from error
    if value is None:
        raise SendError("executable_not_found")
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SendError("invalid_executable")
    try:
        resolved = Path(value).expanduser().resolve(strict=True)
        mode = resolved.stat().st_mode
        if (
            not resolved.is_absolute()
            or not stat.S_ISREG(mode)
            or not os.access(str(resolved), os.X_OK)
        ):
            raise SendError("invalid_executable")
    except FileNotFoundError as error:
        raise SendError("executable_not_found") from error
    except SendError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise SendError("invalid_executable") from error
    return str(resolved)


def _session_agent(session: Session) -> str:
    agent = session.agent
    if not isinstance(agent, str):
        raise SendError("unsupported_agent")
    if agent in SUPPORTED_AGENTS:
        return agent
    code = _UNSUPPORTED_AGENT_CODES.get(agent, "unsupported_agent")
    raise SendError(code)


def _validate_session(session: object) -> Tuple[Session, str, str, str]:
    if not isinstance(session, Session):
        raise SendError("invalid_session")
    agent = _session_agent(session)
    if session.status == Status.WORKING:
        raise SendError("working_session")
    if session.status == Status.DEAD:
        raise SendError("dead_session")
    if session.status not in (Status.WAITING, Status.IDLE):
        raise SendError("invalid_session")
    if not isinstance(session.extra, Mapping):
        raise SendError("invalid_session")
    sidechain = session.extra.get("sidechain", False)
    if session.parent_id is not None or sidechain is not False:
        raise SendError("child_session")
    if (
        "host" in session.extra
        or session.extra.get("remote") is True
        or session.extra.get("source") == "remote"
    ):
        raise SendError("remote_session")
    session_id = _validate_session_id(session.session_id)
    project = _resolve_project(session.project)
    return session, agent, session_id, project


def _send_arguments(
    agent: str,
    executable: str,
    session_id: str,
    message: str,
) -> Tuple[str, ...]:
    if agent == "claude":
        return (
            executable,
            "--print",
            "--resume",
            session_id,
            "--input-format",
            "text",
            "--output-format",
            "json",
        )
    if agent == "codex":
        return (executable, "exec", "resume", "--json", session_id, "-")
    if agent == "cursor-cli":
        return (
            executable,
            "--print",
            "--output-format",
            "json",
            "--resume",
            session_id,
            "--",
            message,
        )
    raise SendError("unsupported_agent")


def build_send_plan(
    session: Session,
    message: str,
    executable_resolver: Callable[[str], Optional[str]] = shutil.which,
) -> SendPlan:
    """Build a fixed local resume invocation after all filesystem preflight."""

    message_bytes = validate_message(message)
    _session, agent, session_id, project = _validate_session(session)
    executable = _resolve_executable(
        _EXECUTABLE_NAMES[agent],
        executable_resolver,
    )
    transport = _PROMPT_TRANSPORTS[agent]
    return SendPlan(
        agent=agent,
        session_id=session_id,
        executable=executable,
        argv=_send_arguments(agent, executable, session_id, message),
        cwd=project,
        input_data=message_bytes if transport == "stdin" else None,
        prompt_transport=transport,
    )


def _plan_message(plan: SendPlan) -> str:
    if plan.agent in ("claude", "codex"):
        if not isinstance(plan.input_data, bytes):
            raise SendError("invalid_plan")
        try:
            message = plan.input_data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SendError("invalid_plan") from error
        if validate_message(message) != plan.input_data:
            raise SendError("invalid_plan")
        return message
    if plan.agent == "cursor-cli":
        if plan.input_data is not None or len(plan.argv) != 8:
            raise SendError("invalid_plan")
        message = plan.argv[-1]
        validate_message(message)
        return message
    raise SendError("invalid_plan")


def _preflight_plan(plan: object) -> Tuple[SendPlan, str]:
    if not isinstance(plan, SendPlan):
        raise SendError("invalid_plan")
    if not isinstance(plan.agent, str) or plan.agent not in SUPPORTED_AGENTS:
        raise SendError("invalid_plan")
    session_id = _validate_session_id(plan.session_id)
    if plan.prompt_transport != _PROMPT_TRANSPORTS[plan.agent]:
        raise SendError("invalid_plan")
    project = _resolve_project(plan.cwd)
    if project != plan.cwd:
        raise SendError("invalid_plan")
    executable = _resolve_executable(
        _EXECUTABLE_NAMES[plan.agent],
        lambda _name: plan.executable,
    )
    if executable != plan.executable:
        raise SendError("invalid_plan")
    message = _plan_message(plan)
    expected = _send_arguments(plan.agent, executable, session_id, message)
    if plan.argv != expected:
        raise SendError("invalid_plan")
    return plan, message


def _validate_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise SendError("invalid_timeout")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise SendError("invalid_timeout") from error
    if (
        not math.isfinite(timeout)
        or timeout < 1.0
        or timeout > MAX_SEND_TIMEOUT_SECONDS
    ):
        raise SendError("invalid_timeout")
    return timeout


def _output_bytes(value: object, limit: int) -> Tuple[bytes, bool]:
    if isinstance(value, str):
        raw = _utf8_scalar_text(value).encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raw = b""
    return raw[:limit], len(raw) > limit


def _decoded_output(value: object, limit: int) -> Tuple[str, bool]:
    raw, oversized = _output_bytes(value, limit)
    return raw.decode("utf-8", "replace"), oversized


def _content_text(value: Any, depth: int = 0) -> str:
    if depth > 8:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        parts = [_content_text(item, depth + 1) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, Mapping):
        for key in ("text", "content", "message", "output", "result"):
            if key in value:
                text = _content_text(value[key], depth + 1)
                if text:
                    return text
    return ""


def _json_values(payload: str) -> List[Any]:
    try:
        return [json.loads(payload)]
    except (json.JSONDecodeError, RecursionError, UnicodeError):
        values: List[Any] = []
        for line in payload.splitlines():
            if not line.strip():
                continue
            try:
                values.append(json.loads(line))
            except (json.JSONDecodeError, RecursionError, UnicodeError):
                continue
        return values


def _assistant_record_text(record: Any) -> str:
    if not isinstance(record, Mapping):
        return ""
    if "result" in record:
        text = _content_text(record.get("result"))
        if text:
            return text
    record_type = str(record.get("type") or "").casefold()
    role = str(record.get("role") or "").casefold()
    message = record.get("message")
    if isinstance(message, Mapping):
        role = str(message.get("role") or role).casefold()
    if record_type not in ("assistant", "assistant_message", "message") and role != (
        "assistant"
    ):
        return ""
    source = message if isinstance(message, Mapping) else record
    return _content_text(source.get("content", source.get("text")))


def _parse_claude_or_cursor(payload: str) -> str:
    values = _json_values(payload)
    candidates: List[str] = []
    for value in values:
        records = value if isinstance(value, list) else [value]
        for record in records:
            text = _assistant_record_text(record)
            if text:
                candidates.append(text)
    return candidates[-1] if candidates else payload


def _codex_record_text(record: Any) -> Tuple[str, str]:
    if not isinstance(record, Mapping):
        return "", ""
    final = _content_text(record.get("last_agent_message"))
    containers: List[Mapping[str, Any]] = [record]
    for key in ("item", "payload"):
        value = record.get(key)
        if isinstance(value, Mapping):
            containers.append(value)
            nested = value.get("item")
            if isinstance(nested, Mapping):
                containers.append(nested)
            if not final:
                final = _content_text(value.get("last_agent_message"))
    for container in containers:
        item_type = str(container.get("type") or "").casefold().replace("-", "_")
        if item_type in ("agent_message", "assistant", "assistant_message"):
            text = _content_text(
                container.get(
                    "text",
                    container.get("message", container.get("content")),
                )
            )
            if text:
                return text, final
    return "", final


def _parse_codex(payload: str) -> str:
    messages: List[str] = []
    final = ""
    for value in _json_values(payload):
        records = value if isinstance(value, list) else [value]
        for record in records:
            message, candidate_final = _codex_record_text(record)
            if message and (not messages or messages[-1] != message):
                messages.append(message)
            if candidate_final:
                final = candidate_final
    if final:
        return final
    return "\n".join(messages) if messages else payload


def _redact_message(text: str, message: str) -> str:
    candidates = {message}
    for ensure_ascii in (False, True):
        encoded = json.dumps(message, ensure_ascii=ensure_ascii)
        candidates.add(encoded[1:-1])
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            text = text.replace(candidate, "[message redacted]")
    return text


def _bounded_result_text(text: str, limit: int) -> str:
    normalized = _utf8_scalar_text(text)
    raw = normalized.encode("utf-8")
    if len(raw) <= limit:
        return normalized
    return raw[:limit].decode("utf-8", "ignore")


def _result_returncode(completed: object) -> Optional[int]:
    value = getattr(completed, "returncode", None)
    return value if type(value) is int else None


def _render_output(
    agent: str,
    stdout: object,
    stderr: object,
    message: str,
) -> Tuple[str, str, bool]:
    output_text, stdout_oversized = _decoded_output(stdout, MAX_STDOUT_BYTES)
    error_text, stderr_oversized = _decoded_output(stderr, MAX_STDERR_BYTES)
    response = (
        _parse_codex(output_text)
        if agent == "codex"
        else _parse_claude_or_cursor(output_text)
    )
    response = _bounded_result_text(
        _redact_message(response, message),
        MAX_STDOUT_BYTES,
    )
    error_text = _bounded_result_text(
        _redact_message(error_text, message),
        MAX_STDERR_BYTES,
    )
    return response, error_text, stdout_oversized or stderr_oversized


def execute_send(
    plan: SendPlan,
    *,
    allow_write: bool = False,
    timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
    runner: Callable[..., BoundedProcessResult] = run_bounded,
    monotonic: Callable[[], float] = time.monotonic,
) -> SendResult:
    """Execute one preflighted native resume without retries or persistence."""

    if allow_write is not True:
        raise SendError("write_not_allowed")
    bounded_timeout = _validate_timeout(timeout)
    validated_plan, message = _preflight_plan(plan)
    try:
        completed = runner(
            validated_plan.argv,
            validated_plan.input_data,
            input_limit=MAX_MESSAGE_BYTES,
            stdout_limit=MAX_STDOUT_BYTES,
            stderr_limit=MAX_STDERR_BYTES,
            timeout=bounded_timeout,
            env=None,
            cwd=validated_plan.cwd,
            monotonic=monotonic,
        )
    except subprocess.TimeoutExpired as error:
        response, error_text, _oversized = _render_output(
            validated_plan.agent,
            error.output,
            error.stderr,
            message,
        )
        return SendResult(
            agent=validated_plan.agent,
            session_id=validated_plan.session_id,
            outcome="timed_out",
            delivery="unknown",
            response=response,
            stderr=error_text,
            error_code="timeout",
        )
    except OSError:
        return SendResult(
            agent=validated_plan.agent,
            session_id=validated_plan.session_id,
            outcome="failed",
            delivery="unknown",
            error_code="spawn_error",
        )

    response, error_text, oversized = _render_output(
        validated_plan.agent,
        getattr(completed, "stdout", b""),
        getattr(completed, "stderr", b""),
        message,
    )
    overflow = getattr(completed, "overflow", None)
    if overflow not in (None, "input", "stdout", "stderr"):
        overflow = None
    returncode = _result_returncode(completed)
    if overflow is not None or oversized:
        return SendResult(
            agent=validated_plan.agent,
            session_id=validated_plan.session_id,
            outcome="overflow",
            delivery="unknown",
            returncode=returncode,
            response=response,
            stderr=error_text,
            error_code="{}_overflow".format(overflow or "output"),
        )

    if returncode == 0:
        return SendResult(
            agent=validated_plan.agent,
            session_id=validated_plan.session_id,
            outcome="completed",
            delivery="delivered",
            returncode=0,
            response=response,
            stderr=error_text,
        )
    return SendResult(
        agent=validated_plan.agent,
        session_id=validated_plan.session_id,
        outcome="failed",
        delivery="unknown",
        returncode=returncode,
        response=response,
        stderr=error_text,
        error_code="native_exit",
    )


__all__ = [
    "DEFAULT_SEND_TIMEOUT_SECONDS",
    "DELIVERY_STATES",
    "MAX_MESSAGE_BYTES",
    "MAX_SEND_TIMEOUT_SECONDS",
    "MAX_SESSION_ID_BYTES",
    "MAX_STDERR_BYTES",
    "MAX_STDOUT_BYTES",
    "SEND_OUTCOMES",
    "SUPPORTED_AGENTS",
    "SendError",
    "SendPlan",
    "SendResult",
    "build_send_plan",
    "execute_send",
    "validate_message",
]
