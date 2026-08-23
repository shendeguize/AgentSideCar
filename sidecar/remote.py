"""Bounded, dependency-free remote inventory and sidecar aggregation.

The SSH transport intentionally has a very small surface: it probes a fixed
``python3`` command, then streams an in-memory zipapp to a fixed bootstrap.
Host aliases are always separate argv entries and no inventory data is copied
into the remote shell command.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
import math
import os
import re
import shlex
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sidecar.process_runner import (
    BoundedProcessResult,
    bounded_execution_signal_guard as _bounded_execution_signal_guard,
    run_bounded as _run_bounded,
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
MAX_JSON_ITEMS = 65536
MAX_JSON_STRING_BYTES = 256 * 1024
PROBE_TIMEOUT_SECONDS = 5.0
HOST_TIMEOUT_SECONDS = 15.0
FLEET_TIMEOUT_SECONDS = 30.0
REMOTE_CHILD_TIMEOUT_SECONDS = 8
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
_STATUS_VALUES = frozenset(("working", "waiting", "idle", "dead"))
_COMMAND_ARGS = {
    "list": ("list", "--json", "--all"),
    "status": ("status", "--json"),
}
_ROOT_MAIN = (
    '"""Zipapp entry point for agent-sidecar."""\n'
    "from sidecar.cli import main\n\n"
    'if __name__ == "__main__":\n'
    "    raise SystemExit(main())\n"
).encode("utf-8")


class RemoteInventoryError(ValueError):
    """A deliberately non-diagnostic inventory error safe for CLI display."""

    code = "inventory"
    exit_code = EXIT_INVALID_INVENTORY

    def __init__(self) -> None:
        super().__init__("remote inventory is unavailable or invalid")


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
        if not self.succeeded:
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


def _bounded_popen(
    argv: Sequence[str],
    *,
    input_data: Optional[bytes] = None,
    input_limit: int = MAX_ARTIFACT_BYTES,
    stdout_limit: int,
    stderr_limit: int = MAX_STDERR_BYTES,
    timeout: float,
    env: Optional[Mapping[str, str]] = None,
    cancel_event: Optional[threading.Event] = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> BoundedProcessResult:
    """Run the canonical bounded transport within the remote timeout ceiling."""

    try:
        bounded_timeout = float(timeout)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("invalid bounded transport limit") from error
    if not 0 < bounded_timeout <= FLEET_TIMEOUT_SECONDS:
        raise ValueError("invalid bounded transport limit")
    return _run_bounded(
        argv,
        input_data,
        input_limit=input_limit,
        stdout_limit=stdout_limit,
        stderr_limit=stderr_limit,
        timeout=bounded_timeout,
        env=env,
        cancel_event=cancel_event,
        monotonic=monotonic,
    )


def _command_arguments(command: object) -> Tuple[str, ...]:
    if not isinstance(command, str) or command not in _COMMAND_ARGS:
        raise ValueError("remote command must be list or status")
    return _COMMAND_ARGS[command]


def _duplicate_checked_object(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("non-finite JSON number")


def _validate_json_value(value: Any, depth: int = 0, count: Optional[List[int]] = None) -> None:
    if count is None:
        count = [0]
    count[0] += 1
    if count[0] > MAX_JSON_ITEMS or depth > MAX_JSON_DEPTH:
        raise ValueError("JSON structure exceeds limits")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if value.bit_length() > 4096:
            raise ValueError("JSON integer exceeds limits")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        return
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("JSON string contains invalid Unicode") from error
        if len(encoded) > MAX_JSON_STRING_BYTES:
            raise ValueError("JSON string exceeds limits")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth + 1, count)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object key is not text")
            _validate_json_value(key, depth + 1, count)
            _validate_json_value(item, depth + 1, count)
        return
    raise ValueError("unsupported JSON value")


def parse_bounded_json(payload: object, *, max_bytes: int) -> Any:
    """Parse UTF-8 JSON with duplicate-key, size, depth, and item limits."""

    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("JSON payload contains invalid Unicode") from error
    elif isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    else:
        raise ValueError("JSON payload must be bytes or text")
    if not raw or len(raw) > max_bytes:
        raise ValueError("JSON payload exceeds limits")
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_checked_object,
            parse_constant=_reject_json_constant,
        )
        _validate_json_value(value)
    except (RecursionError, UnicodeError, ValueError) as error:
        raise ValueError("invalid bounded JSON payload") from error
    return value


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
    if not raw or len(raw) > max_bytes or b"\r" in raw:
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


def _inventory_container(value: Any) -> List[Tuple[Optional[str], Mapping[str, Any]]]:
    container = value
    if isinstance(value, dict):
        if "hosts" not in value:
            raise RemoteInventoryError()
        container = value["hosts"]
    rows: List[Tuple[Optional[str], Mapping[str, Any]]] = []
    if isinstance(container, list):
        if len(container) > MAX_HOSTS:
            raise RemoteInventoryError()
        for row in container:
            if not isinstance(row, dict):
                raise RemoteInventoryError()
            rows.append((None, row))
        return rows
    if isinstance(container, dict):
        if len(container) > MAX_HOSTS:
            raise RemoteInventoryError()
        for alias, row in container.items():
            if not isinstance(row, dict):
                raise RemoteInventoryError()
            rows.append((alias, row))
        return rows
    raise RemoteInventoryError()


def _row_alias(key_alias: Optional[str], row: Mapping[str, Any]) -> str:
    row_alias = row.get("name")
    if key_alias is None:
        alias = row_alias
    else:
        alias = key_alias
        if row_alias is not None and row_alias != key_alias:
            raise RemoteInventoryError()
    try:
        return _validate_alias(alias)
    except ValueError as error:
        raise RemoteInventoryError() from error


def _register_alias(alias: str, aliases: Dict[str, str]) -> None:
    folded = alias.casefold()
    if folded in aliases:
        raise RemoteInventoryError()
    aliases[folded] = alias


def _strict_bool(value: object) -> bool:
    if type(value) is not bool:
        raise RemoteInventoryError()
    return value


def _strict_phase(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        raise RemoteInventoryError()
    return value


def _hosts_from_canonical(value: Any) -> Tuple[RemoteHost, ...]:
    aliases: Dict[str, str] = {}
    eligible: List[RemoteHost] = []
    for key_alias, row in _inventory_container(value):
        alias = _row_alias(key_alias, row)
        _register_alias(alias, aliases)
        config = row.get("config")
        if config is not None and not isinstance(config, dict):
            raise RemoteInventoryError()
        config = {} if config is None else config
        local = _strict_bool(row.get("local", config.get("local")))
        if local:
            continue
        enabled = _strict_bool(config.get("enabled", row.get("enabled")))
        orphaned = _strict_bool(row.get("orphaned"))
        phase = _strict_phase(row.get("phase"))
        if enabled and not orphaned and phase in ELIGIBLE_PHASES:
            try:
                eligible.append(RemoteHost(alias=alias, phase=phase))
            except ValueError as error:
                raise RemoteInventoryError() from error
    return tuple(sorted(eligible, key=lambda host: (host.alias.casefold(), host.alias)))


def _fallback_rows(value: Any) -> List[Tuple[str, Mapping[str, Any]]]:
    rows = _inventory_container(value)
    aliases: Dict[str, str] = {}
    normalized: List[Tuple[str, Mapping[str, Any]]] = []
    for key_alias, row in rows:
        alias = _row_alias(key_alias, row)
        _register_alias(alias, aliases)
        normalized.append((alias, row))
    return normalized


def _hosts_from_fallback(config_value: Any, state_value: Any) -> Tuple[RemoteHost, ...]:
    config_rows = _fallback_rows(config_value)
    state_rows = _fallback_rows(state_value)
    states = {alias.casefold(): (alias, row) for alias, row in state_rows}
    eligible: List[RemoteHost] = []
    for alias, config in config_rows:
        state_entry = states.get(alias.casefold())
        if state_entry is None:
            continue
        state_alias, state = state_entry
        if state_alias != alias:
            raise RemoteInventoryError()
        enabled = _strict_bool(config.get("enabled"))
        local = _strict_bool(config.get("local"))
        if not enabled or local:
            continue
        phase = _strict_phase(state.get("phase"))
        if phase in ELIGIBLE_PHASES:
            orphaned = _strict_bool(state.get("orphaned"))
            if orphaned:
                continue
            try:
                eligible.append(RemoteHost(alias=alias, phase=phase))
            except ValueError as error:
                raise RemoteInventoryError() from error
    return tuple(sorted(eligible, key=lambda host: (host.alias.casefold(), host.alias)))


def _inventory_root(environment: Mapping[str, str], home: Optional[Path]) -> Path:
    configured = environment.get("DSHC_HOME")
    if configured:
        return Path(configured).expanduser()
    if home is not None:
        return Path(home).expanduser() / ".dsh_center"
    configured_home = environment.get("HOME")
    if configured_home:
        return Path(configured_home).expanduser() / ".dsh_center"
    return Path.home() / ".dsh_center"


def _read_bounded_inventory_file(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(MAX_INVENTORY_BYTES + 1)


def load_remote_hosts(
    *,
    runner: Optional[Callable[..., object]] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    file_reader: Optional[Callable[[Path], bytes]] = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Tuple[RemoteHost, ...]:
    """Load eligible hosts from dshc, falling back to merged config and state.

    A successful ``dshc ls --json`` result is authoritative and malformed
    successful output is rejected. Manager status is not host inventory; when
    ``ls`` is unavailable, the fallback requires both config and state files.
    """

    if not 0 < float(timeout) <= 30:
        raise ValueError("inventory timeout is out of bounds")
    environment = dict(os.environ if env is None else env)
    argv = ("dshc", "ls", "--json")
    try:
        if runner is None:
            completed = _run_bounded(
                argv,
                input_limit=1,
                stdout_limit=MAX_INVENTORY_BYTES,
                stderr_limit=MAX_STDERR_BYTES,
                timeout=float(timeout),
                env=environment,
            )
        else:
            completed = runner(
                list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=float(timeout),
                env=environment,
            )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if (
        completed is not None
        and _completed_overflow(completed) is None
        and _completed_returncode(completed) == 0
    ):
        try:
            value = parse_bounded_json(
                _completed_stdout(completed),
                max_bytes=MAX_INVENTORY_BYTES,
            )
            return _hosts_from_canonical(value)
        except (TypeError, UnicodeError, ValueError) as error:
            raise RemoteInventoryError() from error

    root = _inventory_root(environment, home)
    reader = _read_bounded_inventory_file if file_reader is None else file_reader
    try:
        config_payload = reader(root / "config.json")
        state_payload = reader(root / "state.json")
        config_value = parse_bounded_json(
            config_payload,
            max_bytes=MAX_INVENTORY_BYTES,
        )
        state_value = parse_bounded_json(
            state_payload,
            max_bytes=MAX_INVENTORY_BYTES,
        )
        return _hosts_from_fallback(config_value, state_value)
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise RemoteInventoryError() from error


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    return info


def _source_members(source_root: Path) -> Dict[str, bytes]:
    root = source_root.resolve()
    package = root / "sidecar"
    if not package.is_dir() or package.is_symlink():
        raise ValueError("sidecar package source is unavailable")
    members: Dict[str, bytes] = {"__main__.py": _ROOT_MAIN}
    for source in sorted(package.rglob("*.py"), key=lambda path: path.as_posix()):
        if not source.is_file() or source.is_symlink():
            continue
        try:
            resolved = source.resolve()
            relative = resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise ValueError("sidecar source escapes package root") from error
        name = relative.as_posix()
        if "__pycache__" in relative.parts or not name.startswith("sidecar/"):
            continue
        data = source.read_bytes()
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ValueError("sidecar source exceeds artifact limit")
        members[name] = data
    if "sidecar/__init__.py" not in members or "sidecar/cli.py" not in members:
        raise ValueError("sidecar package source is incomplete")
    return members


def build_zipapp_bytes(*, source_root: Optional[Path] = None) -> bytes:
    """Build a reproducible, uncompressed sidecar zipapp in memory."""

    root = (
        Path(__file__).resolve().parent.parent
        if source_root is None
        else Path(source_root)
    )
    members = _source_members(root)
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=False,
    ) as archive:
        for name in sorted(members):
            archive.writestr(_zip_info(name), members[name])
    artifact = output.getvalue()
    if not artifact or len(artifact) > MAX_ARTIFACT_BYTES:
        raise ValueError("sidecar zipapp exceeds artifact limit")
    return artifact


def build_zipapp_to_path(
    path: Path,
    *,
    source_root: Optional[Path] = None,
) -> Path:
    """Atomically write a deterministic zipapp and return its final path."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    artifact = build_zipapp_bytes(source_root=source_root)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(destination.name),
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(artifact)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(destination))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


