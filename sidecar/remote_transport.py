"""Deterministic zipapp packaging and bounded SSH host execution."""

from __future__ import annotations

import io
import os
import shlex
import stat
import subprocess
import threading
import time
import zipfile
import zipimport
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from sidecar.process_runner import (
    BoundedProcessResult,
    run_bounded as _run_bounded,
)
from sidecar.remote_types import (
    FLEET_TIMEOUT_SECONDS,
    HOST_TIMEOUT_SECONDS,
    MAX_ARTIFACT_BYTES,
    MAX_PROTOCOL_BYTES,
    MAX_STDERR_BYTES,
    PROBE_TIMEOUT_SECONDS,
    ProtocolResourceLimitError,
    RemoteFailure,
    RemoteHost,
    _RemoteResponseFailure,
    _command_arguments,
    _completed_overflow,
    _completed_returncode,
    _completed_stderr,
    _completed_stdout,
    _parse_execution_response,
    _parse_one_line_json,
    _validate_alias,
)


_ROOT_MAIN = (
    '"""Zipapp entry point for agent-sidecar."""\n'
    "from sidecar.cli import main\n\n"
    'if __name__ == "__main__":\n'
    "    raise SystemExit(main())\n"
).encode("utf-8")
_MAX_ARCHIVE_MEMBERS = 2048
_MAX_SOURCE_MEMBERS = 512
_MAX_SOURCE_NAME_BYTES = 1024
REMOTE_MIN_PYTHON = (3, 8, 0)
_FILESYSTEM_REQUIRED_MEMBERS = frozenset(
    (
        "sidecar/__init__.py",
        "sidecar/cli.py",
    )
)
_ARCHIVE_REQUIRED_MEMBERS = frozenset(
    (
        "sidecar/__init__.py",
        "sidecar/cli.py",
        "sidecar/remote_transport.py",
    )
)
_SUPPORTED_SOURCE_COMPRESSION = frozenset((zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED))


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
    if not _FILESYSTEM_REQUIRED_MEMBERS.issubset(members):
        raise ValueError("sidecar package source is incomplete")
    return members


def _active_zipimport_archive() -> Optional[Path]:
    specification = globals().get("__spec__")
    loader = getattr(specification, "loader", None)
    if not isinstance(loader, zipimport.zipimporter):
        return None
    archive_name = getattr(loader, "archive", None)
    if not isinstance(archive_name, str) or not archive_name or "\x00" in archive_name:
        raise ValueError("sidecar zipimport source is unavailable")
    try:
        module_name = loader.get_filename(__name__)
    except (AttributeError, ImportError, OSError) as error:
        raise ValueError("sidecar zipimport source is unavailable") from error
    if not isinstance(module_name, str) or "\x00" in module_name:
        raise ValueError("sidecar zipimport source is unavailable")
    archive_absolute = os.path.abspath(archive_name)
    expected_module = os.path.join(
        archive_absolute,
        "sidecar",
        "remote_transport.py",
    )
    if os.path.normcase(os.path.abspath(module_name)) != os.path.normcase(
        expected_module
    ):
        raise ValueError("sidecar zipimport source is unavailable")
    try:
        return Path(archive_absolute).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("sidecar zipimport source is unavailable") from error


def _canonical_source_member(name: object) -> bool:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        return False
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if len(encoded) > _MAX_SOURCE_NAME_BYTES:
        return False
    parts = name.split("/")
    return bool(
        len(parts) >= 2
        and parts[0] == "sidecar"
        and all(part not in ("", ".", "..") for part in parts)
        and parts[-1].endswith(".py")
        and parts[-1] != ".py"
    )


