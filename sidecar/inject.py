"""Fail-closed local headless-resume planning and execution."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from sidecar.model import Session, Status
from sidecar.process_runner import (
    BoundedProcessResult,
    DescendantContainmentUnsupportedError,
    run_bounded,
)
from sidecar.send_audit import (
    AuditError,
    AuditIdentity,
    AuditNamespace,
    AuditReceipt,
    SendAuditStore,
    generate_request_id,
    make_audit_identity,
    validate_request_id,
)
from sidecar.text_utils import normalize_scalar_text, redact_message

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX Python
    fcntl = None  # type: ignore[assignment]


MAX_MESSAGE_BYTES = 16 * 1024
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
DEFAULT_SEND_TIMEOUT_SECONDS = 300.0
MAX_SEND_TIMEOUT_SECONDS = 900.0
MAX_SESSION_ID_BYTES = 512
SEND_LOCK_DIRECTORY = "send-locks"
RUNTIME_ENV = "AGENT_SIDECAR_RUNTIME_DIR"

SUPPORTED_AGENTS = frozenset(("claude", "codex", "cursor-cli"))
SEND_OUTCOMES = frozenset(
    (
        "completed",
        "failed",
        "timed_out",
        "overflow",
        "request_pending",
        "audit_error",
    )
)
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
    "revalidation_required": "send execution requires a fresh session revalidation",
    "session_busy": "session already has a resume in progress",
    "unsafe_lock": "session lock path is unsafe",
    "session_changed": "session changed since send planning",
    "session_unavailable": "session is unavailable for fresh revalidation",
    "audit_corrupt": "send audit is corrupt",
    "audit_error": "send audit could not be updated",
    "invalid_request_id": (
        "request ID must be conservative ASCII and at most 128 bytes"
    ),
    "request_conflict": "request ID was already used for a different target",
}
_CRITICAL_EXTRA_KEYS = (
    "source",
    "sidechain",
    "transcript_kind",
    "store",
    "wal",
    "directory_session_id",
    "cwd_hash",
    "agentId",
    "agent_id",
    "agent_id_matches_directory",
    "session_id_mismatch",
    "host",
    "remote",
)


class SendError(ValueError):
    """A pre-spawn failure carrying only a stable, display-safe code."""

    def __init__(self, code: str) -> None:
        if code not in _ERROR_MESSAGES:
            raise ValueError("invalid send error code")
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code}


FilesystemIdentity = Tuple[str, int, int, int]
ExecutableIdentity = Tuple[str, int, int, int, int, int, int, str]
SourceFileSignature = Tuple[str, bool, int, int, int, int, int, int]


@dataclass(frozen=True, repr=False)
class SendTarget:
    """Message-free identity and source snapshot captured while planning."""

    agent: str
    session_id: str
    project: str
    project_identity: FilesystemIdentity
    transcript: str
    parent_id: Optional[str]
    critical_identity: Tuple[Tuple[str, object], ...]
    source_signature: Tuple[SourceFileSignature, ...]
    executable_identity: ExecutableIdentity


@dataclass(frozen=True, repr=False)
class SendPlan:
    """An immutable, shell-free native resume invocation."""

    agent: str
    session_id: str
    executable: str
    argv: Tuple[str, ...] = field(repr=False)
    cwd: str
    target: SendTarget = field(repr=False)
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
    request_id: str = ""
    replayed: bool = False

    def __post_init__(self) -> None:
        if self.request_id:
            try:
                validate_request_id(self.request_id)
            except AuditError as error:
                raise ValueError("invalid send result request ID") from error
        if type(self.replayed) is not bool:
            raise ValueError("send result replayed flag must be boolean")
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
            "request_id": self.request_id,
            "replayed": self.replayed,
        }


def validate_message(message: object) -> bytes:
    """Validate a prompt and return its exact UTF-8 representation."""

    if not isinstance(message, str):
        raise SendError("invalid_message_type")
    try:
        normalized = normalize_scalar_text(message, errors="strict")
    except UnicodeEncodeError as error:
        raise SendError("invalid_message_utf8") from error
    encoded = normalized.encode("utf-8")
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


def _directory_identity(path: str) -> FilesystemIdentity:
    try:
        details = os.stat(path)
    except OSError as error:
        raise SendError("invalid_project") from error
    if not stat.S_ISDIR(details.st_mode):
        raise SendError("invalid_project")
    return (
        path,
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
    )


def _executable_identity(path: str) -> ExecutableIdentity:
    try:
        with open(path, "rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        linked = os.stat(path)
    except OSError as error:
        raise SendError("invalid_executable") from error
    before_signature = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_mode),
        int(before.st_size),
        int(getattr(before, "st_mtime_ns", int(before.st_mtime * 1e9))),
        int(getattr(before, "st_ctime_ns", int(before.st_ctime * 1e9))),
    )
    after_signature = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_mode),
        int(after.st_size),
        int(getattr(after, "st_mtime_ns", int(after.st_mtime * 1e9))),
        int(getattr(after, "st_ctime_ns", int(after.st_ctime * 1e9))),
    )
    linked_signature = (
        int(linked.st_dev),
        int(linked.st_ino),
        int(linked.st_mode),
        int(linked.st_size),
        int(getattr(linked, "st_mtime_ns", int(linked.st_mtime * 1e9))),
        int(getattr(linked, "st_ctime_ns", int(linked.st_ctime * 1e9))),
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or not os.access(path, os.X_OK)
        or before_signature != after_signature
        or before_signature != linked_signature
    ):
        raise SendError("invalid_executable")
    return (
        path,
        *before_signature,
        digest.hexdigest(),
    )


def _transcript_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SendError("session_unavailable")
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise SendError("session_unavailable")
        return os.path.abspath(str(candidate))
    except SendError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise SendError("session_unavailable") from error


def _source_paths(session: Session, transcript: str) -> Tuple[str, ...]:
    paths = [transcript]
    if session.extra.get("transcript_kind") == "cursor-chat-sqlite":
        wal = session.extra.get("wal")
        wal_path = (
            os.path.abspath(os.path.expanduser(wal))
            if isinstance(wal, str) and wal and "\x00" not in wal
            else transcript + "-wal"
        )
        if wal_path not in paths:
            paths.append(wal_path)
    return tuple(paths)


def _stat_source(path: str, *, required: bool) -> SourceFileSignature:
    try:
        details = os.stat(path)
    except FileNotFoundError as error:
        if required:
            raise SendError("session_unavailable") from error
        return (path, False, 0, 0, 0, 0, 0, 0)
    except OSError as error:
        raise SendError("session_unavailable") from error
    if not stat.S_ISREG(details.st_mode):
        raise SendError("session_unavailable")
    mtime_ns = int(
        getattr(details, "st_mtime_ns", int(details.st_mtime * 1e9))
    )
    ctime_ns = int(
        getattr(details, "st_ctime_ns", int(details.st_ctime * 1e9))
    )
    return (
        path,
        True,
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        mtime_ns,
        ctime_ns,
        int(details.st_size),
    )


def _critical_identity(session: Session) -> Tuple[Tuple[str, object], ...]:
    identity: List[Tuple[str, object]] = []
    for key in _CRITICAL_EXTRA_KEYS:
        value = session.extra.get(key)
        if value is None or isinstance(value, (bool, int, float, str)):
            identity.append((key, value))
        else:
            raise SendError("invalid_session")
    return tuple(identity)


def _send_target(
    session: Session,
    agent: str,
    session_id: str,
    project: str,
    executable: str,
) -> SendTarget:
    transcript = _transcript_path(session.transcript)
    paths = _source_paths(session, transcript)
    signature = tuple(
        _stat_source(path, required=index == 0)
        for index, path in enumerate(paths)
    )
    return SendTarget(
        agent=agent,
        session_id=session_id,
        project=project,
        project_identity=_directory_identity(project),
        transcript=transcript,
        parent_id=session.parent_id,
        critical_identity=_critical_identity(session),
        source_signature=signature,
        executable_identity=_executable_identity(executable),
    )


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
    target = _send_target(
        _session,
        agent,
        session_id,
        project,
        executable,
    )
    transport = _PROMPT_TRANSPORTS[agent]
    return SendPlan(
        agent=agent,
        session_id=session_id,
        executable=executable,
        argv=_send_arguments(agent, executable, session_id, message),
        cwd=project,
        target=target,
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


def _preflight_plan(
    plan: object,
    *,
    filesystem: bool = True,
) -> Tuple[SendPlan, str]:
    if not isinstance(plan, SendPlan):
        raise SendError("invalid_plan")
    if not isinstance(plan.agent, str) or plan.agent not in SUPPORTED_AGENTS:
        raise SendError("invalid_plan")
    session_id = _validate_session_id(plan.session_id)
    if not isinstance(plan.target, SendTarget):
        raise SendError("invalid_plan")
    if (
        plan.target.agent != plan.agent
        or plan.target.session_id != session_id
        or plan.target.project != plan.cwd
        or not isinstance(plan.target.project_identity, tuple)
        or len(plan.target.project_identity) != 4
        or not isinstance(plan.target.transcript, str)
        or not plan.target.transcript
        or not isinstance(plan.target.critical_identity, tuple)
        or not isinstance(plan.target.source_signature, tuple)
        or not isinstance(plan.target.executable_identity, tuple)
        or len(plan.target.executable_identity) != 8
        or plan.target.executable_identity[0] != plan.executable
    ):
        raise SendError("invalid_plan")
    if plan.prompt_transport != _PROMPT_TRANSPORTS[plan.agent]:
        raise SendError("invalid_plan")
    if (
        not isinstance(plan.cwd, str)
        or not os.path.isabs(plan.cwd)
        or "\x00" in plan.cwd
    ):
        raise SendError("invalid_plan")
    if (
        not isinstance(plan.executable, str)
        or not os.path.isabs(plan.executable)
        or "\x00" in plan.executable
    ):
        raise SendError("invalid_plan")
    message = _plan_message(plan)
    expected = _send_arguments(plan.agent, plan.executable, session_id, message)
    if plan.argv != expected:
        raise SendError("invalid_plan")
    if filesystem:
        project = _resolve_project(plan.cwd)
        if project != plan.cwd:
            raise SendError("invalid_plan")
        if (
            _directory_identity(project) != plan.target.project_identity
        ):
            raise SendError("session_changed")
        executable = _resolve_executable(
            _EXECUTABLE_NAMES[plan.agent],
            lambda _name: plan.executable,
        )
        if executable != plan.executable:
            raise SendError("invalid_executable")
        if (
            _executable_identity(executable) != plan.target.executable_identity
        ):
            raise SendError("session_changed")
        current_source_signature = tuple(
            _stat_source(signature[0], required=index == 0)
            for index, signature in enumerate(plan.target.source_signature)
        )
        if current_source_signature != plan.target.source_signature:
            raise SendError("session_changed")
    return plan, message


def _runtime_root(
    runtime_dir: Optional[Union[str, os.PathLike]],
) -> Path:
    configured: Union[str, os.PathLike]
    if runtime_dir is None:
        configured = os.environ.get(RUNTIME_ENV) or str(
            Path.home() / ".agent_sidecar"
        )
    else:
        configured = runtime_dir
    try:
        root = Path(configured).expanduser()
    except (TypeError, ValueError) as error:
        raise SendError("unsafe_lock") from error
    if not root.is_absolute():
        raise SendError("unsafe_lock")
    if any(part in ("", ".", "..") for part in root.parts[1:]):
        raise SendError("unsafe_lock")
    return root


DirectoryLink = Tuple[int, str, int, bool]


def _require_anchored_lock_support() -> None:
    if (
        os.name != "posix"
        or not hasattr(os, "geteuid")
        or getattr(os, "O_DIRECTORY", None) is None
        or getattr(os, "O_NOFOLLOW", None) is None
        or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
    ):
        raise SendError("unsafe_lock")


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _validate_directory(details: os.stat_result, *, private: bool) -> None:
    if not stat.S_ISDIR(details.st_mode):
        raise SendError("unsafe_lock")
    permissions = stat.S_IMODE(details.st_mode)
    if private:
        if details.st_uid != os.geteuid() or permissions != 0o700:
            raise SendError("unsafe_lock")
        return
    if details.st_uid not in (0, os.geteuid()):
        raise SendError("unsafe_lock")
    if permissions & 0o022:
        root_sticky = details.st_uid == 0 and bool(details.st_mode & stat.S_ISVTX)
        if not root_sticky:
            raise SendError("unsafe_lock")


def _entry_stat(parent_fd: int, name: str) -> os.stat_result:
    try:
        return os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise SendError("unsafe_lock") from error


def _verify_directory_link(link: DirectoryLink) -> None:
    parent_fd, name, child_fd, private = link
    try:
        opened = os.fstat(child_fd)
    except OSError as error:
        raise SendError("unsafe_lock") from error
    linked = _entry_stat(parent_fd, name)
    _validate_directory(opened, private=private)
    _validate_directory(linked, private=private)
    if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
        raise SendError("unsafe_lock")


def _open_directory_at(
    parent_fd: int,
    name: str,
    *,
    create: bool,
    private: bool,
) -> Tuple[int, DirectoryLink]:
    created = False
    creation_observed = False
    try:
        descriptor = os.open(
            name,
            _directory_flags(),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        creation_observed = True
        if not create:
            raise SendError("unsafe_lock")
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            # A competing creator won after the anchored ENOENT observation.
            # Reopen exactly once below and validate the resulting inode.
            pass
        except OSError as error:
            raise SendError("unsafe_lock") from error
        try:
            descriptor = os.open(
                name,
                _directory_flags(),
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise SendError("unsafe_lock") from error
    except OSError as error:
        raise SendError("unsafe_lock") from error
    try:
        if created:
            os.fchmod(descriptor, 0o700)
        link = (parent_fd, name, descriptor, private)
        _verify_directory_link(link)
        if creation_observed:
            try:
                os.fsync(parent_fd)
            except OSError as error:
                raise SendError("unsafe_lock") from error
        return descriptor, link
    except BaseException:
        os.close(descriptor)
        raise


def _open_runtime_directories(
    runtime_dir: Optional[Union[str, os.PathLike]],
) -> Tuple[List[int], List[DirectoryLink], int]:
    descriptors, links, parent_fd, name, _canonical = (
        _open_runtime_parent(runtime_dir)
    )
    try:
        child_fd, link = _open_directory_at(
            parent_fd,
            name,
            create=True,
            private=True,
        )
        descriptors.append(child_fd)
        links.append(link)
        return descriptors, links, child_fd
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _open_runtime_parent(
    runtime_dir: Optional[Union[str, os.PathLike]],
) -> Tuple[List[int], List[DirectoryLink], int, str, str]:
    _require_anchored_lock_support()
    root = _runtime_root(runtime_dir)
    components = root.parts[1:]
    if not components:
        raise SendError("unsafe_lock")
    try:
        root_fd = os.open("/", _directory_flags())
    except OSError as error:
        raise SendError("unsafe_lock") from error
    descriptors = [root_fd]
    links: List[DirectoryLink] = []
    try:
        _validate_directory(os.fstat(root_fd), private=False)
        current_fd = root_fd
        for component in components[:-1]:
            child_fd, link = _open_directory_at(
                current_fd,
                component,
                create=False,
                private=False,
            )
            descriptors.append(child_fd)
            links.append(link)
            current_fd = child_fd
        return (
            descriptors,
            links,
            current_fd,
            components[-1],
            str(root),
        )
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _lock_name(agent: str, session_id: str) -> str:
    identity = "{}\0{}\0{}".format(os.geteuid(), agent, session_id).encode(
        "utf-8"
    )
    return "{}.lock".format(hashlib.sha256(identity).hexdigest())


def _validate_lock_file(directory_fd: int, name: str, descriptor: int) -> None:
    try:
        opened = os.fstat(descriptor)
    except OSError as error:
        raise SendError("unsafe_lock") from error
    linked = _entry_stat(directory_fd, name)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or opened.st_uid != os.geteuid()
        or linked.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or stat.S_IMODE(linked.st_mode) != 0o600
        or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
    ):
        raise SendError("unsafe_lock")


def _open_lock_file(directory_fd: int, name: str) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    created = False
    try:
        descriptor = os.open(
            name,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=directory_fd,
        )
        created = True
    except FileExistsError:
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise SendError("unsafe_lock") from error
    except OSError as error:
        raise SendError("unsafe_lock") from error
    try:
        if created:
            os.fchmod(descriptor, 0o600)
        _validate_lock_file(directory_fd, name, descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _session_lock(
    agent: str,
    session_id: str,
    runtime_dir: Optional[Union[str, os.PathLike]],
    runtime_namespace: Optional[AuditNamespace] = None,
) -> Iterator[Callable[[], None]]:
    if fcntl is None or os.name != "posix":
        raise SendError("unsafe_lock")
    directory_fds: List[int] = []
    links: List[DirectoryLink] = []
    descriptor: Optional[int] = None
    locked = False
    try:
        if runtime_namespace is None:
            directory_fds, links, runtime_fd = _open_runtime_directories(
                runtime_dir
            )
            runtime_links = tuple(links)

            def validate_runtime_namespace() -> None:
                for retained_link in runtime_links:
                    _verify_directory_link(retained_link)

        else:
            runtime_namespace.validate()
            runtime_fd = runtime_namespace.runtime_fd

            def validate_runtime_namespace() -> None:
                runtime_namespace.validate()

        lock_directory_fd, lock_link = _open_directory_at(
            runtime_fd,
            SEND_LOCK_DIRECTORY,
            create=True,
            private=True,
        )
        directory_fds.append(lock_directory_fd)
        links.append(lock_link)
        name = _lock_name(agent, session_id)
        descriptor = _open_lock_file(lock_directory_fd, name)
        try:
            os.fsync(lock_directory_fd)
        except OSError as error:
            raise SendError("unsafe_lock") from error

        def validate_lock_namespace() -> None:
            validate_runtime_namespace()
            _verify_directory_link(lock_link)
            assert descriptor is not None
            _validate_lock_file(lock_directory_fd, name, descriptor)

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise SendError("session_busy") from error
            raise SendError("unsafe_lock") from error
        locked = True
        validate_lock_namespace()
        yield validate_lock_namespace
    finally:
        if descriptor is not None:
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(descriptor)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _refreshed_send_plan(
    planned: SendPlan,
    message: str,
    refresher: Callable[[], Iterable[Session]],
    executable_resolver: Callable[[str], Optional[str]],
) -> SendPlan:
    try:
        refreshed = list(refresher())
    except Exception as error:
        raise SendError("session_unavailable") from error
    matches = [
        session
        for session in refreshed
        if isinstance(session, Session)
        and session.agent == planned.agent
        and session.session_id == planned.session_id
    ]
    if not matches:
        raise SendError("session_unavailable")
    if len(matches) != 1:
        raise SendError("session_changed")
    final_plan = build_send_plan(
        matches[0],
        message,
        executable_resolver=executable_resolver,
    )
    if final_plan.target != planned.target:
        raise SendError("session_changed")
    validated, _message = _preflight_plan(final_plan)
    return validated


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
        raw = normalize_scalar_text(value, errors="replace").encode("utf-8")
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


def _bounded_result_text(text: str, limit: int) -> str:
    normalized = normalize_scalar_text(text, errors="replace")
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
        redact_message(response, message),
        MAX_STDOUT_BYTES,
    )
    error_text = _bounded_result_text(
        redact_message(error_text, message),
        MAX_STDERR_BYTES,
    )
    return response, error_text, stdout_oversized or stderr_oversized


def _run_native_send(
    planned: SendPlan,
    message: str,
    *,
    bounded_timeout: float,
    refresher: Callable[[], Iterable[Session]],
    executable_resolver: Callable[[str], Optional[str]],
    runtime_dir: Optional[Union[str, os.PathLike]],
    runtime_namespace: AuditNamespace,
    runner: Callable[..., BoundedProcessResult],
    monotonic: Callable[[], float],
    request_id: str,
) -> SendResult:
    with _session_lock(
        planned.agent,
        planned.session_id,
        runtime_dir,
        runtime_namespace,
    ) as validate_lock_namespace:
        validated_plan = _refreshed_send_plan(
            planned,
            message,
            refresher,
            executable_resolver,
        )
        validate_lock_namespace()
        validated_plan, _message = _preflight_plan(validated_plan)

        def revalidate_at_spawn() -> None:
            validate_lock_namespace()
            _preflight_plan(validated_plan)

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
                pre_exec=revalidate_at_spawn,
                require_descendant_containment=True,
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
                error_code=(
                    "cleanup_incomplete"
                    if getattr(error, "cleanup_incomplete", False) is True
                    else "timeout"
                ),
                request_id=request_id,
            )
        except DescendantContainmentUnsupportedError:
            return SendResult(
                agent=validated_plan.agent,
                session_id=validated_plan.session_id,
                outcome="failed",
                delivery="unknown",
                error_code="containment_unsupported",
                request_id=request_id,
            )
        except OSError:
            return SendResult(
                agent=validated_plan.agent,
                session_id=validated_plan.session_id,
                outcome="failed",
                delivery="unknown",
                error_code="spawn_error",
                request_id=request_id,
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
        if getattr(completed, "cleanup_incomplete", False) is True:
            return SendResult(
                agent=validated_plan.agent,
                session_id=validated_plan.session_id,
                outcome="failed",
                delivery="unknown",
                returncode=returncode,
                response=response,
                stderr=error_text,
                error_code="cleanup_incomplete",
                request_id=request_id,
            )
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
                request_id=request_id,
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
                request_id=request_id,
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
            request_id=request_id,
        )


def _send_audit_identity(plan: SendPlan, message: str) -> AuditIdentity:
    return make_audit_identity(
        agent=plan.agent,
        session_id=plan.session_id,
        project=plan.target.project,
        executable_basename=os.path.basename(plan.executable),
        confirmation_mode="allow_write",
        message=message.encode("utf-8"),
    )


def _send_error_from_audit(error: AuditError) -> SendError:
    code = "unsafe_lock" if error.code == "unsafe_audit" else error.code
    return SendError(code)


def _replayed_send_result(
    plan: SendPlan,
    receipt: AuditReceipt,
) -> SendResult:
    return SendResult(
        agent=receipt.agent,
        session_id=plan.session_id,
        outcome=receipt.outcome,
        delivery=receipt.delivery,
        returncode=receipt.returncode,
        error_code=receipt.error,
        request_id=receipt.request_id,
        replayed=True,
    )


def _append_audit_failure_terminal(
    namespace: AuditNamespace,
    request_id: str,
    identity: AuditIdentity,
    error_code: str,
) -> None:
    try:
        namespace.append_terminal(
            request_id,
            identity,
            outcome="failed",
            delivery="unknown",
            error=error_code,
            returncode=None,
        )
    except BaseException:
        # The original control-flow exception is more important and must not
        # be masked. The retained pending record still prevents another spawn.
        pass


def execute_send(
    plan: SendPlan,
    *,
    allow_write: bool = False,
    timeout: float = DEFAULT_SEND_TIMEOUT_SECONDS,
    refresher: Optional[Callable[[], Iterable[Session]]] = None,
    executable_resolver: Callable[[str], Optional[str]] = shutil.which,
    runtime_dir: Optional[Union[str, os.PathLike]] = None,
    runner: Callable[..., BoundedProcessResult] = run_bounded,
    monotonic: Callable[[], float] = time.monotonic,
    request_id: Optional[str] = None,
) -> SendResult:
    """Reserve a request ID, refresh once, and run at most one native resume."""

    if allow_write is not True:
        raise SendError("write_not_allowed")
    bounded_timeout = _validate_timeout(timeout)
    planned, message = _preflight_plan(plan, filesystem=False)
    if not callable(refresher):
        raise SendError("revalidation_required")
    try:
        validated_request_id = validate_request_id(
            generate_request_id() if request_id is None else request_id
        )
        identity = _send_audit_identity(planned, message)
        audit_store = SendAuditStore(runtime_dir)
        with audit_store.open_reserved(
            validated_request_id,
            identity,
        ) as (audit_namespace, replay):
            if replay is not None:
                return _replayed_send_result(planned, replay)

            try:
                result = _run_native_send(
                    planned,
                    message,
                    bounded_timeout=bounded_timeout,
                    refresher=refresher,
                    executable_resolver=executable_resolver,
                    runtime_dir=runtime_dir,
                    runtime_namespace=audit_namespace,
                    runner=runner,
                    monotonic=monotonic,
                    request_id=validated_request_id,
                )
            except KeyboardInterrupt:
                _append_audit_failure_terminal(
                    audit_namespace,
                    validated_request_id,
                    identity,
                    "interrupted",
                )
                raise
            except AuditError as error:
                converted = _send_error_from_audit(error)
                _append_audit_failure_terminal(
                    audit_namespace,
                    validated_request_id,
                    identity,
                    converted.code,
                )
                raise converted from error
            except SendError as error:
                _append_audit_failure_terminal(
                    audit_namespace,
                    validated_request_id,
                    identity,
                    error.code,
                )
                raise
            except BaseException:
                _append_audit_failure_terminal(
                    audit_namespace,
                    validated_request_id,
                    identity,
                    "execution_error",
                )
                raise

            try:
                audit_namespace.append_terminal(
                    validated_request_id,
                    identity,
                    outcome=result.outcome,
                    delivery=result.delivery,
                    error=result.error_code,
                    returncode=result.returncode,
                )
            except Exception:
                return SendResult(
                    agent=result.agent,
                    session_id=result.session_id,
                    outcome="audit_error",
                    delivery="unknown",
                    returncode=result.returncode,
                    response=result.response,
                    stderr=result.stderr,
                    error_code="audit_error",
                    request_id=validated_request_id,
                )
            return result
    except AuditError as error:
        raise _send_error_from_audit(error) from error


__all__ = [
    "DEFAULT_SEND_TIMEOUT_SECONDS",
    "DELIVERY_STATES",
    "MAX_MESSAGE_BYTES",
    "MAX_SEND_TIMEOUT_SECONDS",
    "MAX_SESSION_ID_BYTES",
    "MAX_STDERR_BYTES",
    "MAX_STDOUT_BYTES",
    "RUNTIME_ENV",
    "SEND_OUTCOMES",
    "SEND_LOCK_DIRECTORY",
    "SUPPORTED_AGENTS",
    "SendError",
    "SendPlan",
    "SendResult",
    "SendTarget",
    "build_send_plan",
    "execute_send",
    "validate_message",
]