_PROBE_CODE = (
    "import json,sys;"
    "v=sys.version_info;"
    'print(json.dumps({"python":[v.major,v.minor,v.micro]},'
    'separators=(",",":")))'
)

REMOTE_BOOTSTRAP = r"""
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time

MAX_ARTIFACT = 4194304
MAX_OUTPUT = 8388608
MAX_ERROR = 65536
CHILD_TIMEOUT = 8
path = None
fd = None

def emit(ok, **fields):
    value = {"ok": ok}
    value.update(fields)
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))

def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value

def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

def bounded_child(argv):
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        kill_group(process)
        raise OSError("pipes")
    selector = selectors.DefaultSelector()
    streams = (process.stdout, process.stderr)
    output = bytearray()
    errors = bytearray()
    overflow = None
    timed_out = False
    deadline = time.monotonic() + CHILD_TIMEOUT

    def close_stream(stream):
        try:
            selector.unregister(stream)
        except (KeyError, ValueError):
            pass
        try:
            stream.close()
        except OSError:
            pass

    try:
        for kind, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, (kind, stream))

        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                kill_group(process)
                for key in list(selector.get_map().values()):
                    close_stream(key.fileobj)
                break
            events = selector.select(min(0.05, remaining))
            for key, _mask in events:
                kind, stream = key.data
                target = output if kind == "stdout" else errors
                limit = MAX_OUTPUT if kind == "stdout" else MAX_ERROR
                available = max(0, limit - len(target))
                try:
                    chunk = os.read(
                        stream.fileno(),
                        max(1, min(65536, available + 1)),
                    )
                except BlockingIOError:
                    continue
                except OSError:
                    close_stream(stream)
                    continue
                if not chunk:
                    close_stream(stream)
                    continue
                target.extend(chunk[:available])
                if len(chunk) > available:
                    overflow = kind
                    kill_group(process)
                    for active in list(selector.get_map().values()):
                        close_stream(active.fileobj)
                    break
            if overflow is not None:
                break

        try:
            returncode = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            kill_group(process)
            returncode = process.wait(timeout=1)
    finally:
        if process.poll() is None:
            kill_group(process)
        for stream in streams:
            close_stream(stream)
        selector.close()
        if process.poll() is None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                kill_group(process)

    return returncode, bytes(output), overflow, timed_out

try:
    child_args = sys.argv[1:]
    if child_args not in (["list", "--json", "--all"], ["status", "--json"]):
        emit(False, code="protocol")
    else:
        data = sys.stdin.buffer.read(MAX_ARTIFACT + 1)
        if not data or len(data) > MAX_ARTIFACT:
            emit(False, code="protocol")
        else:
            fd, path = tempfile.mkstemp(suffix=".pyz")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(data)
            returncode, output, overflow, timed_out = bounded_child(
                [sys.executable, "-I", path] + child_args
            )
            if timed_out:
                emit(False, code="timeout")
            elif overflow is not None:
                emit(False, code="protocol")
            elif returncode != 0:
                emit(False, code="remote")
            elif not output or b"\r" in output:
                emit(False, code="protocol")
            else:
                if output.endswith(b"\n"):
                    output = output[:-1]
                if not output or b"\n" in output or output != output.strip():
                    emit(False, code="protocol")
                else:
                    try:
                        rows = json.loads(
                            output.decode("utf-8"),
                            object_pairs_hook=pairs,
                            parse_constant=lambda value: (_ for _ in ()).throw(
                                ValueError("constant")
                            ),
                        )
                        if not isinstance(rows, list):
                            raise ValueError("rows")
                    except Exception:
                        emit(False, code="protocol")
                    else:
                        emit(True, rows=rows)
except Exception:
    emit(False, code="remote")
finally:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    if path is not None:
        try:
            os.unlink(path)
        except OSError:
            pass
""".strip()

