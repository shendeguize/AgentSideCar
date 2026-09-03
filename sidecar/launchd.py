"""Explicit macOS user LaunchAgent lifecycle for the sidecar daemon."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import math
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

try:
    import pwd
except ImportError:  # pragma: no cover - unavailable on Windows
    pwd = None  # type: ignore[assignment]

from sidecar.client import PingInfo, SidecarClient, SidecarClientError
from sidecar.daemon import RUNTIME_ENV, default_runtime_dir
from sidecar.process_runner import run_bounded
from sidecar.runtime_cmd import (
    RuntimeCommandError,
    resolve_runtime_prefix,
    validate_runtime_prefix,
)


LABEL = "com.agent-sidecar.daemon"
PLIST_NAME = LABEL + ".plist"
LOCK_NAME = "." + LABEL + ".lock"
LAUNCHCTL_PATH = Path("/bin/launchctl")
PATH_VALUE = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
THROTTLE_INTERVAL = 10
CONTROL_TIMEOUT = 10.0
# A daemon answers nothing until its first index scan ends, and that scan grows
# with the index — 22s on a 1,952-session machine. A shorter window boots out a
# healthy service and blames a daemon that was only still scanning, so this
# matches the CLI's own start window.
READY_TIMEOUT = 45.0
POLL_INTERVAL = 0.1
MAX_PLIST_BYTES = 1024 * 1024
MAX_CONTROL_OUTPUT = 64 * 1024
_PLIST_KEYS = frozenset(
    (
        "EnvironmentVariables",
        "KeepAlive",
        "Label",
        "ProcessType",
        "ProgramArguments",
        "RunAtLoad",
        "StandardErrorPath",
        "StandardOutPath",
        "ThrottleInterval",
    )
)


class LaunchdError(RuntimeError):
    """Base class for fixed, terminal-safe service errors."""

    exit_code = 1


class LaunchdControlError(LaunchdError):
    """The platform, launchctl, or requested transition is invalid."""

    exit_code = 2


class LaunchdSecurityError(LaunchdControlError):
    """A service filesystem object failed ownership or type validation."""


class LaunchdOperationError(LaunchdError):
    """A bounded launchd operation failed."""


@dataclass(frozen=True)
class ServiceResult:
    exit_code: int
    message: str


@dataclass(frozen=True)
class ServicePaths:
    home: Path
    library: Path
    agents: Path
    plist: Path
    lock: Path


@dataclass(frozen=True)
class _StoredPlist:
    payload: bytes
    document: Mapping[str, Any]
    identity: Tuple[int, int]
    digest: str
    stat_signature: Tuple[int, ...]


@dataclass(frozen=True)
class _LaunchdJob:
    loaded: bool
    state: Optional[str] = None
    pid: Optional[int] = None

    @property
    def running(self) -> bool:
        return self.loaded and self.state == "running" and self.pid is not None


@dataclass
class _InstallMutation:
    bootout_started: bool = False
    bootout_completed: bool = False
    write_started: bool = False
    write_completed: bool = False
    bootstrap_started: bool = False
    bootstrap_completed: bool = False

    @property
    def started(self) -> bool:
        return (
            self.bootout_started
            or self.write_started
            or self.bootstrap_started
        )


@dataclass(frozen=True)
class _ManagedPlistConfig:
    runtime_dir: Path
    http: bool
    http_port: Optional[int]


def service_domain(euid: Optional[int] = None) -> str:
    uid = _effective_uid() if euid is None else euid
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise LaunchdControlError("service control: invalid user identity")
    return "gui/{}".format(uid)


def _canonical_home(euid: int, home: Optional[Path]) -> Path:
    if home is None:
        if pwd is None:
            raise LaunchdControlError(
                "service control is supported only on macOS with launchctl"
            )
        try:
            value = pwd.getpwuid(euid).pw_dir
        except (KeyError, OSError) as error:
            raise LaunchdSecurityError(
                "service control: cannot resolve the account home"
            ) from error
        candidate = Path(value)
    else:
        candidate = Path(home)
    if not candidate.is_absolute() or "\x00" in str(candidate):
        raise LaunchdSecurityError("service control: account home is invalid")
    return candidate


def service_paths(
    *,
    euid: Optional[int] = None,
    home: Optional[Path] = None,
) -> ServicePaths:
    uid = _effective_uid() if euid is None else euid
    root = _canonical_home(uid, home)
    library = root / "Library"
    agents = library / "LaunchAgents"
    return ServicePaths(
        home=root,
        library=library,
        agents=agents,
        plist=agents / PLIST_NAME,
        lock=agents / LOCK_NAME,
    )


def _effective_uid() -> int:
    provider = getattr(os, "geteuid", None)
    if not callable(provider):
        raise LaunchdControlError(
            "service control is supported only on macOS with launchctl"
        )
    uid = provider()
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise LaunchdControlError("service control: invalid user identity")
    return uid


def _selected_platform(
    platform: Optional[str],
    euid: Optional[int],
) -> Tuple[str, int]:
    selected = sys.platform if platform is None else platform
    if selected != "darwin":
        raise LaunchdControlError(
            "service control is supported only on macOS with launchctl"
        )
    return selected, _effective_uid() if euid is None else euid


def _validate_directory(path: Path, euid: int, *, name: str) -> None:
    try:
        details = path.lstat()
    except OSError as error:
        raise LaunchdSecurityError(
            "service control: cannot inspect the {} directory".format(name)
        ) from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != euid
        or details.st_mode & stat.S_IRWXU != stat.S_IRWXU
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        raise LaunchdSecurityError(
            "service control: unsafe {} directory".format(name)
        )


def _prepare_paths(paths: ServicePaths, euid: int, *, create: bool) -> bool:
    _validate_directory(paths.home, euid, name="home")
    for directory, name in (
        (paths.library, "Library"),
        (paths.agents, "LaunchAgents"),
    ):
        try:
            directory.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                os.mkdir(str(directory), 0o700)
                os.chmod(str(directory), 0o700)
                _fsync_directory(directory.parent)
            except OSError as error:
                raise LaunchdSecurityError(
                    "service control: cannot create the {} directory".format(name)
                ) from error
        except OSError as error:
            raise LaunchdSecurityError(
                "service control: cannot inspect the {} directory".format(name)
            ) from error
        _validate_directory(directory, euid, name=name)
    return True


def _ensure_supported(platform: str, launchctl_path: Path) -> Path:
    if platform != "darwin":
        raise LaunchdControlError(
            "service control is supported only on macOS with launchctl"
        )
    try:
        resolved = launchctl_path.resolve(strict=True)
        details = resolved.stat()
    except (OSError, RuntimeError) as error:
        raise LaunchdControlError(
            "service control requires the macOS launchctl executable"
        ) from error
    if (
        not resolved.is_absolute()
        or not stat.S_ISREG(details.st_mode)
        or not os.access(str(resolved), os.X_OK)
    ):
        raise LaunchdControlError(
            "service control requires the macOS launchctl executable"
        )
    return resolved


def _runtime_directory(value: Optional[Path]) -> str:
    selected = default_runtime_dir() if value is None else Path(value)
    rendered = str(selected)
    try:
        encoded = os.fsencode(rendered)
    except (UnicodeError, ValueError) as error:
        raise LaunchdSecurityError(
            "service install: runtime directory is invalid"
        ) from error
    if (
        not selected.is_absolute()
        or not rendered
        or len(encoded) > 4096
        or "\x00" in rendered
        or "$" in rendered
        or "~" in rendered
        or any(ord(character) < 32 or ord(character) == 127 for character in rendered)
    ):
        raise LaunchdSecurityError("service install: runtime directory is invalid")
    try:
        return str(selected.resolve(strict=False))
    except (OSError, RuntimeError) as error:
        raise LaunchdSecurityError(
            "service install: runtime directory is invalid"
        ) from error


def _validated_runtime_prefix(prefix: Sequence[str]) -> Tuple[str, ...]:
    try:
        return validate_runtime_prefix(prefix)
    except RuntimeCommandError as error:
        raise LaunchdSecurityError(
            "service install: runtime command is invalid"
        ) from error


def build_program_arguments(
    prefix: Sequence[str],
    *,
    http: bool = False,
    http_port: Optional[int] = None,
) -> Tuple[str, ...]:
    arguments = _validated_runtime_prefix(prefix)
    if http_port is not None and not http:
        raise LaunchdControlError("service install: --http-port requires --http")
    if http_port is not None and (
        not isinstance(http_port, int)
        or isinstance(http_port, bool)
        or not 0 <= http_port <= 65535
    ):
        raise LaunchdControlError("service install: HTTP port is invalid")
    command = arguments + ("daemon", "run")
    if http:
        command += ("--http",)
        if http_port is not None:
            command += ("--http-port", str(http_port))
    return command


def build_plist(
    prefix: Sequence[str],
    *,
    runtime_dir: Optional[Path] = None,
    http: bool = False,
    http_port: Optional[int] = None,
) -> Mapping[str, Any]:
    return {
        "Label": LABEL,
        "ProgramArguments": list(
            build_program_arguments(prefix, http=http, http_port=http_port)
        ),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": THROTTLE_INTERVAL,
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
        "EnvironmentVariables": {
            RUNTIME_ENV: _runtime_directory(runtime_dir),
            "PATH": PATH_VALUE,
        },
    }


def plist_bytes(document: Mapping[str, Any]) -> bytes:
    try:
        payload = plistlib.dumps(
            dict(document),
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        )
    except (TypeError, ValueError, OverflowError) as error:
        raise LaunchdControlError("service install: cannot encode the service") from error
    if len(payload) > MAX_PLIST_BYTES:
        raise LaunchdControlError("service install: service definition is too large")
    return payload


def _plist_stat_signature(details: os.stat_result) -> Tuple[int, ...]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_uid,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _read_plist(path: Path, euid: int) -> Optional[_StoredPlist]:
    parent_descriptor = -1
    descriptor = -1
    try:
        parent_descriptor = os.open(
            str(path.parent),
            os.O_RDONLY
            | os.O_CLOEXEC
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        details = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        return None
    except OSError as error:
        if parent_descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(parent_descriptor)
        raise LaunchdSecurityError(
            "service control: cannot inspect the service definition"
        ) from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != euid
        or stat.S_IMODE(details.st_mode) != 0o644
        or details.st_size > MAX_PLIST_BYTES
    ):
        os.close(parent_descriptor)
        raise LaunchdSecurityError(
            "service control: unsafe service definition"
        )
    identity = (details.st_dev, details.st_ino)
    signature = _plist_stat_signature(details)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_NONBLOCK
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        if (
            _plist_stat_signature(opened) != signature
            or opened.st_uid != euid
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o644
            or opened.st_size > MAX_PLIST_BYTES
        ):
            raise LaunchdSecurityError(
                "service control: service definition changed during validation"
            )
        chunks = []
        remaining = MAX_PLIST_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after_read = os.fstat(descriptor)
        after = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except LaunchdError:
        raise
    except OSError as error:
        raise LaunchdSecurityError(
            "service control: cannot read the service definition"
        ) from error
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if parent_descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(parent_descriptor)
    if (
        len(payload) > MAX_PLIST_BYTES
        or len(payload) != opened.st_size
        or _plist_stat_signature(after_read) != signature
        or _plist_stat_signature(after) != signature
        or after.st_uid != euid
        or not stat.S_ISREG(after.st_mode)
        or stat.S_IMODE(after.st_mode) != 0o644
    ):
        raise LaunchdSecurityError(
            "service control: service definition changed during validation"
        )
    try:
        document = plistlib.loads(payload)
    except (plistlib.InvalidFileException, ValueError, TypeError) as error:
        raise LaunchdSecurityError(
            "service control: invalid service definition"
        ) from error
    if not isinstance(document, Mapping):
        raise LaunchdSecurityError("service control: invalid service definition")
    return _StoredPlist(
        payload,
        document,
        identity,
        hashlib.sha256(payload).hexdigest(),
        signature,
    )


def _program_http_configuration(
    value: object,
    prefix: Sequence[str],
) -> Optional[Tuple[bool, Optional[int]]]:
    if not isinstance(value, list) or not all(type(item) is str for item in value):
        return None
    arguments = tuple(value)
    base = tuple(prefix) + ("daemon", "run")
    if arguments == base:
        return False, None
    if arguments == base + ("--http",):
        return True, None
    if len(arguments) != len(base) + 3 or arguments[: len(base)] != base:
        return None
    if arguments[len(base) : len(base) + 2] != ("--http", "--http-port"):
        return None
    try:
        port = int(arguments[-1])
    except (TypeError, ValueError):
        return None
    if str(port) != arguments[-1] or not 0 <= port <= 65535:
        return None
    return True, port


def _exact_value(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        if set(actual) != set(expected):  # type: ignore[arg-type]
            return False
        return all(
            _exact_value(actual[key], expected[key])  # type: ignore[index]
            for key in expected
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(  # type: ignore[arg-type]
            _exact_value(left, right)
            for left, right in zip(actual, expected)  # type: ignore[arg-type]
        )
    return actual == expected


def _stored_runtime_directory(value: object, euid: int) -> Optional[Path]:
    if type(value) is not str:
        return None
    candidate = Path(value)
    try:
        encoded = os.fsencode(value)
    except (UnicodeError, ValueError):
        return None
    if (
        not candidate.is_absolute()
        or not value
        or len(encoded) > 4096
        or "\x00" in value
        or "$" in value
        or "~" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if resolved != candidate:
        return None
    try:
        details = candidate.lstat()
    except FileNotFoundError:
        return candidate
    except OSError:
        return None
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != euid
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        return None
    return candidate


def _managed_plist(
    document: Mapping[str, Any],
    prefix: Sequence[str],
    euid: int,
) -> Optional[_ManagedPlistConfig]:
    if set(document) != _PLIST_KEYS or document.get("Label") != LABEL:
        return None
    configuration = _program_http_configuration(
        document.get("ProgramArguments"),
        prefix,
    )
    if configuration is None:
        return None
    environment = document.get("EnvironmentVariables")
    if (
        not isinstance(environment, dict)
        or set(environment) != {RUNTIME_ENV, "PATH"}
        or environment.get("PATH") != PATH_VALUE
    ):
        return None
    stored_runtime = _stored_runtime_directory(
        environment.get(RUNTIME_ENV),
        euid,
    )
    if stored_runtime is None:
        return None
    try:
        expected = build_plist(
            prefix,
            runtime_dir=stored_runtime,
            http=configuration[0],
            http_port=configuration[1],
        )
    except LaunchdError:
        return None
    if not _exact_value(dict(document), dict(expected)):
        return None
    return _ManagedPlistConfig(
        runtime_dir=stored_runtime,
        http=configuration[0],
        http_port=configuration[1],
    )


def _client_for_runtime(
    client: Optional[object],
    runtime_dir: Path,
    client_factory: Optional[Callable[[Path], object]],
) -> object:
    if client_factory is not None:
        return client_factory(runtime_dir)
    if client is None:
        return SidecarClient(runtime_dir=runtime_dir)
    socket_path = getattr(client, "socket_path", None)
    if socket_path is None:
        return client
    try:
        configured = Path(socket_path).expanduser().parent.resolve(strict=False)
    except (OSError, RuntimeError, TypeError):
        configured = None
    if configured == runtime_dir:
        return client
    return SidecarClient(runtime_dir=runtime_dir)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short plist write")
        offset += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        str(path),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _same_stored_plist(
    current: Optional[_StoredPlist],
    expected: Optional[_StoredPlist],
) -> bool:
    if current is None or expected is None:
        return current is expected
    return (
        current.digest == expected.digest
        and current.stat_signature == expected.stat_signature
        and current.payload == expected.payload
    )


def _atomic_write(
    path: Path,
    payload: bytes,
    expected: Optional[_StoredPlist],
    euid: int,
) -> None:
    descriptor = -1
    temporary: Optional[Path] = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix="." + path.name + ".",
            suffix=".tmp",
            dir=str(path.parent),
        )
        temporary = Path(name)
        os.fchmod(descriptor, 0o644)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1

        current = _read_plist(path, euid)
        if expected is None:
            if current is not None:
                raise LaunchdSecurityError(
                    "service control: service definition changed during replacement"
                )
        elif not _same_stored_plist(current, expected):
            raise LaunchdSecurityError(
                "service control: service definition changed during replacement"
            )
        os.replace(str(temporary), str(path))
        temporary = None
        _fsync_directory(path.parent)
    except LaunchdError:
        raise
    except OSError as error:
        raise LaunchdOperationError(
            "service control: cannot write the service definition"
        ) from error
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _remove_plist(path: Path, expected: _StoredPlist, euid: int) -> None:
    current = _read_plist(path, euid)
    if not _same_stored_plist(current, expected):
        raise LaunchdSecurityError(
            "service uninstall: service definition changed before removal"
        )
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as error:
        raise LaunchdOperationError(
            "service uninstall: cannot remove the service definition"
        ) from error


@contextlib.contextmanager
def _operation_lock(path: Path, euid: int) -> Iterator[None]:
    if fcntl is None:
        raise LaunchdControlError(
            "service control: filesystem locking is unavailable"
        )
    flags = (
        os.O_RDWR
        | os.O_CLOEXEC
        | os.O_NONBLOCK
        | getattr(os, "O_NOFOLLOW", 0)
    )
    created = False
    expected_identity: Optional[Tuple[int, int]] = None
    try:
        descriptor = os.open(str(path), flags | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        try:
            before = path.lstat()
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or before.st_uid != euid
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise LaunchdSecurityError("service control: unsafe operation lock")
            expected_identity = (before.st_dev, before.st_ino)
            descriptor = os.open(str(path), flags)
        except LaunchdError:
            raise
        except OSError as error:
            raise LaunchdSecurityError(
                "service control: cannot open the operation lock"
            ) from error
    except OSError as error:
        raise LaunchdSecurityError(
            "service control: cannot create the operation lock"
        ) from error
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != euid
            or (not created and stat.S_IMODE(details.st_mode) != 0o600)
            or (
                expected_identity is not None
                and (details.st_dev, details.st_ino) != expected_identity
            )
        ):
            raise LaunchdSecurityError("service control: unsafe operation lock")
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
            _fsync_directory(path.parent)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise LaunchdControlError(
                "service control: another service operation is in progress"
            ) from error
        except OSError as error:
            raise LaunchdControlError(
                "service control: cannot lock service operations"
            ) from error
        yield
    finally:
        with contextlib.suppress(OSError):
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _control_result(
    runner: Callable[..., Any],
    launchctl: Path,
    arguments: Sequence[str],
) -> Any:
    try:
        result = runner(
            (str(launchctl),) + tuple(arguments),
            input_limit=1,
            stdout_limit=MAX_CONTROL_OUTPUT,
            stderr_limit=MAX_CONTROL_OUTPUT,
            timeout=CONTROL_TIMEOUT,
            env={"PATH": PATH_VALUE},
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise LaunchdControlError(
            "service control: launchctl operation failed"
        ) from error
    if (
        getattr(result, "overflow", None) is not None
        or len(_output_bytes(result, "stdout")) > MAX_CONTROL_OUTPUT
        or len(_output_bytes(result, "stderr")) > MAX_CONTROL_OUTPUT
    ):
        raise LaunchdControlError("service control: launchctl output exceeded its bound")
    return result


def _output_bytes(result: Any, name: str) -> bytes:
    value = getattr(result, name, b"")
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    return value if isinstance(value, bytes) else b""


def _returncode(result: Any) -> int:
    try:
        return int(getattr(result, "returncode"))
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        raise LaunchdControlError(
            "service control: launchctl returned an invalid result"
        ) from error


def _is_missing_result(result: Any) -> bool:
    returncode = _returncode(result)
    if returncode in (3, 113):
        return True
    diagnostic = _output_bytes(result, "stderr").lower()
    return any(
        marker in diagnostic
        for marker in (
            b"could not find service",
            b"service not found",
            b"no such process",
        )
    )


def _brace_delta(line: str) -> int:
    delta = 0
    quote: Optional[str] = None
    escaped = False
    for character in line:
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote is not None:
            escaped = True
            continue
        if character in ("'", '"'):
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if quote is None:
            if character == "{":
                delta += 1
            elif character == "}":
                delta -= 1
    if quote is not None or escaped:
        raise LaunchdControlError(
            "service control: launchctl returned an invalid job record"
        )
    return delta


def _parse_loaded_job(result: Any, domain: str) -> _LaunchdJob:
    payload = _output_bytes(result, "stdout")
    try:
        text = payload.decode("utf-8", "strict")
    except UnicodeError as error:
        raise LaunchdControlError(
            "service control: launchctl returned an invalid job record"
        ) from error
    if "\x00" in text:
        raise LaunchdControlError(
            "service control: launchctl returned an invalid job record"
        )
    header = re.compile(
        r"^\s*{}\s*=\s*\{{\s*$".format(
            re.escape("{}/{}".format(domain, LABEL))
        )
    )
    state_pattern = re.compile(r"^\s*state\s*=\s*([A-Za-z][A-Za-z0-9 _-]{0,63})\s*$")
    pid_pattern = re.compile(r"^\s*pid\s*=\s*([1-9][0-9]{0,9})\s*$")
    states = []
    pids = []
    malformed_identity = False
    started = False
    closed = False
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not started:
            if not stripped:
                continue
            if not header.fullmatch(line):
                malformed_identity = True
                break
            started = True
            depth = 1
            continue
        if closed:
            if stripped:
                malformed_identity = True
            continue
        before = depth
        if before == 1:
            state_match = state_pattern.fullmatch(line)
            if state_match is not None:
                states.append(state_match.group(1).strip().casefold())
            elif re.match(r"^\s*state\s*=", line):
                malformed_identity = True
            pid_match = pid_pattern.fullmatch(line)
            if pid_match is not None:
                pid = int(pid_match.group(1))
                if pid > 2_147_483_647:
                    malformed_identity = True
                pids.append(pid)
            elif re.match(r"^\s*pid\s*=", line):
                malformed_identity = True
        depth += _brace_delta(line)
        if depth < 0:
            malformed_identity = True
            break
        if depth == 0:
            if stripped != "}":
                malformed_identity = True
            closed = True
    if (
        not started
        or not closed
        or depth != 0
        or malformed_identity
        or len(states) != 1
        or len(pids) > 1
    ):
        raise LaunchdControlError(
            "service control: launchctl returned an invalid job record"
        )
    pid = pids[0] if pids else None
    if states[0] == "running" and pid is None:
        raise LaunchdControlError(
            "service control: launchctl running job has no valid pid"
        )
    return _LaunchdJob(True, states[0], pid)


def _query_job(
    runner: Callable[..., Any],
    launchctl: Path,
    domain: str,
) -> _LaunchdJob:
    result = _control_result(
        runner,
        launchctl,
        ("print", "{}/{}".format(domain, LABEL)),
    )
    if _returncode(result) == 0:
        return _parse_loaded_job(result, domain)
    if _is_missing_result(result):
        return _LaunchdJob(False)
    raise LaunchdControlError("service control: cannot query the service")


def _bootstrap(
    runner: Callable[..., Any],
    launchctl: Path,
    domain: str,
    plist: Path,
) -> None:
    result = _control_result(
        runner,
        launchctl,
        ("bootstrap", domain, str(plist)),
    )
    if _returncode(result) != 0:
        raise LaunchdOperationError("service install: launchctl bootstrap failed")


def _bootout(
    runner: Callable[..., Any],
    launchctl: Path,
    domain: str,
    *,
    missing_ok: bool,
) -> None:
    result = _control_result(
        runner,
        launchctl,
        ("bootout", "{}/{}".format(domain, LABEL)),
    )
    if _returncode(result) == 0:
        return
    if missing_ok and _is_missing_result(result):
        return
    raise LaunchdOperationError("service control: launchctl bootout failed")


def _ping(client: object) -> Optional[PingInfo]:
    try:
        provider = getattr(client, "ping_info", None)
        value = provider() if callable(provider) else client.ping()  # type: ignore[attr-defined]
        if isinstance(value, PingInfo):
            return value
        return PingInfo.from_response(value)
    except (AttributeError, OSError, SidecarClientError):
        return None


def _http_matches(info: PingInfo, http: bool, port: Optional[int]) -> bool:
    if not http:
        return not info.http.enabled
    if (
        not info.http.enabled
        or info.http.host != "127.0.0.1"
        or info.http.port is None
    ):
        return False
    return port in (None, 0) or info.http.port == port


def _wait_ready(
    client: object,
    *,
    runner: Callable[..., Any],
    launchctl: Path,
    domain: str,
    http: bool,
    http_port: Optional[int],
    timeout: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> Optional[PingInfo]:
    deadline = monotonic() + timeout
    while True:
        job = _query_job(runner, launchctl, domain)
        info = _ping(client)
        if info is not None and (
            not job.running or job.pid != info.pid
        ):
            raise LaunchdOperationError(
                "service install: loaded job and daemon pid do not match"
            )
        if (
            info is not None
            and job.running
            and job.pid == info.pid
            and _http_matches(info, http, http_port)
        ):
            return info
        if monotonic() >= deadline:
            return None
        sleep(min(POLL_INTERVAL, max(0.0, deadline - monotonic())))


def _wait_stopped(
    client: object,
    *,
    timeout: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    deadline = monotonic() + timeout
    while True:
        if _ping(client) is None:
            return True
        if monotonic() >= deadline:
            return False
        sleep(min(POLL_INTERVAL, max(0.0, deadline - monotonic())))


def _resolve_prefix(prefix: Optional[Sequence[str]]) -> Tuple[str, ...]:
    if prefix is not None:
        return _validated_runtime_prefix(prefix)
    try:
        return _validated_runtime_prefix(resolve_runtime_prefix())
    except RuntimeCommandError as error:
        raise LaunchdControlError(
            "service control: cannot resolve the sidecar runtime command"
        ) from error


def _ready_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise LaunchdControlError(
            "service control: readiness timeout is invalid"
        ) from error
    if not math.isfinite(timeout) or timeout < 0 or timeout > 60.0:
        raise LaunchdControlError(
            "service control: readiness timeout is invalid"
        )
    return timeout


def _rollback_install(
    *,
    paths: ServicePaths,
    previous: Optional[_StoredPlist],
    previous_loaded: bool,
    runner: Callable[..., Any],
    launchctl: Path,
    domain: str,
    euid: int,
    mutation: _InstallMutation,
    desired: bytes,
) -> Optional[LaunchdOperationError]:
    failures = []
    unloaded = False
    definition_ready = previous is not None and not mutation.write_started
    if mutation.bootout_started or mutation.bootstrap_started:
        try:
            _bootout(runner, launchctl, domain, missing_ok=True)
            unloaded = True
        except BaseException:
            failures.append("unload")
    if not unloaded and (mutation.bootout_started or mutation.bootstrap_started):
        try:
            _bootout(runner, launchctl, domain, missing_ok=True)
            unloaded = True
            failures = [value for value in failures if value != "unload"]
        except BaseException:
            if "unload" not in failures:
                failures.append("unload")
    if mutation.write_started:
        try:
            current = _read_plist(paths.plist, euid)
            if previous is None:
                if current is not None:
                    if current.payload != desired:
                        raise LaunchdSecurityError(
                            "service install: rollback found an unknown definition"
                        )
                    _remove_plist(paths.plist, current, euid)
            elif current is None or current.payload != previous.payload:
                if current is not None and current.payload != desired:
                    raise LaunchdSecurityError(
                        "service install: rollback found an unknown definition"
                    )
                _atomic_write(paths.plist, previous.payload, current, euid)
            definition_ready = previous is not None
        except BaseException:
            failures.append("definition")
    if previous_loaded and previous is not None and mutation.bootout_started:
        if definition_ready:
            try:
                current = _read_plist(paths.plist, euid)
                definition_ready = (
                    current is not None
                    and current.payload == previous.payload
                    and current.document == previous.document
                )
            except BaseException:
                definition_ready = False
        if not definition_ready:
            failures.append("previous-service")
        else:
            try:
                _bootstrap(runner, launchctl, domain, paths.plist)
            except BaseException:
                failures.append("previous-service")
    if failures:
        return LaunchdOperationError(
            "service install: rollback incomplete ({})".format(
                ",".join(sorted(set(failures)))
            )
        )
    return None


def _record_rollback_error(
    original: BaseException,
    rollback_error: LaunchdOperationError,
) -> None:
    setattr(original, "launchd_rollback_error", rollback_error)
    note = str(rollback_error)
    add_note = getattr(original, "add_note", None)
    if callable(add_note):
        add_note(note)


def _service_error_result(error: LaunchdError) -> ServiceResult:
    rollback_error = getattr(error, "launchd_rollback_error", None)
    message = str(error)
    if isinstance(rollback_error, LaunchdOperationError):
        message += "; rollback incomplete"
    return ServiceResult(error.exit_code, message)


def _generic_error_result(error: BaseException) -> ServiceResult:
    message = "service control: operation failed safely"
    if isinstance(
        getattr(error, "launchd_rollback_error", None),
        LaunchdOperationError,
    ):
        message += "; rollback incomplete"
    return ServiceResult(2, message)


def _install(
    *,
    http: bool,
    http_port: Optional[int],
    force: bool,
    runner: Callable[..., Any],
    client: Optional[object],
    client_factory: Optional[Callable[[Path], object]],
    prefix: Optional[Sequence[str]],
    runtime_dir: Optional[Path],
    home: Optional[Path],
    platform: str,
    launchctl_path: Path,
    euid: int,
    ready_timeout: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> ServiceResult:
    ready_timeout = _ready_timeout(ready_timeout)
    launchctl = _ensure_supported(platform, launchctl_path)
    paths = service_paths(euid=euid, home=home)
    _prepare_paths(paths, euid, create=True)
    domain = service_domain(euid)
    selected_prefix = _resolve_prefix(prefix)
    document = build_plist(
        selected_prefix,
        runtime_dir=runtime_dir,
        http=http,
        http_port=http_port,
    )
    desired = plist_bytes(document)
    desired_runtime = Path(document["EnvironmentVariables"][RUNTIME_ENV])

    with _operation_lock(paths.lock, euid):
        previous = _read_plist(paths.plist, euid)
        previous_config = None
        if previous is not None:
            previous_config = _managed_plist(
                previous.document,
                selected_prefix,
                euid,
            )
            if previous_config is None:
                raise LaunchdSecurityError(
                    "service install: refusing to replace a foreign service definition"
                )
        job = _query_job(runner, launchctl, domain)
        if job.loaded and previous is None:
            raise LaunchdSecurityError(
                "service install: refusing to replace a loaded job "
                "without a validated restorable definition"
            )
        active_runtime = (
            previous_config.runtime_dir
            if previous_config is not None
            else desired_runtime
        )
        current_client = _client_for_runtime(
            client,
            active_runtime,
            client_factory,
        )
        desired_client = (
            current_client
            if active_runtime == desired_runtime
            else _client_for_runtime(
                client,
                desired_runtime,
                client_factory,
            )
        )
        running = _ping(current_client)
        identical = previous is not None and previous.payload == desired

        if running is not None and not job.loaded:
            raise LaunchdControlError(
                "service install: a manually started daemon is already running"
            )
        if running is not None and (
            not job.running or job.pid != running.pid
        ):
            raise LaunchdControlError(
                "service install: loaded job and daemon pid do not match"
            )
        if identical and job.loaded:
            if (
                running is not None
                and _http_matches(running, http, http_port)
            ):
                return ServiceResult(0, "service already installed and running")
            info = _wait_ready(
                current_client,
                runner=runner,
                launchctl=launchctl,
                domain=domain,
                http=http,
                http_port=http_port,
                timeout=ready_timeout,
                monotonic=monotonic,
                sleep=sleep,
            )
            if info is None:
                raise LaunchdOperationError(
                    "service install: loaded service did not become ready"
                )
            return ServiceResult(0, "service already installed and running")
        if previous is not None and not identical and not force:
            raise LaunchdControlError(
                "service install: a different definition exists; use --force"
            )
        if job.loaded and not force and not identical:
            raise LaunchdControlError(
                "service install: a different loaded service exists; use --force"
            )

        changed = previous is None or previous.payload != desired
        mutation = _InstallMutation()
        try:
            if job.loaded:
                before_bootout = _read_plist(paths.plist, euid)
                if not _same_stored_plist(before_bootout, previous):
                    raise LaunchdSecurityError(
                        "service install: definition changed before force replacement"
                    )
                mutation.bootout_started = True
                _bootout(runner, launchctl, domain, missing_ok=True)
                mutation.bootout_completed = True
            if changed:
                mutation.write_started = True
                _atomic_write(paths.plist, desired, previous, euid)
                mutation.write_completed = True
            persisted = _read_plist(paths.plist, euid)
            if (
                persisted is None
                or persisted.digest != hashlib.sha256(desired).hexdigest()
                or persisted.payload != desired
            ):
                raise LaunchdSecurityError(
                    "service install: persisted definition changed before bootstrap"
                )
            mutation.bootstrap_started = True
            _bootstrap(runner, launchctl, domain, paths.plist)
            mutation.bootstrap_completed = True
            info = _wait_ready(
                desired_client,
                runner=runner,
                launchctl=launchctl,
                domain=domain,
                http=http,
                http_port=http_port,
                timeout=ready_timeout,
                monotonic=monotonic,
                sleep=sleep,
            )
            if info is None:
                raise LaunchdOperationError(
                    "service install: daemon did not become ready"
                )
        except BaseException as original:
            if mutation.started:
                rollback_error = _rollback_install(
                    paths=paths,
                    previous=previous,
                    previous_loaded=job.loaded,
                    runner=runner,
                    launchctl=launchctl,
                    domain=domain,
                    euid=euid,
                    mutation=mutation,
                    desired=desired,
                )
                if rollback_error is not None:
                    _record_rollback_error(original, rollback_error)
            raise
    return ServiceResult(0, "service installed and running")


def install_service(
    *,
    http: bool = False,
    http_port: Optional[int] = None,
    force: bool = False,
    runner: Callable[..., Any] = run_bounded,
    client: Optional[object] = None,
    client_factory: Optional[Callable[[Path], object]] = None,
    prefix: Optional[Sequence[str]] = None,
    runtime_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    platform: Optional[str] = None,
    launchctl_path: Path = LAUNCHCTL_PATH,
    euid: Optional[int] = None,
    ready_timeout: float = READY_TIMEOUT,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ServiceResult:
    """Install and bootstrap the explicit current-user LaunchAgent."""

    try:
        selected_platform, selected_euid = _selected_platform(platform, euid)
        return _install(
            http=http,
            http_port=http_port,
            force=force,
            runner=runner,
            client=client,
            client_factory=client_factory,
            prefix=prefix,
            runtime_dir=runtime_dir,
            home=home,
            platform=selected_platform,
            launchctl_path=Path(launchctl_path),
            euid=selected_euid,
            ready_timeout=ready_timeout,
            monotonic=monotonic,
            sleep=sleep,
        )
    except LaunchdError as error:
        return _service_error_result(error)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        return _generic_error_result(error)


def _uninstall(
    *,
    runner: Callable[..., Any],
    client: Optional[object],
    client_factory: Optional[Callable[[Path], object]],
    prefix: Optional[Sequence[str]],
    runtime_dir: Optional[Path],
    home: Optional[Path],
    platform: str,
    launchctl_path: Path,
    euid: int,
    ready_timeout: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> ServiceResult:
    ready_timeout = _ready_timeout(ready_timeout)
    launchctl = _ensure_supported(platform, launchctl_path)
    paths = service_paths(euid=euid, home=home)
    paths_ready = _prepare_paths(paths, euid, create=False)
    domain = service_domain(euid)
    selected_prefix = _resolve_prefix(prefix)
    if not paths_ready:
        job = _query_job(runner, launchctl, domain)
        if job.loaded:
            raise LaunchdSecurityError(
                "service uninstall: loaded service has no validated definition"
            )
        current_client = _client_for_runtime(
            client,
            Path(_runtime_directory(runtime_dir)),
            client_factory,
        )
        if _ping(current_client) is not None:
            raise LaunchdControlError(
                "service uninstall: a manually started daemon is running"
            )
        return ServiceResult(0, "service is not installed")

    with _operation_lock(paths.lock, euid):
        job = _query_job(runner, launchctl, domain)
        stored = _read_plist(paths.plist, euid)
        if stored is None:
            if job.loaded:
                raise LaunchdSecurityError(
                    "service uninstall: loaded service has no validated definition"
                )
            current_client = _client_for_runtime(
                client,
                Path(_runtime_directory(runtime_dir)),
                client_factory,
            )
            if _ping(current_client) is not None:
                raise LaunchdControlError(
                    "service uninstall: a manually started daemon is running"
                )
            return ServiceResult(0, "service is not installed")
        stored_config = _managed_plist(
            stored.document,
            selected_prefix,
            euid,
        )
        if stored_config is None:
            raise LaunchdSecurityError(
                "service uninstall: refusing to remove a foreign service definition"
            )
        stored_client = _client_for_runtime(
            client,
            stored_config.runtime_dir,
            client_factory,
        )
        running = _ping(stored_client)
        if running is not None and not job.loaded:
            raise LaunchdControlError(
                "service uninstall: a manually started daemon is running"
            )
        if running is not None and (
            not job.running or job.pid != running.pid
        ):
            raise LaunchdControlError(
                "service uninstall: loaded job and daemon pid do not match"
            )
        if job.loaded:
            before_bootout = _read_plist(paths.plist, euid)
            if not _same_stored_plist(before_bootout, stored):
                raise LaunchdSecurityError(
                    "service uninstall: definition changed before bootout"
                )
            _bootout(runner, launchctl, domain, missing_ok=True)
        if not _wait_stopped(
            stored_client,
            timeout=ready_timeout,
            monotonic=monotonic,
            sleep=sleep,
        ):
            raise LaunchdOperationError(
                "service uninstall: daemon did not stop"
            )
        _remove_plist(paths.plist, stored, euid)
    return ServiceResult(0, "service uninstalled")


def uninstall_service(
    *,
    runner: Callable[..., Any] = run_bounded,
    client: Optional[object] = None,
    client_factory: Optional[Callable[[Path], object]] = None,
    prefix: Optional[Sequence[str]] = None,
    runtime_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    platform: Optional[str] = None,
    launchctl_path: Path = LAUNCHCTL_PATH,
    euid: Optional[int] = None,
    ready_timeout: float = READY_TIMEOUT,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ServiceResult:
    """Boot out and remove only this installation's validated plist."""

    try:
        selected_platform, selected_euid = _selected_platform(platform, euid)
        return _uninstall(
            runner=runner,
            client=client,
            client_factory=client_factory,
            prefix=prefix,
            runtime_dir=runtime_dir,
            home=home,
            platform=selected_platform,
            launchctl_path=Path(launchctl_path),
            euid=selected_euid,
            ready_timeout=ready_timeout,
            monotonic=monotonic,
            sleep=sleep,
        )
    except LaunchdError as error:
        return _service_error_result(error)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        return _generic_error_result(error)