def _archive_source_members(archive_path: Path) -> Dict[str, bytes]:
    members: Dict[str, bytes] = {"__main__.py": _ROOT_MAIN}
    source_bytes = len(_ROOT_MAIN)
    try:
        with Path(archive_path).open("rb") as stream:
            details = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(details.st_mode)
                or details.st_size <= 0
                or details.st_size > MAX_ARTIFACT_BYTES
            ):
                raise ValueError("sidecar zipimport archive exceeds limits")
            with zipfile.ZipFile(stream, mode="r", allowZip64=False) as archive:
                infos = archive.infolist()
                if len(infos) > _MAX_ARCHIVE_MEMBERS:
                    raise ValueError("sidecar zipimport archive exceeds limits")
                source_count = 0
                for info in infos:
                    name = info.filename
                    if not _canonical_source_member(name):
                        if isinstance(name, str) and (
                            name.startswith("sidecar\\")
                            or (name.startswith("sidecar/") and name.endswith(".py"))
                        ):
                            raise ValueError("sidecar zipimport member is invalid")
                        continue
                    source_count += 1
                    if source_count > _MAX_SOURCE_MEMBERS or name in members:
                        raise ValueError("sidecar zipimport archive exceeds limits")
                    mode = info.external_attr >> 16
                    if (
                        info.is_dir()
                        or info.create_system not in (0, 3)
                        or (
                            info.create_system == 3
                            and stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                        )
                        or info.flag_bits & 0x1
                        or info.compress_type not in _SUPPORTED_SOURCE_COMPRESSION
                        or info.file_size < 0
                        or info.compress_size < 0
                    ):
                        raise ValueError("sidecar zipimport member is invalid")
                    remaining = MAX_ARTIFACT_BYTES - source_bytes
                    if info.file_size > remaining:
                        raise ValueError("sidecar zipimport archive exceeds limits")
                    with archive.open(info, mode="r") as source:
                        data = source.read(remaining + 1)
                    if len(data) != info.file_size or len(data) > remaining:
                        raise ValueError("sidecar zipimport archive exceeds limits")
                    members[name] = data
                    source_bytes += len(data)
    except ValueError:
        raise
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as error:
        raise ValueError("sidecar zipimport archive is invalid") from error
    if not _ARCHIVE_REQUIRED_MEMBERS.issubset(members):
        raise ValueError("sidecar package source is incomplete")
    return members


def build_zipapp_bytes(*, source_root: Optional[Path] = None) -> bytes:
    """Build a reproducible, uncompressed sidecar zipapp in memory."""

    archive_path = _active_zipimport_archive() if source_root is None else None
    if archive_path is None:
        root = (
            Path(__file__).resolve().parent.parent
            if source_root is None
            else Path(source_root)
        )
        members = _source_members(root)
    else:
        members = _archive_source_members(archive_path)
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


_PROBE_CODE = (
    "import json,sys;"
    "v=sys.version_info;"
    'print(json.dumps({"python":[v.major,v.minor,v.micro]},'
    'separators=(",",":")))'
)