_PROBE_REMOTE_COMMAND = shlex.join(("python3", "-c", _PROBE_CODE))


def remote_shell_command(command: str) -> str:
    """Return the fixed, shell-quoted bootstrap command for an allowlisted op."""

    return shlex.join(("python3", "-c", REMOTE_BOOTSTRAP) + _command_arguments(command))


def ssh_argv(alias: str, *, command: Optional[str] = None) -> Tuple[str, ...]:
    """Build direct OpenSSH argv for a probe or allowlisted sidecar command."""

    _validate_alias(alias)
    remote_command = (
        _PROBE_REMOTE_COMMAND if command is None else remote_shell_command(command)
    )
    return (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=4",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ControlPersist=no",
        "-T",
        alias,
        remote_command,
    )


def _safe_stderr(completed: object) -> str:
    value = _completed_stderr(completed)
    if isinstance(value, str):
        raw = value.encode("utf-8", "replace")
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    else:
        raw = b""
    return raw[:65536].decode("utf-8", "replace").casefold()


def _failure_code(completed: object) -> str:
    text = _safe_stderr(completed)
    if (
        "host key verification failed" in text
        or "remote host identification has changed" in text
        or "offending " in text
    ):
        return "host_key"
    if (
        "permission denied" in text
        or "authentication failed" in text
        or "no supported authentication methods" in text
    ):
        return "auth"
    if "timed out" in text or "operation timeout" in text:
        return "timeout"
    if any(
        marker in text
        for marker in (
            "no route to host",
            "network is unreachable",
            "connection refused",
            "could not resolve hostname",
            "name or service not known",
            "connection closed",
            "connection reset",
        )
    ):
        return "unreachable"
    if _completed_returncode(completed) == 255:
        return "unreachable"
    return "remote"