def _status(
    *,
    runner: Callable[..., Any],
    client: Optional[object],
    client_factory: Optional[Callable[[Path], object]],
    prefix: Optional[Sequence[str]],
    runtime_dir: Optional[Path],
    home: Optional[Path],
    platform: str,
    launchctl_path: Path,
    euid: int,
) -> ServiceResult:
    launchctl = _ensure_supported(platform, launchctl_path)
    paths = service_paths(euid=euid, home=home)
    paths_ready = _prepare_paths(paths, euid, create=False)
    domain = service_domain(euid)
    job = _query_job(runner, launchctl, domain)
    if not paths_ready:
        if job.loaded:
            raise LaunchdSecurityError(
                "service status: loaded service has no validated definition"
            )
        current_client = _client_for_runtime(
            client,
            Path(_runtime_directory(runtime_dir)),
            client_factory,
        )
        if _ping(current_client) is not None:
            return ServiceResult(1, "service is unloaded; daemon is running manually")
        return ServiceResult(1, "service is unloaded")

    stored = _read_plist(paths.plist, euid)
    if stored is None:
        if job.loaded:
            raise LaunchdSecurityError(
                "service status: loaded service has no validated definition"
            )
        current_client = _client_for_runtime(
            client,
            Path(_runtime_directory(runtime_dir)),
            client_factory,
        )
        if _ping(current_client) is not None:
            return ServiceResult(1, "service is unloaded; daemon is running manually")
        return ServiceResult(1, "service is unloaded")
    selected_prefix = _resolve_prefix(prefix)
    stored_config = _managed_plist(
        stored.document,
        selected_prefix,
        euid,
    )
    if stored_config is None:
        raise LaunchdSecurityError(
            "service status: foreign service definition"
        )
    stored_client = _client_for_runtime(
        client,
        stored_config.runtime_dir,
        client_factory,
    )
    info = _ping(stored_client)
    if job.running and info is not None and job.pid == info.pid:
        return ServiceResult(0, "service is running (pid {})".format(info.pid))
    if job.loaded and info is not None:
        return ServiceResult(1, "service is degraded; daemon pid does not match")
    if job.loaded and job.running and job.pid is not None:
        # launchd hands back a live pid for the whole first scan, during which
        # the socket answers nothing. The pid is the stronger evidence.
        return ServiceResult(
            1,
            "service is running (pid {}) but the daemon is not answering "
            "yet".format(job.pid),
        )
    if job.loaded:
        return ServiceResult(1, "service is loaded but daemon is not running")
    if info is not None:
        return ServiceResult(1, "service is unloaded; daemon is running manually")
    return ServiceResult(1, "service is unloaded")