REMOTE_BOOTSTRAP = r"""
import json
import math
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
MAX_RECENT = 31536000.0
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

def allowed_child_args(args):
    if args in (["list", "--json", "--all"], ["status", "--json"]):
        return args
    if (
        len(args) == 4
        and args[:3] == ["list", "--json", "--recent-seconds"]
    ):
        try:
            seconds = float(args[3])
        except (TypeError, ValueError, OverflowError):
            return None
        if math.isfinite(seconds) and 0 < seconds <= MAX_RECENT:
            return args
    return None

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
    child_args = allowed_child_args(sys.argv[1:])
    if child_args is None:
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
                emit(False, code="resource_limit" if overflow == "stdout" else "protocol")
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
                    except RecursionError:
                        emit(False, code="resource_limit")
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


def remote_shell_command(
    command: str,
    *,
    recent_seconds: Optional[float] = None,
) -> str:
    """Return the fixed, shell-quoted bootstrap command for an allowlisted op."""

    return shlex.join(
        ("python3", "-c", REMOTE_BOOTSTRAP)
        + _command_arguments(command, recent_seconds)
    )


def ssh_argv(
    alias: str,
    *,
    command: Optional[str] = None,
    recent_seconds: Optional[float] = None,
) -> Tuple[str, ...]:
    """Build direct OpenSSH argv for a probe or allowlisted sidecar command."""

    if command is None and recent_seconds is not None:
        raise ValueError("recent_seconds requires a remote command")
    remote_command = (
        _PROBE_REMOTE_COMMAND
        if command is None
        else remote_shell_command(command, recent_seconds=recent_seconds)
    )
    return _ssh_command_argv(alias, remote_command)


def _ssh_command_argv(alias: str, remote_command: str) -> Tuple[str, ...]:
    """Build strict noninteractive OpenSSH argv for one fixed remote command."""

    _validate_alias(alias)
    if (
        not isinstance(remote_command, str)
        or not remote_command
        or "\x00" in remote_command
    ):
        raise ValueError("invalid remote command")
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


def probe_remote_python(
    host: RemoteHost,
    *,
    runner: Optional[Callable[..., object]] = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[Optional[Tuple[int, int, int]], Optional[RemoteFailure]]:
    """Probe one host for bounded Python 3.8+ capability."""

    if not isinstance(host, RemoteHost):
        raise TypeError("host must be a RemoteHost")
    try:
        bounded_timeout = float(timeout)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("probe timeout is out of bounds") from error
    if not 0 < bounded_timeout <= PROBE_TIMEOUT_SECONDS:
        raise ValueError("probe timeout is out of bounds")
    if cancel_event is not None and cancel_event.is_set():
        return None, RemoteFailure(host.alias, "timeout")
    try:
        if runner is None:
            completed = _bounded_popen(
                ssh_argv(host.alias),
                stdout_limit=1024,
                stderr_limit=MAX_STDERR_BYTES,
                timeout=bounded_timeout,
                cancel_event=cancel_event,
            )
        else:
            completed = runner(
                list(ssh_argv(host.alias)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=bounded_timeout,
            )
    except subprocess.TimeoutExpired:
        return None, RemoteFailure(host.alias, "timeout")
    except OSError:
        return None, RemoteFailure(host.alias, "unreachable")
    if _completed_overflow(completed) is not None:
        code = (
            "resource_limit"
            if _completed_overflow(completed) == "stdout"
            else "protocol"
        )
        return None, RemoteFailure(host.alias, code)
    if _completed_returncode(completed) != 0:
        return None, RemoteFailure(host.alias, _failure_code(completed))
    try:
        version = _probe_version(_completed_stdout(completed))
    except ProtocolResourceLimitError:
        return None, RemoteFailure(host.alias, "resource_limit")
    except (TypeError, UnicodeError, ValueError):
        return None, RemoteFailure(host.alias, "protocol")
    if version < REMOTE_MIN_PYTHON:
        return None, RemoteFailure(host.alias, "python_too_old")
    return version, None


def execute_remote_host(
    host: RemoteHost,
    command: str,
    artifact: bytes,
    *,
    recent_seconds: Optional[float] = None,
    runner: Optional[Callable[..., object]] = None,
    timeout: float = HOST_TIMEOUT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[Optional[Tuple[Mapping[str, Any], ...]], Optional[RemoteFailure]]:
    """Probe and execute one host without retries or persistent remote files."""

    _command_arguments(command, recent_seconds)
    if not isinstance(host, RemoteHost):
        raise TypeError("host must be a RemoteHost")
    if (
        not isinstance(artifact, bytes)
        or not artifact
        or len(artifact) > MAX_ARTIFACT_BYTES
    ):
        raise ValueError("invalid zipapp artifact")
    if not 0 < float(timeout) <= HOST_TIMEOUT_SECONDS:
        raise ValueError("host timeout is out of bounds")
    deadline = monotonic() + float(timeout)

    remaining = deadline - monotonic()
    if remaining <= 0 or (cancel_event is not None and cancel_event.is_set()):
        return None, RemoteFailure(host.alias, "timeout")
    _version, failure = probe_remote_python(
        host,
        runner=runner,
        timeout=min(PROBE_TIMEOUT_SECONDS, remaining),
        cancel_event=cancel_event,
    )
    if failure is not None:
        return None, failure

    remaining = deadline - monotonic()
    if remaining <= 0 or (cancel_event is not None and cancel_event.is_set()):
        return None, RemoteFailure(host.alias, "timeout")
    try:
        if runner is None:
            completed = _bounded_popen(
                ssh_argv(
                    host.alias,
                    command=command,
                    recent_seconds=recent_seconds,
                ),
                input_data=artifact,
                input_limit=MAX_ARTIFACT_BYTES,
                stdout_limit=MAX_PROTOCOL_BYTES,
                stderr_limit=MAX_STDERR_BYTES,
                timeout=remaining,
                cancel_event=cancel_event,
            )
        else:
            completed = runner(
                list(
                    ssh_argv(
                        host.alias,
                        command=command,
                        recent_seconds=recent_seconds,
                    )
                ),
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
        code = (
            "resource_limit"
            if _completed_overflow(completed) == "stdout"
            else "protocol"
        )
        return None, RemoteFailure(host.alias, code)
    if _completed_returncode(completed) != 0:
        return None, RemoteFailure(host.alias, _failure_code(completed))
    try:
        rows = _parse_execution_response(_completed_stdout(completed), host.alias)
    except _RemoteResponseFailure as error:
        return None, RemoteFailure(host.alias, error.code)
    except ProtocolResourceLimitError:
        return None, RemoteFailure(host.alias, "resource_limit")
    except (TypeError, UnicodeError, ValueError):
        return None, RemoteFailure(host.alias, "protocol")
    return rows, None


__all__ = [
    "REMOTE_BOOTSTRAP",
    "REMOTE_MIN_PYTHON",
    "build_zipapp_bytes",
    "execute_remote_host",
    "probe_remote_python",
    "remote_shell_command",
    "ssh_argv",
]