def _probe_version(payload: object) -> Tuple[int, int, int]:
    value = _parse_one_line_json(payload, max_bytes=1024)
    if not isinstance(value, dict) or set(value) != {"python"}:
        raise ValueError("invalid capability response")
    version = value["python"]
    if (
        not isinstance(version, list)
        or len(version) != 3
        or any(type(item) is not int or item < 0 or item > 999 for item in version)
    ):
        raise ValueError("invalid capability response")
    return version[0], version[1], version[2]


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
        raise ValueError("remote row text exceeds limit")
    return value


def _validate_updated_at(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid remote row timestamp")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("invalid remote row timestamp")
    if value < MIN_SESSION_TIMESTAMP or value > MAX_SESSION_TIMESTAMP:
        raise ValueError("remote row timestamp exceeds datetime range")


def _encoded_row(source: Mapping[str, Any]) -> bytes:
    return json.dumps(
        source,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def _validate_protocol_rows(value: object, host: str) -> Tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise ValueError("invalid remote rows")
    rows: List[Mapping[str, Any]] = []
    for source in value:
        if not isinstance(source, dict) or set(source) != _SESSION_KEYS:
            raise ValueError("invalid remote row")
        _validate_json_value(source)
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
        parent_id = source["parent_id"]
        if parent_id is not None:
            _validated_session_string(source, "parent_id")
        encoded = _encoded_row(source)
        if len(encoded) > MAX_ROW_BYTES:
            raise ValueError("remote row exceeds limit")
        row = dict(source)
        row["host"] = host
        rows.append(row)
    return tuple(rows)


def _parse_execution_response(payload: object, host: str) -> Tuple[Mapping[str, Any], ...]:
    value = _parse_one_line_json(payload, max_bytes=MAX_PROTOCOL_BYTES)
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise ValueError("invalid remote response")
    if value["ok"] is False:
        if set(value) != {"ok", "code"}:
            raise ValueError("invalid remote response")
        code = value["code"]
        if code not in ("timeout", "protocol", "remote"):
            raise ValueError("invalid remote response")
        raise _RemoteResponseFailure(code)
    if set(value) != {"ok", "rows"}:
        raise ValueError("invalid remote response")
    return _validate_protocol_rows(value["rows"], host)


class _RemoteResponseFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def execute_remote_host(
    host: RemoteHost,
    command: str,
    artifact: bytes,
    *,
    runner: Optional[Callable[..., object]] = None,
    timeout: float = HOST_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[Optional[Tuple[Mapping[str, Any], ...]], Optional[RemoteFailure]]:
    """Probe and execute one host without retries or persistent remote files."""

    _command_arguments(command)
    if not isinstance(host, RemoteHost):
        raise TypeError("host must be a RemoteHost")
    if not isinstance(artifact, bytes) or not artifact or len(artifact) > MAX_ARTIFACT_BYTES:
        raise ValueError("invalid zipapp artifact")
    if not 0 < float(timeout) <= HOST_TIMEOUT_SECONDS:
        raise ValueError("host timeout is out of bounds")
    deadline = monotonic() + float(timeout)

    remaining = deadline - monotonic()
    if remaining <= 0 or (cancel_event is not None and cancel_event.is_set()):
        return None, RemoteFailure(host.alias, "timeout")
    try:
        if runner is None:
            probe = _bounded_popen(
                ssh_argv(host.alias),
                stdout_limit=1024,
                stderr_limit=MAX_STDERR_BYTES,
                timeout=min(PROBE_TIMEOUT_SECONDS, remaining),
                cancel_event=cancel_event,
            )
        else:
            probe = runner(
                list(ssh_argv(host.alias)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=min(PROBE_TIMEOUT_SECONDS, remaining),
            )
    except subprocess.TimeoutExpired:
        return None, RemoteFailure(host.alias, "timeout")
    except OSError:
        return None, RemoteFailure(host.alias, "unreachable")
    if _completed_overflow(probe) is not None:
        return None, RemoteFailure(host.alias, "protocol")
    if _completed_returncode(probe) != 0:
        return None, RemoteFailure(host.alias, _failure_code(probe))
    try:
        version = _probe_version(_completed_stdout(probe))
    except (TypeError, UnicodeError, ValueError):
        return None, RemoteFailure(host.alias, "protocol")
    if version < (3, 9, 0):
        return None, RemoteFailure(host.alias, "python_too_old")

    remaining = deadline - monotonic()
    if remaining <= 0 or (cancel_event is not None and cancel_event.is_set()):
        return None, RemoteFailure(host.alias, "timeout")
    try:
        if runner is None:
            completed = _bounded_popen(
                ssh_argv(host.alias, command=command),
                input_data=artifact,
                input_limit=MAX_ARTIFACT_BYTES,
                stdout_limit=MAX_PROTOCOL_BYTES,
                stderr_limit=MAX_STDERR_BYTES,
                timeout=remaining,
                cancel_event=cancel_event,
            )
        else:
            completed = runner(
                list(ssh_argv(host.alias, command=command)),
                input=artifact,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=remaining,
            )
    except subprocess.TimeoutExpired:
        return None, RemoteFailure(host.alias, "timeout")
    except OSError:
        return None, RemoteFailure(host.alias, "unreachable")
    if _completed_overflow(completed) is not None:
        return None, RemoteFailure(host.alias, "protocol")
    if _completed_returncode(completed) != 0:
        return None, RemoteFailure(host.alias, _failure_code(completed))
    try:
        rows = _parse_execution_response(_completed_stdout(completed), host.alias)
    except _RemoteResponseFailure as error:
        return None, RemoteFailure(host.alias, error.code)
    except (TypeError, UnicodeError, ValueError):
        return None, RemoteFailure(host.alias, "protocol")
    return rows, None


def _selected_hosts(
    hosts: Sequence[RemoteHost],
    selected: Optional[Iterable[str]],
) -> Tuple[RemoteHost, ...]:
    if len(hosts) > MAX_HOSTS:
        raise ValueError("remote fleet exceeds host limit")
    aliases: Dict[str, RemoteHost] = {}
    for host in hosts:
        if not isinstance(host, RemoteHost):
            raise TypeError("hosts must contain RemoteHost values")
        folded = host.alias.casefold()
        if folded in aliases:
            raise ValueError("duplicate remote host alias")
        aliases[folded] = host
    if selected is None:
        values = list(aliases.values())
    else:
        requested: Dict[str, str] = {}
        for alias in selected:
            valid = _validate_alias(alias)
            folded = valid.casefold()
            if folded in requested:
                raise ValueError("duplicate selected host alias")
            requested[folded] = valid
        missing = sorted(
            (requested[folded] for folded in requested if folded not in aliases),
            key=lambda alias: (alias.casefold(), alias),
        )
        if missing:
            raise ValueError(
                "selected remote host is not eligible: {}".format(missing[0])
            )
        values = [host for folded, host in aliases.items() if folded in requested]
    return tuple(sorted(values, key=lambda host: (host.alias.casefold(), host.alias)))


def _row_sort_key(row: Mapping[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(row.get("host", "")).casefold(),
        str(row.get("agent", "")).casefold(),
        str(row.get("session_id", "")),
        json.dumps(
            row,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ),
    )


def aggregate_remote(
    command: str,
    *,
    hosts: Optional[Sequence[RemoteHost]] = None,
    selected: Optional[Iterable[str]] = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    runner: Optional[Callable[..., object]] = None,
    artifact: Optional[bytes] = None,
    inventory_env: Optional[Mapping[str, str]] = None,
    inventory_home: Optional[Path] = None,
    inventory_file_reader: Optional[Callable[[Path], bytes]] = None,
    fleet_timeout: float = FLEET_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> RemoteAggregate:
    """Run an allowlisted read-only command across an eligible remote fleet."""

    _command_arguments(command)
    if type(max_workers) is not int or max_workers <= 0:
        raise ValueError("max_workers must be a positive integer")
    if not 0 < float(fleet_timeout) <= FLEET_TIMEOUT_SECONDS:
        raise ValueError("fleet timeout is out of bounds")
    workers = min(max_workers, MAX_WORKERS)
    available = (
        load_remote_hosts(
            runner=runner,
            env=inventory_env,
            home=inventory_home,
            file_reader=inventory_file_reader,
        )
        if hosts is None
        else tuple(hosts)
    )
    targets = _selected_hosts(available, selected)
    labels = tuple(host.alias for host in targets)
    if not targets:
        return RemoteAggregate(command=command, hosts=labels)
    zipapp = build_zipapp_bytes() if artifact is None else artifact
    if not isinstance(zipapp, bytes) or not zipapp or len(zipapp) > MAX_ARTIFACT_BYTES:
        raise ValueError("invalid zipapp artifact")

    rows: List[Mapping[str, Any]] = []
    failures: List[RemoteFailure] = []
    succeeded: List[str] = []
    aggregate_bytes = 2
    cancel_event = threading.Event()
    wall_deadline = monotonic() + float(fleet_timeout)
    cleanup_reserve = min(0.25, float(fleet_timeout) / 5.0)
    deadline = wall_deadline - cleanup_reserve
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(workers, len(targets)),
        thread_name_prefix="sidecar-remote",
    )
    futures: Dict[concurrent.futures.Future, RemoteHost] = {}
    next_target = 0
    deadline_expired = False

    def submit_available() -> None:
        nonlocal next_target
        while len(futures) < workers and next_target < len(targets):
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            host = targets[next_target]
            next_target += 1
            future = executor.submit(
                execute_remote_host,
                host,
                command,
                zipapp,
                runner=runner,
                timeout=min(HOST_TIMEOUT_SECONDS, remaining),
                cancel_event=cancel_event,
            )
            futures[future] = host

    def consume(future: concurrent.futures.Future, host: RemoteHost) -> None:
        nonlocal aggregate_bytes
        try:
            host_rows, failure = future.result()
        except Exception:
            host_rows = None
            failure = RemoteFailure(host.alias, "remote")
        if failure is not None:
            failures.append(failure)
            return
        candidate_rows = tuple(host_rows or ())
        try:
            candidate_bytes = sum(
                len(_encoded_row(row)) + 1 for row in candidate_rows
            )
        except (TypeError, UnicodeError, ValueError):
            failures.append(RemoteFailure(host.alias, "protocol"))
            return
        if (
            len(rows) + len(candidate_rows) > MAX_ROWS
            or aggregate_bytes + candidate_bytes > MAX_AGGREGATE_BYTES
        ):
            failures.append(RemoteFailure(host.alias, "protocol"))
            return
        aggregate_bytes += candidate_bytes
        rows.extend(candidate_rows)
        succeeded.append(host.alias)

    try:
        with _bounded_execution_signal_guard() as process_registry:
            try:
                submit_available()
                while futures:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        deadline_expired = True
                        break
                    done, _pending = concurrent.futures.wait(
                        tuple(futures),
                        timeout=remaining,
                        return_when=concurrent.futures.ALL_COMPLETED,
                    )
                    if not done:
                        deadline_expired = True
                        break
                    for future in sorted(
                        done,
                        key=lambda item: (
                            futures[item].alias.casefold(),
                            futures[item].alias,
                        ),
                    ):
                        consume(future, futures.pop(future))
                    if futures:
                        deadline_expired = True
                        break
                    submit_available()

                if next_target < len(targets):
                    deadline_expired = True
                if deadline_expired:
                    for future in sorted(
                        tuple(futures),
                        key=lambda item: (
                            futures[item].alias.casefold(),
                            futures[item].alias,
                        ),
                    ):
                        if future.done():
                            consume(future, futures.pop(future))
                    cancel_event.set()
                    for future, host in futures.items():
                        future.cancel()
                        failures.append(RemoteFailure(host.alias, "timeout"))
                    for host in targets[next_target:]:
                        failures.append(RemoteFailure(host.alias, "timeout"))
                    cleanup_remaining = max(0.0, wall_deadline - monotonic())
                    if futures and cleanup_remaining > 0:
                        concurrent.futures.wait(
                            tuple(futures),
                            timeout=cleanup_remaining,
                        )
            except BaseException:
                cancel_event.set()
                for future in futures:
                    future.cancel()
                process_registry.kill_all()
                executor.shutdown(wait=True, cancel_futures=True)
                raise
    except BaseException:
        raise
    else:
        executor.shutdown(
            wait=not deadline_expired or all(future.done() for future in futures),
            cancel_futures=deadline_expired,
        )

    return RemoteAggregate(
        command=command,
        rows=tuple(sorted(rows, key=_row_sort_key)),
        failures=tuple(
            sorted(failures, key=lambda item: (item.host.casefold(), item.host))
        ),
        hosts=labels,
        succeeded=tuple(sorted(succeeded, key=lambda alias: (alias.casefold(), alias))),
    )

__all__ = [
    "DEFAULT_MAX_WORKERS",
    "ELIGIBLE_PHASES",
    "EXIT_INVALID_INVENTORY",
    "EXIT_NO_SUCCESS",
    "EXIT_OK",
    "FAILURE_CODES",
    "FLEET_TIMEOUT_SECONDS",
    "HOST_TIMEOUT_SECONDS",
    "MAX_AGGREGATE_BYTES",
    "MAX_ARTIFACT_BYTES",
    "MAX_HOSTS",
    "MAX_INVENTORY_BYTES",
    "MAX_PROTOCOL_BYTES",
    "MAX_ROWS",
    "MAX_STDERR_BYTES",
    "MAX_SESSION_TIMESTAMP",
    "MAX_WORKERS",
    "MIN_SESSION_TIMESTAMP",
    "PROBE_TIMEOUT_SECONDS",
    "REMOTE_BOOTSTRAP",
    "RemoteAggregate",
    "RemoteFailure",
    "RemoteHost",
    "RemoteInventoryError",
    "aggregate_remote",
    "build_zipapp_bytes",
    "build_zipapp_to_path",
    "execute_remote_host",
    "load_remote_hosts",
    "parse_bounded_json",
    "remote_shell_command",
    "ssh_argv",
]
