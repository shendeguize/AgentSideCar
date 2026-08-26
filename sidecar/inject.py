"""Fail-closed local headless-resume planning and execution."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
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

from sidecar.json_limits import JSONLimitError, JSONLimits, JSONSyntaxError, parse_json
from sidecar.kimi_acp import (
    AcpPhase,
    KimiAcpRequest,
    KimiAcpResult,
    PromptWriteBoundary,
    run_kimi_acp,
)
from sidecar.kimi_identity import (
    FileGeneration,
    KimiIdentityError,
    KimiIdentityEvidence,
    capture_kimi_identity,
    revalidate_kimi_identity,
    revalidate_kimi_identity_after_kimi_start,
)
from sidecar.model import Session, Status
from sidecar.process import assert_no_live_kimi_in_project
from sidecar.process_runner import (
    BoundedDuplexLineProcess,
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
KIMI_SUPPORTED_VERSION = "0.38.0"
KIMI_VERSION_TIMEOUT_SECONDS = 30.0
KIMI_VERSION_STDOUT_BYTES = 256
KIMI_PROOF_BYTES = 16 * 1024 * 1024
KIMI_PROOF_LINE_BYTES = 4 * 1024 * 1024
KIMI_EXECUTABLE_BYTES = 32 * 1024 * 1024
KIMI_INTERPRETER_BYTES = 128 * 1024 * 1024
KIMI_RUNTIME_FILE_BYTES = 64 * 1024 * 1024
KIMI_RUNTIME_TOTAL_BYTES = 256 * 1024 * 1024
KIMI_RUNTIME_ASSET_COUNT = 2048
KIMI_DYLIB_COUNT = 256
KIMI_DYLIB_TOTAL_BYTES = 512 * 1024 * 1024

SUPPORTED_AGENTS = frozenset(("claude", "codex", "cursor-cli", "kimi"))
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
    "kimi": "kimi",
}
_PROMPT_TRANSPORTS = {
    "claude": "stdin",
    "codex": "stdin",
    "cursor-cli": "argv",
    "kimi": "ndjson",
}
_SEND_TRANSPORTS = {
    "claude": "argv-resume",
    "codex": "argv-resume",
    "cursor-cli": "argv-resume",
    "kimi": "kimi_acp",
}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_UNSUPPORTED_AGENT_CODES = {
    "cursor-ide": "unsupported_cursor_ide",
    "copilot": "unsupported_copilot",
    "dsh": "unsupported_dsh",
}
_ERROR_MESSAGES = {
    "invalid_message_type": "message must be text",
    "invalid_message_utf8": "message must contain valid Unicode scalars",
    "blank_message": "message must not be blank",
    "message_nul": "message must not contain NUL",
    "message_too_large": "message exceeds the size limit",
    "stdin_interactive": (
        "standard input is an interactive terminal; pipe or redirect the message"
    ),
    "stdin_unreadable": "standard input could not be read as bytes",
    "invalid_session": "session is invalid",
    "unsupported_agent": "session agent is unsupported",
    "unsupported_cursor_ide": "Cursor IDE sessions cannot be resumed by cursor-agent",
    "unsupported_copilot": "Copilot headless resume is unsupported",
    "unsupported_kimi": "Kimi ACP requires the verified supported version",
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
    "protocol_error": "native session protocol validation failed",
    "timeout": "native session operation timed out before prompt submission",
    "cancelled": "native session operation was cancelled before prompt submission",
    "containment_unsupported": "native process containment is unavailable",
    "cleanup_incomplete": "native process cleanup could not be verified",
    "spawn_error": "native session process could not be started",
    "native_exit": "native session process exited before prompt submission",
    "output_overflow": "native session protocol output exceeded limits",
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
class _KimiRuntimeAsset:
    relative_path: str
    identity: ExecutableIdentity = field(repr=False)
    mode: int


@dataclass(frozen=True, repr=False)
class _KimiRuntimeManifest:
    package_root: str
    package_assets: Tuple[_KimiRuntimeAsset, ...] = field(repr=False)
    node: Optional[_KimiRuntimeAsset] = field(default=None, repr=False)
    dylibs: Tuple[_KimiRuntimeAsset, ...] = field(default=(), repr=False)
    system_libraries: Tuple[str, ...] = field(default=(), repr=False)


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
    kimi_runtime: Optional[_KimiRuntimeManifest] = field(default=None, repr=False)
    kimi_identity: Optional[KimiIdentityEvidence] = field(
        default=None,
        repr=False,
    )
    kimi_session: Optional[Session] = field(
        default=None,
        compare=False,
        repr=False,
    )


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
    transport: str = "argv-resume"

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        if isinstance(self.input_data, bytearray):
            object.__setattr__(self, "input_data", bytes(self.input_data))

    def __repr__(self) -> str:
        return (
            "SendPlan(agent={!r}, session_id={!r}, executable={!r}, "
            "cwd={!r}, prompt_transport={!r}, transport={!r})"
        ).format(
            self.agent,
            self.session_id,
            self.executable,
            self.cwd,
            self.prompt_transport,
            self.transport,
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
    *,
    kimi_identity: Optional[KimiIdentityEvidence] = None,
    kimi_runtime: Optional[_KimiRuntimeManifest] = None,
) -> SendTarget:
    if kimi_identity is None:
        transcript = _transcript_path(session.transcript)
        paths = _source_paths(session, transcript)
        signature = tuple(
            _stat_source(path, required=index == 0)
            for index, path in enumerate(paths)
        )
        session_snapshot = None
    else:
        transcript = kimi_identity.root_wire.canonical_path
        signature = ()
        session_snapshot = Session(
            agent=session.agent,
            session_id=session.session_id,
            project=session.project,
            transcript=session.transcript,
            updated_at=session.updated_at,
            title=session.title,
            status=session.status,
            extra=dict(session.extra),
            parent_id=session.parent_id,
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
        kimi_runtime=kimi_runtime,
        kimi_identity=kimi_identity,
        kimi_session=session_snapshot,
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


def _send_error_from_kimi_identity(error: KimiIdentityError) -> SendError:
    code = error.code
    if code not in _ERROR_MESSAGES:
        code = "invalid_session"
    return SendError(code)


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
    if agent == "kimi":
        return (executable, "acp")
    raise SendError("unsupported_agent")


def _probe_kimi_version(
    executable: str,
    executable_identity: ExecutableIdentity,
    runner: Callable[..., BoundedProcessResult],
) -> None:
    try:
        before = _executable_identity(executable)
        if before != executable_identity:
            raise SendError("unsupported_kimi")
        result = runner(
            (executable, "--version"),
            None,
            input_limit=1,
            stdout_limit=KIMI_VERSION_STDOUT_BYTES,
            stderr_limit=KIMI_VERSION_STDOUT_BYTES,
            timeout=KIMI_VERSION_TIMEOUT_SECONDS,
            env=None,
            cwd=None,
            require_descendant_containment=False,
        )
        after = _executable_identity(executable)
        stdout = getattr(result, "stdout", b"")
        if isinstance(stdout, str):
            version = stdout.strip()
        elif isinstance(stdout, (bytes, bytearray)):
            version = bytes(stdout).decode("utf-8", "strict").strip()
        else:
            version = ""
        if (
            before != after
            or after != executable_identity
            or _result_returncode(result) != 0
            or getattr(result, "overflow", None) is not None
            or getattr(result, "cleanup_incomplete", False) is True
            or version != KIMI_SUPPORTED_VERSION
        ):
            raise SendError("unsupported_kimi")
    except SendError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise SendError("unsupported_kimi") from error


@dataclass(repr=False)
class _BoundKimiExecutable:
    manifest: _KimiRuntimeManifest = field(repr=False)
    executable: str
    _snapshot: tempfile.TemporaryDirectory = field(repr=False)

    def close(self) -> None:
        self._snapshot.cleanup()


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short snapshot write")
        offset += written


def _owner_safe_file_identity(path: str) -> ExecutableIdentity:
    try:
        with open(path, "rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        linked = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise SendError("unsupported_kimi") from error
    signature = (
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
    effective_uid = (
        os.geteuid() if callable(getattr(os, "geteuid", None)) else before.st_uid
    )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid not in (0, effective_uid)
        or bool(stat.S_IMODE(before.st_mode) & 0o022)
        or signature != after_signature
        or signature != linked_signature
    ):
        raise SendError("unsupported_kimi")
    return (path, *signature, digest.hexdigest())


def _runtime_asset(path: str, relative_path: str) -> _KimiRuntimeAsset:
    identity = _owner_safe_file_identity(path)
    if identity[4] < 0 or identity[4] > KIMI_RUNTIME_FILE_BYTES:
        raise SendError("unsupported_kimi")
    mode = 0o500 if stat.S_IMODE(identity[3]) & 0o111 else 0o400
    return _KimiRuntimeAsset(relative_path, identity, mode)


def _snapshot_runtime_asset_for_analysis(
    source: str,
    destination: Path,
    relative_path: str,
) -> _KimiRuntimeAsset:
    source_fd = -1
    destination_fd = -1
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise SendError("unsupported_kimi")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
        source_fd = os.open(source, flags)
        before = os.fstat(source_fd)
        effective_uid = (
            os.geteuid()
            if callable(getattr(os, "geteuid", None))
            else before.st_uid
        )
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in (0, effective_uid)
            or bool(mode & 0o022)
            or before.st_size < 0
            or before.st_size > KIMI_RUNTIME_FILE_BYTES
        ):
            raise SendError("unsupported_kimi")
        destination_fd = os.open(
            str(destination),
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | nofollow,
            0o400,
        )
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > KIMI_RUNTIME_FILE_BYTES:
                raise SendError("unsupported_kimi")
            digest.update(chunk)
            _write_all(destination_fd, chunk)
        os.fsync(destination_fd)
        os.fchmod(destination_fd, 0o400)
        snapshot = os.fstat(destination_fd)
        if (
            not stat.S_ISREG(snapshot.st_mode)
            or snapshot.st_uid != effective_uid
            or stat.S_IMODE(snapshot.st_mode) != 0o400
            or snapshot.st_size != copied
        ):
            raise SendError("unsupported_kimi")
        after = os.fstat(source_fd)
        linked = os.stat(source, follow_symlinks=False)
        signature = (
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
            copied != before.st_size
            or signature != after_signature
            or signature != linked_signature
        ):
            raise SendError("unsupported_kimi")
        identity = (source, *signature, digest.hexdigest())
        asset_mode = 0o500 if mode & 0o111 else 0o400
        return _KimiRuntimeAsset(relative_path, identity, asset_mode)
    except SendError:
        raise
    except OSError as error:
        raise SendError("unsupported_kimi") from error
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)


def _capture_package_assets(root: Path) -> Tuple[_KimiRuntimeAsset, ...]:
    try:
        canonical = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SendError("unsupported_kimi") from error
    assets: List[_KimiRuntimeAsset] = []
    total = 0
    stack = [canonical]
    effective_uid = os.geteuid() if callable(getattr(os, "geteuid", None)) else None
    while stack:
        directory = stack.pop()
        try:
            details = directory.lstat()
            entries = sorted(os.scandir(str(directory)), key=lambda item: item.name)
        except OSError as error:
            raise SendError("unsupported_kimi") from error
        if (
            not stat.S_ISDIR(details.st_mode)
            or (effective_uid is not None and details.st_uid not in (0, effective_uid))
            or bool(stat.S_IMODE(details.st_mode) & 0o022)
        ):
            raise SendError("unsupported_kimi")
        for entry in entries:
            try:
                item_details = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise SendError("unsupported_kimi") from error
            item = Path(entry.path)
            if stat.S_ISDIR(item_details.st_mode):
                stack.append(item)
                continue
            if not stat.S_ISREG(item_details.st_mode):
                raise SendError("unsupported_kimi")
            relative = item.relative_to(canonical).as_posix()
            if (
                not relative
                or relative.startswith("/")
                or any(part in ("", ".", "..") for part in relative.split("/"))
            ):
                raise SendError("unsupported_kimi")
            asset = _runtime_asset(str(item), relative)
            assets.append(asset)
            total += asset.identity[4]
            if (
                len(assets) > KIMI_RUNTIME_ASSET_COUNT
                or total > KIMI_RUNTIME_TOTAL_BYTES
            ):
                raise SendError("unsupported_kimi")
    return tuple(sorted(assets, key=lambda asset: asset.relative_path))


def _otool_dependencies(path: str) -> Tuple[str, ...]:
    try:
        result = run_bounded(
            ("/usr/bin/otool", "-L", path),
            None,
            input_limit=1,
            stdout_limit=256 * 1024,
            stderr_limit=64 * 1024,
            timeout=5.0,
            env=None,
            cwd=None,
            require_descendant_containment=False,
        )
        if _result_returncode(result) != 0:
            raise SendError("unsupported_kimi")
        payload = bytes(result.stdout).decode("utf-8", "strict")
    except SendError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise SendError("unsupported_kimi") from error
    dependencies = []
    for line in payload.splitlines()[1:]:
        value = line.strip()
        marker = value.find(" (")
        if marker <= 0:
            raise SendError("unsupported_kimi")
        dependencies.append(value[:marker])
    return tuple(dependencies)


def _system_library(path: str) -> bool:
    return path.startswith("/usr/lib/") or path.startswith("/System/Library/")


def _resolve_macho_dependency(loader: Path, value: str) -> str:
    if value.startswith("@loader_path/"):
        candidate = loader.parent / value[len("@loader_path/") :]
    elif value.startswith("@rpath/"):
        name = value[len("@rpath/") :]
        candidates = (loader.parent / name, loader.parent.parent / "lib" / name)
        existing = {
            str(candidate.resolve(strict=True))
            for candidate in candidates
            if candidate.is_file()
        }
        if len(existing) != 1:
            raise SendError("unsupported_kimi")
        candidate = Path(existing.pop())
    elif value.startswith("/"):
        candidate = Path(value)
    else:
        raise SendError("unsupported_kimi")
    try:
        return str(candidate.resolve(strict=True))
    except (OSError, RuntimeError) as error:
        raise SendError("unsupported_kimi") from error


def _capture_macho_closure(
    node: str,
) -> Tuple[_KimiRuntimeAsset, Tuple[_KimiRuntimeAsset, ...], Tuple[str, ...]]:
    if sys.platform != "darwin":
        return _runtime_asset(node, "bin/node"), (), ()
    try:
        analysis_root = Path(tempfile.mkdtemp(prefix="agent-sidecar-kimi-otool-"))
    except OSError as error:
        raise SendError("unsupported_kimi") from error
    try:
        try:
            analysis_root.chmod(0o700)
            details = analysis_root.stat()
        except OSError as error:
            raise SendError("unsupported_kimi") from error
        effective_uid = (
            os.geteuid()
            if callable(getattr(os, "geteuid", None))
            else details.st_uid
        )
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != effective_uid
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise SendError("unsupported_kimi")
        sequence = 0

        def capture(
            source: str,
            relative_path: str,
        ) -> Tuple[_KimiRuntimeAsset, Tuple[str, ...]]:
            nonlocal sequence
            analysis_path = analysis_root / "{:04d}.image".format(sequence)
            sequence += 1
            asset = _snapshot_runtime_asset_for_analysis(
                source,
                analysis_path,
                relative_path,
            )
            return asset, _otool_dependencies(str(analysis_path))

        node_asset, node_dependencies = capture(node, "bin/node")
        pending = [(node, node_dependencies)]
        captured: Dict[str, Tuple[_KimiRuntimeAsset, Tuple[str, ...]]] = {
            node: (node_asset, node_dependencies)
        }
        libraries: Dict[str, _KimiRuntimeAsset] = {}
        systems = set()
        total = 0
        while pending:
            loader_path, dependencies = pending.pop()
            loader = Path(loader_path)
            for raw_dependency in dependencies:
                if _system_library(raw_dependency):
                    systems.add(raw_dependency)
                    continue
                dependency = _resolve_macho_dependency(loader, raw_dependency)
                if _system_library(dependency):
                    systems.add(dependency)
                    continue
                name = Path(raw_dependency).name
                captured_dependency = captured.get(dependency)
                if captured_dependency is None:
                    base_asset, dependency_children = capture(
                        dependency,
                        "lib/{}".format(name),
                    )
                    captured_dependency = (base_asset, dependency_children)
                    captured[dependency] = captured_dependency
                    pending.append((dependency, dependency_children))
                base_asset = captured_dependency[0]
                asset = replace(
                    base_asset,
                    relative_path="lib/{}".format(name),
                )
                existing = libraries.get(name)
                if existing is not None and existing.identity != asset.identity:
                    raise SendError("unsupported_kimi")
                if existing is None:
                    libraries[name] = asset
                    total += asset.identity[4]
                    if (
                        len(libraries) > KIMI_DYLIB_COUNT
                        or total > KIMI_DYLIB_TOTAL_BYTES
                    ):
                        raise SendError("unsupported_kimi")
        return (
            node_asset,
            tuple(libraries[name] for name in sorted(libraries)),
            tuple(sorted(systems)),
        )
    finally:
        try:
            shutil.rmtree(str(analysis_root))
        except OSError as error:
            raise SendError("unsupported_kimi") from error
        if analysis_root.exists():
            raise SendError("unsupported_kimi")


def _capture_kimi_runtime_manifest(
    executable: str,
    executable_identity: ExecutableIdentity,
) -> _KimiRuntimeManifest:
    try:
        with open(executable, "rb") as stream:
            shebang = stream.readline(256).rstrip(b"\r\n")
    except OSError as error:
        raise SendError("unsupported_kimi") from error
    if shebang != b"#!/usr/bin/env node":
        asset = _KimiRuntimeAsset("kimi", executable_identity, 0o500)
        return _KimiRuntimeManifest(
            package_root=str(Path(executable).parent),
            package_assets=(asset,),
        )
    main = Path(executable)
    if main.name != "main.mjs" or main.parent.name != "dist":
        raise SendError("unsupported_kimi")
    package_root = main.parent.parent.resolve(strict=True)
    package_assets = _capture_package_assets(package_root)
    relative_assets = {asset.relative_path for asset in package_assets}
    if not {
        "package.json",
        "dist/main.mjs",
        "dist/search-worker.mjs",
    }.issubset(relative_assets):
        raise SendError("unsupported_kimi")
    main_assets = [
        asset for asset in package_assets if asset.relative_path == "dist/main.mjs"
    ]
    if len(main_assets) != 1 or main_assets[0].identity != executable_identity:
        raise SendError("unsupported_kimi")
    resolved_node = shutil.which("node")
    if not isinstance(resolved_node, str):
        raise SendError("unsupported_kimi")
    node = str(Path(resolved_node).resolve(strict=True))
    node_asset, dylibs, systems = _capture_macho_closure(node)
    return _KimiRuntimeManifest(
        package_root=str(package_root),
        package_assets=package_assets,
        node=node_asset,
        dylibs=dylibs,
        system_libraries=systems,
    )


def _validate_runtime_manifest(manifest: _KimiRuntimeManifest) -> None:
    if not isinstance(manifest, _KimiRuntimeManifest):
        raise SendError("unsupported_kimi")
    assets = manifest.package_assets
    if (
        not assets
        or len(assets) > KIMI_RUNTIME_ASSET_COUNT
        or len(manifest.dylibs) > KIMI_DYLIB_COUNT
        or tuple(sorted(set(manifest.system_libraries)))
        != manifest.system_libraries
        or any(not _system_library(path) for path in manifest.system_libraries)
    ):
        raise SendError("unsupported_kimi")
    all_assets = list(assets)
    if manifest.node is not None:
        all_assets.append(manifest.node)
    all_assets.extend(manifest.dylibs)
    seen = set()
    package_total = 0
    dylib_total = 0
    for asset in all_assets:
        if (
            not isinstance(asset, _KimiRuntimeAsset)
            or asset.relative_path in seen
            or asset.mode not in (0o400, 0o500)
            or not asset.relative_path
            or asset.relative_path.startswith("/")
            or any(
                part in ("", ".", "..") for part in asset.relative_path.split("/")
            )
            or _owner_safe_file_identity(asset.identity[0]) != asset.identity
        ):
            raise SendError("session_changed")
        seen.add(asset.relative_path)
    for asset in assets:
        try:
            within_package = (
                os.path.commonpath((manifest.package_root, asset.identity[0]))
                == manifest.package_root
            )
        except (TypeError, ValueError):
            within_package = False
        if not within_package:
            raise SendError("unsupported_kimi")
        package_total += asset.identity[4]
    for asset in manifest.dylibs:
        if (
            not asset.relative_path.startswith("lib/")
            or "/" in asset.relative_path[len("lib/") :]
        ):
            raise SendError("unsupported_kimi")
        dylib_total += asset.identity[4]
    if (
        package_total > KIMI_RUNTIME_TOTAL_BYTES
        or dylib_total > KIMI_DYLIB_TOTAL_BYTES
        or (
            manifest.node is not None
            and manifest.node.relative_path != "bin/node"
        )
    ):
        raise SendError("unsupported_kimi")


def _snapshot_verified_file(
    source: str,
    destination: Path,
    *,
    expected: Optional[ExecutableIdentity],
    limit: int,
    mode: int,
) -> ExecutableIdentity:
    source_fd = -1
    destination_fd = -1
    try:
        if expected is None:
            expected = _executable_identity(source)
        elif expected[0] != source:
            raise SendError("unsupported_kimi")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise SendError("unsupported_kimi")
        source_fd = os.open(source, flags | nofollow)
        before = os.fstat(source_fd)
        effective_uid = (
            os.geteuid() if callable(getattr(os, "geteuid", None)) else before.st_uid
        )
        source_mode = stat.S_IMODE(before.st_mode)
        signature = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_mode),
            int(before.st_size),
            int(getattr(before, "st_mtime_ns", int(before.st_mtime * 1e9))),
            int(getattr(before, "st_ctime_ns", int(before.st_ctime * 1e9))),
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in (0, effective_uid)
            or bool(source_mode & 0o022)
            or before.st_size < 0
            or before.st_size > limit
            or signature != expected[1:7]
        ):
            raise SendError("unsupported_kimi")
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
        destination_flags |= getattr(os, "O_CLOEXEC", 0)
        destination_fd = os.open(str(destination), destination_flags, 0o600)
        digest = hashlib.sha256()
        remaining = int(before.st_size)
        while remaining:
            chunk = os.read(source_fd, min(1024 * 1024, remaining))
            if not chunk:
                raise SendError("unsupported_kimi")
            digest.update(chunk)
            _write_all(destination_fd, chunk)
            remaining -= len(chunk)
        if os.read(source_fd, 1):
            raise SendError("unsupported_kimi")
        after = os.fstat(source_fd)
        linked = os.stat(source, follow_symlinks=False)
        if (
            (int(after.st_dev), int(after.st_ino))
            != (int(before.st_dev), int(before.st_ino))
            or (
                int(after.st_mode),
                int(after.st_size),
                int(getattr(after, "st_mtime_ns", int(after.st_mtime * 1e9))),
                int(getattr(after, "st_ctime_ns", int(after.st_ctime * 1e9))),
            )
            != signature[2:]
            or (int(linked.st_dev), int(linked.st_ino))
            != (int(before.st_dev), int(before.st_ino))
            or digest.hexdigest() != expected[7]
        ):
            raise SendError("unsupported_kimi")
        os.fsync(destination_fd)
        os.fchmod(destination_fd, mode)
        os.close(destination_fd)
        destination_fd = -1
        copied = _owner_safe_file_identity(str(destination))
        details = destination.lstat()
        if (
            details.st_uid != effective_uid
            or stat.S_IMODE(details.st_mode) != mode
            or copied[3] != int(before.st_mode) - source_mode + mode
            or copied[4] != int(before.st_size)
            or copied[7] != expected[7]
        ):
            raise SendError("unsupported_kimi")
        return copied
    except SendError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise SendError("unsupported_kimi") from error
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)


def _bind_kimi_executable(
    manifest: _KimiRuntimeManifest,
) -> _BoundKimiExecutable:
    snapshot: Optional[tempfile.TemporaryDirectory] = None
    try:
        snapshot = tempfile.TemporaryDirectory(prefix="agent-sidecar-kimi-")
        os.chmod(snapshot.name, 0o700)
        root = Path(snapshot.name)
        details = root.lstat()
        effective_uid = (
            os.geteuid() if callable(getattr(os, "geteuid", None)) else details.st_uid
        )
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != effective_uid
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise SendError("unsupported_kimi")
        _validate_runtime_manifest(manifest)
        package_snapshot = root / "package"
        for asset in manifest.package_assets:
            destination = package_snapshot / asset.relative_path
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _snapshot_verified_file(
                asset.identity[0],
                destination,
                expected=asset.identity,
                limit=KIMI_RUNTIME_FILE_BYTES,
                mode=asset.mode,
            )
        if manifest.node is not None:
            node_path = root / manifest.node.relative_path
            node_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _snapshot_verified_file(
                manifest.node.identity[0],
                node_path,
                expected=manifest.node.identity,
                limit=KIMI_INTERPRETER_BYTES,
                mode=0o500,
            )
            for asset in manifest.dylibs:
                destination = root / asset.relative_path
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _snapshot_verified_file(
                    asset.identity[0],
                    destination,
                    expected=asset.identity,
                    limit=KIMI_INTERPRETER_BYTES,
                    mode=0o500,
                )
            launch_path = root / "kimi"
            launcher = (
                "#!/bin/sh\n"
                "unset NODE_OPTIONS NODE_PATH DYLD_INSERT_LIBRARIES "
                "DYLD_FRAMEWORK_PATH DYLD_FALLBACK_FRAMEWORK_PATH "
                "DYLD_FALLBACK_LIBRARY_PATH\n"
                "DYLD_LIBRARY_PATH={} exec {} {} \"$@\"\n".format(
                    shlex.quote(str(root / "lib")),
                    shlex.quote(str(node_path)),
                    shlex.quote(str(package_snapshot / "dist" / "main.mjs")),
                )
            ).encode("utf-8")
            descriptor = os.open(
                str(launch_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o500,
            )
            try:
                _write_all(descriptor, launcher)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            launch_path = root / "kimi"
            source = package_snapshot / manifest.package_assets[0].relative_path
            os.replace(str(source), str(launch_path))
        launch_details = launch_path.lstat()
        if (
            not stat.S_ISREG(launch_details.st_mode)
            or launch_details.st_uid != effective_uid
            or stat.S_IMODE(launch_details.st_mode) != 0o500
        ):
            raise SendError("unsupported_kimi")
        bound = _BoundKimiExecutable(
            manifest=manifest,
            executable=str(launch_path),
            _snapshot=snapshot,
        )
        snapshot = None
        return bound
    except SendError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise SendError("unsupported_kimi") from error
    finally:
        if snapshot is not None:
            snapshot.cleanup()


def _probe_bound_kimi_version(
    bound: _BoundKimiExecutable,
    runner: Callable[..., BoundedProcessResult],
) -> None:
    try:
        result = runner(
            (bound.executable, "--version"),
            None,
            input_limit=1,
            stdout_limit=KIMI_VERSION_STDOUT_BYTES,
            stderr_limit=KIMI_VERSION_STDOUT_BYTES,
            timeout=KIMI_VERSION_TIMEOUT_SECONDS,
            env=None,
            cwd=None,
            require_descendant_containment=False,
        )
        stdout = getattr(result, "stdout", b"")
        version = (
            stdout.strip()
            if isinstance(stdout, str)
            else bytes(stdout).decode("utf-8", "strict").strip()
            if isinstance(stdout, (bytes, bytearray))
            else ""
        )
        if (
            _result_returncode(result) != 0
            or getattr(result, "overflow", None) is not None
            or getattr(result, "cleanup_incomplete", False) is True
            or version != KIMI_SUPPORTED_VERSION
        ):
            raise SendError("unsupported_kimi")
    except SendError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise SendError("unsupported_kimi") from error


def _probe_bound_kimi_initialize(bound: _BoundKimiExecutable) -> None:
    if bound.manifest.node is None:
        return
    process: Optional[BoundedDuplexLineProcess] = None
    try:
        process = BoundedDuplexLineProcess(
            (bound.executable, "acp"),
            line_limit=256 * 1024,
            stdout_limit=1024 * 1024,
            stderr_limit=64 * 1024,
            require_descendant_containment=False,
        )
        deadline = time.monotonic() + 30.0
        frame = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {
                        "name": "agent-sidecar",
                        "version": "runtime-probe",
                    },
                },
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        process.write_line(frame, deadline=deadline)
        response = parse_json(process.read_line(deadline=deadline), _KIMI_JSON_LIMITS)
        result = response.get("result") if isinstance(response, Mapping) else None
        capabilities = (
            result.get("agentCapabilities") if isinstance(result, Mapping) else None
        )
        sessions = (
            capabilities.get("sessionCapabilities")
            if isinstance(capabilities, Mapping)
            else None
        )
        if (
            response.get("id") != 1
            or result.get("protocolVersion") != 1
            or capabilities.get("loadSession") is not True
            or not isinstance(sessions, Mapping)
            or not isinstance(sessions.get("list"), Mapping)
            or not isinstance(sessions.get("resume"), Mapping)
        ):
            raise SendError("unsupported_kimi")
    except SendError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise SendError("unsupported_kimi") from error
    finally:
        if process is not None:
            try:
                observed = process.terminate_tree(deadline=time.monotonic() + 5.0)
                if not observed.cleanup_complete:
                    raise SendError("unsupported_kimi")
            finally:
                process.close()


def build_send_plan(
    session: Session,
    message: str,
    executable_resolver: Callable[[str], Optional[str]] = shutil.which,
    *,
    version_runner: Callable[..., BoundedProcessResult] = run_bounded,
    _kimi_runtime: Optional[_KimiRuntimeManifest] = None,
) -> SendPlan:
    """Build a fixed local resume invocation after all filesystem preflight."""

    message_bytes = validate_message(message)
    _session, agent, session_id, project = _validate_session(session)
    kimi_identity: Optional[KimiIdentityEvidence] = None
    kimi_runtime: Optional[_KimiRuntimeManifest] = None
    try:
        if agent == "kimi":
            try:
                kimi_identity = capture_kimi_identity(_session)
            except KimiIdentityError as error:
                raise _send_error_from_kimi_identity(error) from error
        executable = _resolve_executable(
            _EXECUTABLE_NAMES[agent],
            executable_resolver,
        )
        if agent == "kimi":
            if _kimi_runtime is None:
                executable_identity = _executable_identity(executable)
                kimi_runtime = _capture_kimi_runtime_manifest(
                    executable,
                    executable_identity,
                )
            else:
                kimi_runtime = _kimi_runtime
                _validate_runtime_manifest(kimi_runtime)
        target = _send_target(
            _session,
            agent,
            session_id,
            project,
            executable,
            kimi_identity=kimi_identity,
            kimi_runtime=kimi_runtime,
        )
        if agent == "kimi" and _kimi_runtime is None:
            _probe_kimi_version(
                executable,
                target.executable_identity,
                version_runner,
            )
    except BaseException:
        if kimi_identity is not None:
            kimi_identity.close()
        raise
    transport = _PROMPT_TRANSPORTS[agent]
    return SendPlan(
        agent=agent,
        session_id=session_id,
        executable=executable,
        argv=_send_arguments(agent, executable, session_id, message),
        cwd=project,
        target=target,
        input_data=message_bytes if transport in ("stdin", "ndjson") else None,
        prompt_transport=transport,
        transport=_SEND_TRANSPORTS[agent],
    )


def _plan_message(plan: SendPlan) -> str:
    if plan.agent in ("claude", "codex", "kimi"):
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
    if plan.transport != _SEND_TRANSPORTS[plan.agent]:
        raise SendError("invalid_plan")
    if plan.agent == "kimi":
        evidence = plan.target.kimi_identity
        snapshot = plan.target.kimi_session
        runtime_manifest = plan.target.kimi_runtime
        executable_assets = (
            ()
            if not isinstance(runtime_manifest, _KimiRuntimeManifest)
            else tuple(
                asset
                for asset in runtime_manifest.package_assets
                if asset.identity[0] == plan.executable
            )
        )
        if (
            not isinstance(evidence, KimiIdentityEvidence)
            or not isinstance(snapshot, Session)
            or not isinstance(runtime_manifest, _KimiRuntimeManifest)
            or len(executable_assets) != 1
            or executable_assets[0].identity != plan.target.executable_identity
            or evidence.native_root_id != session_id
            or evidence.agent_id != "main"
            or not evidence.native_root
            or not evidence.ids_agree
            or not evidence.projects_agree
            or evidence.root_wire.canonical_path != plan.target.transcript
            or (evidence.project.dev, evidence.project.ino)
            != (
                plan.target.project_identity[1],
                plan.target.project_identity[2],
            )
            or plan.target.source_signature
        ):
            raise SendError("invalid_plan")
    elif plan.target.kimi_runtime is not None:
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
        if plan.agent == "kimi":
            evidence = plan.target.kimi_identity
            snapshot = plan.target.kimi_session
            assert isinstance(evidence, KimiIdentityEvidence)
            assert isinstance(snapshot, Session)
            if evidence.closed:
                raise SendError("session_changed")
            try:
                fresh = revalidate_kimi_identity(evidence, snapshot)
            except KimiIdentityError as error:
                raise SendError("session_changed") from error
            else:
                fresh.close()
            _validate_runtime_manifest(plan.target.kimi_runtime)
        else:
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
    version_runner: Callable[..., BoundedProcessResult] = run_bounded,
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
        version_runner=version_runner,
        _kimi_runtime=(
            planned.target.kimi_runtime if planned.agent == "kimi" else None
        ),
    )
    if final_plan.target != planned.target:
        if final_plan.target.kimi_identity is not None:
            final_plan.target.kimi_identity.close()
        raise SendError("session_changed")
    try:
        validated, _message = _preflight_plan(final_plan)
    except BaseException:
        if final_plan.target.kimi_identity is not None:
            final_plan.target.kimi_identity.close()
        raise
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


_KIMI_JSON_LIMITS = JSONLimits(
    max_bytes=KIMI_PROOF_LINE_BYTES,
    max_depth=32,
    max_items=8192,
    max_nodes=16384,
    max_string_bytes=KIMI_PROOF_LINE_BYTES,
    max_integer_bits=256,
)


@dataclass(frozen=True, repr=False)
class _KimiExecutableProcessIdentity:
    canonical_path: str
    dev: int
    ino: int


@dataclass(frozen=True, repr=False)
class _KimiCompletionBoundary:
    evidence: KimiIdentityEvidence = field(repr=False)
    session: Session = field(repr=False)
    wire_offset: int
    previous_main_turn_id: int
    state_generation: FileGeneration = field(repr=False)
    state_identity_digest: str = field(repr=False)
    state_reason: Optional[str] = field(repr=False)


def _kimi_process_identity(plan: SendPlan) -> _KimiExecutableProcessIdentity:
    identity = plan.target.executable_identity
    return _KimiExecutableProcessIdentity(
        canonical_path=identity[0],
        dev=identity[1],
        ino=identity[2],
    )


def _guard_kimi_processes(
    plan: SendPlan,
    evidence: KimiIdentityEvidence,
    guard: Callable[..., None],
    *,
    excluded_process_group: Optional[int] = None,
) -> None:
    try:
        guard(
            evidence.project,
            _kimi_process_identity(plan),
            excluded_process_group=excluded_process_group,
        )
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, SendError):
            raise
        raise SendError("session_busy") from error


def _read_kimi_file(descriptor: int, size: int, limit: int) -> bytes:
    if (
        type(descriptor) is not int
        or descriptor < 0
        or type(size) is not int
        or size < 0
        or size > limit
    ):
        raise SendError("session_changed")
    chunks: List[bytes] = []
    offset = 0
    try:
        while offset < size:
            chunk = os.pread(descriptor, min(size - offset, 1024 * 1024), offset)
            if not chunk:
                raise SendError("session_changed")
            chunks.append(chunk)
            offset += len(chunk)
    except SendError:
        raise
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise SendError("session_changed") from error
    payload = b"".join(chunks)
    if len(payload) != size:
        raise SendError("session_changed")
    return payload


def _parse_kimi_jsonl(payload: bytes) -> Tuple[Mapping[str, Any], ...]:
    if payload and not payload.endswith(b"\n"):
        raise SendError("protocol_error")
    records: List[Mapping[str, Any]] = []
    for line in payload.splitlines():
        if not line or len(line) > KIMI_PROOF_LINE_BYTES:
            raise SendError("protocol_error")
        try:
            value = parse_json(line, _KIMI_JSON_LIMITS)
        except (JSONLimitError, JSONSyntaxError) as error:
            raise SendError("protocol_error") from error
        if not isinstance(value, Mapping):
            raise SendError("protocol_error")
        records.append(value)
    return tuple(records)


def _previous_main_turn_id(records: Sequence[Mapping[str, Any]]) -> int:
    maximum = -1
    for record in records:
        if record.get("type") != "turn.ended" or record.get("agentId") != "main":
            continue
        turn_id = record.get("turnId")
        if type(turn_id) is not int or turn_id < 0:
            raise SendError("protocol_error")
        maximum = max(maximum, turn_id)
    return maximum


def _capture_kimi_boundary(
    evidence: KimiIdentityEvidence,
    session: Session,
) -> _KimiCompletionBoundary:
    size = evidence.root_wire_generation.size
    payload = _read_kimi_file(
        evidence._anchors.descriptor("wire"),
        size,
        KIMI_PROOF_BYTES,
    )
    records = _parse_kimi_jsonl(payload)
    return _KimiCompletionBoundary(
        evidence=evidence,
        session=session,
        wire_offset=size,
        previous_main_turn_id=_previous_main_turn_id(records),
        state_generation=evidence.state_generation,
        state_identity_digest=evidence.state_identity_digest,
        state_reason=_kimi_state_reason(evidence),
    )


def _kimi_prompt_matches(record: Mapping[str, Any], message: str) -> bool:
    if record.get("type") != "turn.prompt" or record.get("agentId") != "main":
        return False
    blocks = record.get("input")
    if not isinstance(blocks, list) or len(blocks) != 1:
        return False
    block = blocks[0]
    return (
        isinstance(block, Mapping)
        and block.get("type") == "text"
        and block.get("text") == message
    )


def _kimi_suffix_records(
    evidence: KimiIdentityEvidence,
    boundary: _KimiCompletionBoundary,
) -> Tuple[Mapping[str, Any], ...]:
    size = evidence.root_wire_generation.size
    if size < boundary.wire_offset or size - boundary.wire_offset > KIMI_PROOF_BYTES:
        raise SendError("protocol_error")
    descriptor = evidence._anchors.descriptor("wire")
    try:
        prefix = os.pread(descriptor, boundary.wire_offset, 0)
        suffix = os.pread(
            descriptor,
            size - boundary.wire_offset,
            boundary.wire_offset,
        )
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise SendError("protocol_error") from error
    original_prefix = _read_kimi_file(
        boundary.evidence._anchors.descriptor("wire"),
        boundary.wire_offset,
        KIMI_PROOF_BYTES,
    )
    if len(prefix) != boundary.wire_offset or prefix != original_prefix:
        raise SendError("protocol_error")
    if len(suffix) != size - boundary.wire_offset:
        raise SendError("protocol_error")
    return _parse_kimi_jsonl(suffix)


def _kimi_state_reason(evidence: KimiIdentityEvidence) -> Optional[str]:
    try:
        details = os.fstat(evidence._anchors.descriptor("state"))
        if (
            details.st_size < 0
            or details.st_size > 512 * 1024
            or int(details.st_size) != evidence.state_generation.size
        ):
            return None
        payload = _read_kimi_file(
            evidence._anchors.descriptor("state"),
            int(details.st_size),
            512 * 1024,
        )
        value = parse_json(payload, _KIMI_JSON_LIMITS)
    except (
        JSONLimitError,
        JSONSyntaxError,
        KimiIdentityError,
        SendError,
        OSError,
    ):
        return None
    if not isinstance(value, Mapping):
        return None
    reason = value.get("lastTurnReason")
    return reason if isinstance(reason, str) else None


def _kimi_state_completed(
    evidence: KimiIdentityEvidence,
    boundary: _KimiCompletionBoundary,
) -> bool:
    return (
        evidence.state_identity_digest == boundary.state_identity_digest
        and evidence.state_generation != boundary.state_generation
        and _kimi_state_reason(evidence) == "completed"
    )


def _kimi_durable_completed(
    records: Sequence[Mapping[str, Any]],
    message: str,
    previous_main_turn_id: int,
) -> bool:
    main_prompts = [
        index
        for index, record in enumerate(records)
        if record.get("type") == "turn.prompt" and record.get("agentId") == "main"
    ]
    if len(main_prompts) != 1:
        return False
    prompt_index = main_prompts[0]
    if not _kimi_prompt_matches(records[prompt_index], message):
        return False
    ended: Optional[Mapping[str, Any]] = None
    for record in records[prompt_index + 1 :]:
        if record.get("agentId") != "main":
            continue
        if record.get("type") in ("turn.prompt", "turn.steer"):
            return False
        if record.get("type") == "turn.ended":
            ended = record
            break
    if ended is None:
        return False
    turn_id = ended.get("turnId")
    return (
        type(turn_id) is int
        and turn_id > previous_main_turn_id
        and ended.get("reason") == "completed"
    )


def _close_kimi_plan(plan: SendPlan) -> None:
    evidence = plan.target.kimi_identity
    if isinstance(evidence, KimiIdentityEvidence):
        evidence.close()


def _run_kimi_send(
    planned: SendPlan,
    message: str,
    *,
    bound_executable: _BoundKimiExecutable,
    bounded_timeout: float,
    refresher: Callable[[], Iterable[Session]],
    executable_resolver: Callable[[str], Optional[str]],
    runtime_dir: Optional[Union[str, os.PathLike]],
    runtime_namespace: AuditNamespace,
    monotonic: Callable[[], float],
    request_id: str,
    version_runner: Callable[..., BoundedProcessResult],
    kimi_runner: Callable[..., KimiAcpResult],
    process_guard: Callable[..., None],
) -> SendResult:
    validated_plan: Optional[SendPlan] = None
    current_evidence: Optional[KimiIdentityEvidence] = None
    boundary: Optional[_KimiCompletionBoundary] = None
    with _session_lock(
        planned.agent,
        planned.session_id,
        runtime_dir,
        runtime_namespace,
    ) as validate_lock_namespace:
        try:
            validated_plan = _refreshed_send_plan(
                planned,
                message,
                refresher,
                executable_resolver,
                version_runner,
            )
            evidence = validated_plan.target.kimi_identity
            session = validated_plan.target.kimi_session
            if not isinstance(evidence, KimiIdentityEvidence) or not isinstance(
                session,
                Session,
            ):
                raise SendError("invalid_plan")
            current_evidence = evidence
            validate_lock_namespace()
            if (
                bound_executable.manifest
                != validated_plan.target.kimi_runtime
            ):
                raise SendError("unsupported_kimi")
            _probe_bound_kimi_version(bound_executable, version_runner)
            _guard_kimi_processes(
                validated_plan,
                current_evidence,
                process_guard,
            )

            def before_prompt(process_identity: object) -> None:
                nonlocal current_evidence, boundary
                validate_lock_namespace()
                _validate_runtime_manifest(bound_executable.manifest)
                _probe_bound_kimi_version(bound_executable, version_runner)
                try:
                    fresh = revalidate_kimi_identity_after_kimi_start(
                        current_evidence,
                        session,
                    )
                except KimiIdentityError as error:
                    raise SendError("session_changed") from error
                current_evidence.close()
                current_evidence = fresh
                process_group_id = getattr(process_identity, "process_group_id", None)
                _guard_kimi_processes(
                    validated_plan,
                    current_evidence,
                    process_guard,
                    excluded_process_group=process_group_id,
                )
                boundary = _capture_kimi_boundary(current_evidence, session)

            def validate_session_cwd(actual: str) -> bool:
                if not isinstance(actual, str) or not actual or "\x00" in actual:
                    return False
                try:
                    details = os.stat(actual)
                except (OSError, TypeError, ValueError):
                    return False
                return (
                    stat.S_ISDIR(details.st_mode)
                    and (int(details.st_dev), int(details.st_ino))
                    == (
                        current_evidence.project.dev,
                        current_evidence.project.ino,
                    )
                )

            acp_result = kimi_runner(
                KimiAcpRequest(
                    executable=bound_executable.executable,
                    cwd=validated_plan.cwd,
                    session_id=validated_plan.session_id,
                    request_id=request_id,
                    message=validated_plan.input_data or b"",
                    deadline=monotonic() + bounded_timeout,
                ),
                before_prompt=before_prompt,
                validate_session_cwd=validate_session_cwd,
                monotonic=monotonic,
            )
            response = _bounded_result_text(
                redact_message(acp_result.response_text, message),
                MAX_STDOUT_BYTES,
            )
            if acp_result.prompt_write is PromptWriteBoundary.NONE:
                raise SendError(acp_result.error_code or "protocol_error")
            error_code = acp_result.error_code
            delivered = False
            if (
                acp_result.prompt_write is PromptWriteBoundary.COMPLETE
                and acp_result.phase is AcpPhase.PROMPT_SETTLED
                and acp_result.stop_reason == "end_turn"
                and acp_result.returncode == 0
                and (
                    error_code == "cleanup_incomplete"
                    or (
                        error_code is None
                        and acp_result.clean_exit
                        and acp_result.cleanup_complete
                    )
                )
                and boundary is not None
            ):
                fresh = None
                try:
                    fresh = revalidate_kimi_identity_after_kimi_start(
                        current_evidence,
                        session,
                    )
                    records = _kimi_suffix_records(fresh, boundary)
                    delivered = _kimi_durable_completed(
                        records,
                        message,
                        boundary.previous_main_turn_id,
                    ) and _kimi_state_completed(fresh, boundary)
                except BaseException as error:
                    if fresh is not None:
                        fresh.close()
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        raise
                    delivered = False
                else:
                    current_evidence.close()
                    current_evidence = fresh
            if delivered:
                return SendResult(
                    agent=validated_plan.agent,
                    session_id=validated_plan.session_id,
                    outcome="completed",
                    delivery="unknown",
                    returncode=0,
                    response=response,
                    request_id=request_id,
                )
            if not acp_result.cleanup_complete:
                error_code = "cleanup_incomplete"
            elif error_code is None:
                error_code = "protocol_error"
            outcome = (
                "timed_out"
                if error_code == "timeout"
                else "overflow"
                if error_code == "output_overflow"
                else "failed"
            )
            return SendResult(
                agent=validated_plan.agent,
                session_id=validated_plan.session_id,
                outcome=outcome,
                delivery="unknown",
                returncode=acp_result.returncode,
                response=response,
                error_code=error_code,
                request_id=request_id,
            )
        finally:
            if current_evidence is not None:
                current_evidence.close()
            if validated_plan is not None:
                _close_kimi_plan(validated_plan)


def _run_native_send(
    planned: SendPlan,
    message: str,
    *,
    bound_kimi_executable: Optional[_BoundKimiExecutable],
    bounded_timeout: float,
    refresher: Callable[[], Iterable[Session]],
    executable_resolver: Callable[[str], Optional[str]],
    runtime_dir: Optional[Union[str, os.PathLike]],
    runtime_namespace: AuditNamespace,
    runner: Callable[..., BoundedProcessResult],
    monotonic: Callable[[], float],
    request_id: str,
    version_runner: Callable[..., BoundedProcessResult],
    kimi_runner: Callable[..., KimiAcpResult],
    process_guard: Callable[..., None],
) -> SendResult:
    if planned.transport == "kimi_acp":
        if not isinstance(bound_kimi_executable, _BoundKimiExecutable):
            raise SendError("unsupported_kimi")
        return _run_kimi_send(
            planned,
            message,
            bound_executable=bound_kimi_executable,
            bounded_timeout=bounded_timeout,
            refresher=refresher,
            executable_resolver=executable_resolver,
            runtime_dir=runtime_dir,
            runtime_namespace=runtime_namespace,
            monotonic=monotonic,
            request_id=request_id,
            version_runner=version_runner,
            kimi_runner=kimi_runner,
            process_guard=process_guard,
        )
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
    outcome = receipt.outcome
    if (
        plan.agent == "kimi"
        and receipt.outcome == "failed"
        and receipt.delivery == "unknown"
        and receipt.returncode == 0
        and receipt.error is None
    ):
        outcome = "completed"
    return SendResult(
        agent=receipt.agent,
        session_id=plan.session_id,
        outcome=outcome,
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
    version_runner: Callable[..., BoundedProcessResult] = run_bounded,
    kimi_runner: Callable[..., KimiAcpResult] = run_kimi_acp,
    process_guard: Callable[..., None] = assert_no_live_kimi_in_project,
) -> SendResult:
    """Reserve a request ID, refresh once, and run at most one native resume."""

    if allow_write is not True:
        raise SendError("write_not_allowed")
    bounded_timeout = _validate_timeout(timeout)
    planned, message = _preflight_plan(plan, filesystem=False)
    bound_kimi_executable: Optional[_BoundKimiExecutable] = None
    try:
        if not callable(refresher):
            raise SendError("revalidation_required")
        if planned.transport == "kimi_acp":
            runtime_manifest = planned.target.kimi_runtime
            if not isinstance(runtime_manifest, _KimiRuntimeManifest):
                raise SendError("unsupported_kimi")
            bound_kimi_executable = _bind_kimi_executable(runtime_manifest)
            _probe_bound_kimi_version(bound_kimi_executable, version_runner)
            _probe_bound_kimi_initialize(bound_kimi_executable)
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
                    bound_kimi_executable=bound_kimi_executable,
                    bounded_timeout=bounded_timeout,
                    refresher=refresher,
                    executable_resolver=executable_resolver,
                    runtime_dir=runtime_dir,
                    runtime_namespace=audit_namespace,
                    runner=runner,
                    monotonic=monotonic,
                    request_id=validated_request_id,
                    version_runner=version_runner,
                    kimi_runner=kimi_runner,
                    process_guard=process_guard,
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
                audit_outcome = (
                    "failed"
                    if result.agent == "kimi"
                    and result.outcome == "completed"
                    and result.delivery == "unknown"
                    else result.outcome
                )
                audit_namespace.append_terminal(
                    validated_request_id,
                    identity,
                    outcome=audit_outcome,
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
    finally:
        if bound_kimi_executable is not None:
            bound_kimi_executable.close()
        if isinstance(plan, SendPlan) and plan.agent == "kimi":
            _close_kimi_plan(plan)


__all__ = [
    "DEFAULT_SEND_TIMEOUT_SECONDS",
    "DELIVERY_STATES",
    "KIMI_SUPPORTED_VERSION",
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