def service_status(
    *,
    runner: Callable[..., Any] = run_bounded,
    client: Optional[object] = None,
    client_factory: Optional[Callable[[Path], object]] = None,
    prefix: Optional[Sequence[str]] = None,
    runtime_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    platform: Optional[str] = None,
    launchctl_path: Path = LAUNCHCTL_PATH,
    euid: Optional[int] = None,
) -> ServiceResult:
    """Return sanitized combined launchd and daemon health."""

    try:
        selected_platform, selected_euid = _selected_platform(platform, euid)
        return _status(
            runner=runner,
            client=client,
            client_factory=client_factory,
            prefix=prefix,
            runtime_dir=runtime_dir,
            home=home,
            platform=selected_platform,
            launchctl_path=Path(launchctl_path),
            euid=selected_euid,
        )
    except LaunchdError as error:
        return _service_error_result(error)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        return _generic_error_result(error)


__all__ = [
    "LABEL",
    "LAUNCHCTL_PATH",
    "LOCK_NAME",
    "PATH_VALUE",
    "PLIST_NAME",
    "LaunchdControlError",
    "LaunchdError",
    "LaunchdOperationError",
    "LaunchdSecurityError",
    "ServicePaths",
    "ServiceResult",
    "build_plist",
    "build_program_arguments",
    "install_service",
    "plist_bytes",
    "service_domain",
    "service_paths",
    "service_status",
    "uninstall_service",
]
