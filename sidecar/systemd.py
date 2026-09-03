"""Explicit Linux current-user systemd service lifecycle for the daemon.

The service is deliberately a user unit.  It never writes /etc, invokes sudo,
or changes user lingering.  The daemon remains a foreground process and
systemd owns restart and process-group cleanup.
"""

from __future__ import annotations

import contextlib
import math
import os
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - unavailable on non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

from sidecar.client import PingInfo, SidecarClient, SidecarClientError
from sidecar.daemon import RUNTIME_ENV, default_runtime_dir
from sidecar.process_runner import run_bounded
from sidecar.runtime_cmd import (
    RuntimeCommandError,
    resolve_runtime_prefix,
    validate_runtime_prefix,
)

from sidecar.launchd import ServiceResult

UNIT_NAME = "agent-sidecar.service"
SYSTEMCTL_PATH = Path("/usr/bin/systemctl")
PATH_VALUE = "/usr/local/bin:/usr/bin:/bin"
CONTROL_TIMEOUT = 10.0
READY_TIMEOUT = 5.0
STOP_TIMEOUT = 10.0
POLL_INTERVAL = 0.1
MAX_CONTROL_OUTPUT = 64 * 1024
MAX_UNIT_BYTES = 64 * 1024
LOCK_NAME = ".agent-sidecar.service.lock"


class SystemdError(RuntimeError):
    """Base class for fixed, terminal-safe service errors."""

    exit_code = 1


class SystemdControlError(SystemdError):
    """The platform, systemd, or requested transition is invalid."""

    exit_code = 2


class SystemdSecurityError(SystemdControlError):
    """A service filesystem object failed ownership or type validation."""


class SystemdOperationError(SystemdError):
    """A bounded systemd operation failed."""


@dataclass(frozen=True)
class ServicePaths:
    home: Path
    config: Path
    units: Path
    unit: Path
    lock: Path


@dataclass(frozen=True)
class _UnitState:
    loaded: bool
    active: bool = False
    main_pid: Optional[int] = None


@dataclass(frozen=True)
class _StoredUnit:
    payload: bytes
    identity: Tuple[int, int]


def _effective_uid() -> int:
    provider = getattr(os, "geteuid", None)
    if not callable(provider):
        raise SystemdControlError("service control requires a POSIX user identity")
    uid = provider()
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise SystemdControlError("service control: invalid user identity")
    return uid


def _selected_platform(platform: Optional[str]) -> str:
    selected = sys.platform if platform is None else platform
    if not selected.startswith("linux"):
        raise SystemdControlError(
            "service control is supported on Linux with systemd --user"
        )
    return selected


def _safe_path(value: Path, *, name: str) -> Path:
    candidate = Path(value).expanduser()
    rendered = str(candidate)
    try:
        encoded = os.fsencode(rendered)
    except (UnicodeError, ValueError) as error:
        raise SystemdSecurityError(
            "service control: {} path is invalid".format(name)
        ) from error
    if (
        not candidate.is_absolute()
        or not rendered
        or len(encoded) > 4096
        or "\x00" in rendered
        or any(ord(character) < 32 or ord(character) == 127 for character in rendered)
    ):
        raise SystemdSecurityError("service control: {} path is invalid".format(name))
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise SystemdSecurityError(
            "service control: {} path is invalid".format(name)
        ) from error
    return resolved


def _validate_directory(path: Path, euid: int, *, name: str) -> None:
    try:
        details = path.lstat()
    except OSError as error:
        raise SystemdSecurityError(
            "service control: cannot inspect the {} directory".format(name)
        ) from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != euid
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        raise SystemdSecurityError(
            "service control: unsafe {} directory".format(name)
        )


def _prepare_paths(paths: ServicePaths, euid: int, *, create: bool) -> bool:
    _validate_directory(paths.home, euid, name="home")
    for directory, name in (
        (paths.config, "config"),
        (paths.config / "systemd", "systemd"),
        (paths.units, "unit"),
    ):
        try:
            directory.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                os.mkdir(str(directory), 0o700)
                os.chmod(str(directory), 0o700)
            except OSError as error:
                raise SystemdSecurityError(
                    "service control: cannot create the {} directory".format(name)
                ) from error
        except OSError as error:
            raise SystemdSecurityError(
                "service control: cannot inspect the {} directory".format(name)
            ) from error
        _validate_directory(directory, euid, name=name)
    return True


def service_paths(
    *,
    euid: Optional[int] = None,
    home: Optional[Path] = None,
    config_home: Optional[Path] = None,
) -> ServicePaths:
    uid = _effective_uid() if euid is None else euid
    if not isinstance(uid, int) or isinstance(uid, bool) or uid < 0:
        raise SystemdControlError("service control: invalid user identity")
    root = _safe_path(
        Path.home() if home is None else Path(home),
        name="account home",
    )
    if config_home is None:
        configured = os.environ.get("XDG_CONFIG_HOME")
        config = (
            _safe_path(Path(configured), name="XDG_CONFIG_HOME")
            if configured
            else root / ".config"
        )
    else:
        config = _safe_path(Path(config_home), name="config home")
    units = config / "systemd" / "user"
    return ServicePaths(
        home=root,
        config=config,
        units=units,
        unit=units / UNIT_NAME,
        lock=units / LOCK_NAME,
    )


def _quote(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SystemdSecurityError("service install: unit argument is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SystemdSecurityError("service install: unit argument is invalid")
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return '"{}"'.format(escaped)


def _runtime_directory(value: Optional[Path]) -> str:
    selected = default_runtime_dir() if value is None else Path(value).expanduser()
    return str(_safe_path(selected, name="runtime directory").resolve(strict=False))


def _validated_runtime_prefix(prefix: Sequence[str]) -> Tuple[str, ...]:
    try:
        arguments = validate_runtime_prefix(prefix)
    except RuntimeCommandError as error:
        raise SystemdSecurityError(
            "service install: runtime command is invalid"
        ) from error
    if any(
        any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in arguments
    ):
        raise SystemdSecurityError("service install: runtime command is invalid")
    return arguments


def _resolve_prefix(prefix: Optional[Sequence[str]]) -> Tuple[str, ...]:
    if prefix is not None:
        return _validated_runtime_prefix(prefix)
    try:
        return _validated_runtime_prefix(resolve_runtime_prefix())
    except RuntimeCommandError as error:
        raise SystemdControlError(
            "service control: cannot resolve the sidecar runtime command"
        ) from error


def _ready_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise SystemdControlError("service control: readiness timeout is invalid") from error
    if not math.isfinite(timeout) or timeout < 0 or timeout > 60.0:
        raise SystemdControlError("service control: readiness timeout is invalid")
    return timeout


def build_unit(
    prefix: Sequence[str],
    *,
    runtime_dir: Optional[Path] = None,
    http: bool = False,
    http_port: Optional[int] = None,
) -> bytes:
    arguments = _validated_runtime_prefix(prefix)
    if http_port is not None and not http:
        raise SystemdControlError("service install: --http-port requires --http")
    if (
        http_port is not None
        and (
            not isinstance(http_port, int)
            or isinstance(http_port, bool)
            or not 0 <= http_port <= 65535
        )
    ):
        raise SystemdControlError("service install: HTTP port is invalid")
    command = list(arguments) + ["daemon", "run"]
    if http:
        command.append("--http")
        if http_port is not None:
            command.extend(["--http-port", str(http_port)])
    runtime = _runtime_directory(runtime_dir)
    lines = [
        "[Unit]",
        "Description=Agent Sidecar daemon",
        "After=default.target",
        "StartLimitIntervalSec=60s",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(_quote(value) for value in command),
        "Restart=on-failure",
        "RestartSec=2s",
        "KillMode=control-group",
        "TimeoutStopSec=35s",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ReadWritePaths=" + _quote(runtime),
        "UMask=0077",
        "Environment=" + _quote("{}={}".format(RUNTIME_ENV, runtime)),
        "Environment=" + _quote("PATH={}".format(PATH_VALUE)),
        "StandardOutput=journal",
        "StandardError=journal",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    payload = "\n".join(lines).encode("utf-8")
    if len(payload) > MAX_UNIT_BYTES:
        raise SystemdControlError("service install: service definition is too large")
    return payload


def _read_unit(path: Path, euid: int) -> Optional[_StoredUnit]:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SystemdSecurityError(
            "service control: cannot inspect the service definition"
        ) from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != euid
        or stat.S_IMODE(details.st_mode) != 0o644
        or details.st_size > MAX_UNIT_BYTES
    ):
        raise SystemdSecurityError("service control: unsafe service definition")
    try:
        payload = path.read_bytes()
    except (OSError, ValueError) as error:
        raise SystemdSecurityError(
            "service control: cannot read the service definition"
        ) from error
    if len(payload) != details.st_size or len(payload) > MAX_UNIT_BYTES:
        raise SystemdSecurityError(
            "service control: service definition changed during validation"
        )
    after = path.lstat()
    if (
        (after.st_dev, after.st_ino, after.st_uid, after.st_size)
        != (details.st_dev, details.st_ino, details.st_uid, details.st_size)
    ):
        raise SystemdSecurityError(
            "service control: service definition changed during validation"
        )
    return _StoredUnit(payload, (details.st_dev, details.st_ino))


def _atomic_write(path: Path, payload: bytes, expected: Optional[_StoredUnit]) -> None:
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
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        current = _read_unit(path, os.geteuid())
        if expected is None:
            if current is not None:
                raise SystemdSecurityError(
                    "service install: service definition changed during replacement"
                )
        elif current is None or current.payload != expected.payload:
            raise SystemdSecurityError(
                "service install: service definition changed during replacement"
            )
        os.replace(str(temporary), str(path))
        temporary = None
    except SystemdError:
        raise
    except OSError as error:
        raise SystemdOperationError(
            "service control: cannot write the service definition"
        ) from error
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _same_unit(actual: Optional[_StoredUnit], payload: bytes) -> bool:
    return actual is not None and actual.payload == payload


def _looks_managed(payload: bytes) -> bool:
    required = (
        b"[Unit]",
        b"Description=Agent Sidecar daemon\n",
        b"[Service]",
        b"Type=simple\n",
        b"Restart=on-failure\n",
        b"KillMode=control-group\n",
        b"[Install]",
        b"WantedBy=default.target\n",
    )
    return all(marker in payload for marker in required)


def _output(result: Any, name: str) -> bytes:
    value = getattr(result, name, b"")
    if isinstance(value, str):
        return value.encode("utf-8", "replace")
    return value if isinstance(value, bytes) else b""


def _returncode(result: Any) -> int:
    try:
        return int(getattr(result, "returncode"))
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        raise SystemdControlError("service control: systemctl returned invalid status") from error


def _control(
    runner: Callable[..., Any],
    systemctl: Path,
    arguments: Sequence[str],
) -> Any:
    try:
        result = runner(
            (str(systemctl), "--user") + tuple(arguments),
            input_limit=1,
            stdout_limit=MAX_CONTROL_OUTPUT,
            stderr_limit=MAX_CONTROL_OUTPUT,
            timeout=CONTROL_TIMEOUT,
            env={"PATH": PATH_VALUE},
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise SystemdControlError("service control: systemctl operation failed") from error
    if (
        getattr(result, "overflow", None) is not None
        or len(_output(result, "stdout")) > MAX_CONTROL_OUTPUT
        or len(_output(result, "stderr")) > MAX_CONTROL_OUTPUT
    ):
        raise SystemdControlError("service control: systemctl output exceeded its bound")
    return result


def _ensure_supported(platform: str, systemctl_path: Path) -> Path:
    _selected_platform(platform)
    try:
        resolved = systemctl_path.resolve(strict=True)
        details = resolved.stat()
    except (OSError, RuntimeError) as error:
        raise SystemdControlError(
            "service control requires the systemctl executable"
        ) from error
    if (
        not resolved.is_absolute()
        or not stat.S_ISREG(details.st_mode)
        or not os.access(str(resolved), os.X_OK)
    ):
        raise SystemdControlError("service control requires the systemctl executable")
    return resolved


def _missing(result: Any) -> bool:
    if _returncode(result) in (1, 3, 4):
        diagnostic = (_output(result, "stderr") + _output(result, "stdout")).lower()
        return not diagnostic or any(
            marker in diagnostic
            for marker in (b"not-found", b"not found", b"could not be found")
        )
    return False


def _parse_state(result: Any) -> _UnitState:
    if _returncode(result) != 0:
        if _missing(result):
            return _UnitState(False)
        raise SystemdControlError("service status: cannot query the service")
    try:
        text = _output(result, "stdout").decode("utf-8", "strict")
    except UnicodeError as error:
        raise SystemdControlError("service status: invalid systemctl response") from error
    values = {}
    for line in text.splitlines():
        if "=" not in line:
            raise SystemdControlError("service status: invalid systemctl response")
        key, value = line.split("=", 1)
        if key not in {"LoadState", "ActiveState", "SubState", "MainPID"}:
            continue
        values[key] = value
    if values.get("LoadState") != "loaded":
        return _UnitState(False)
    try:
        pid = int(values.get("MainPID", "0"))
    except ValueError as error:
        raise SystemdControlError("service status: invalid systemctl pid") from error
    if pid < 0 or pid > 2_147_483_647:
        raise SystemdControlError("service status: invalid systemctl pid")
    return _UnitState(
        True,
        values.get("ActiveState") == "active"
        and values.get("SubState") == "running",
        pid or None,
    )


def _query(
    runner: Callable[..., Any],
    systemctl: Path,
) -> _UnitState:
    result = _control(
        runner,
        systemctl,
        (
            "show",
            UNIT_NAME,
            "--no-pager",
            "--property=LoadState,ActiveState,SubState,MainPID",
        ),
    )
    return _parse_state(result)


def _ping(client: object) -> Optional[PingInfo]:
    try:
        provider = getattr(client, "ping_info", None)
        value = provider() if callable(provider) else client.ping()  # type: ignore[attr-defined]
        if isinstance(value, PingInfo):
            return value
        return PingInfo.from_response(value)
    except (AttributeError, OSError, SidecarClientError):
        return None


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
    return client if configured == runtime_dir else SidecarClient(runtime_dir=runtime_dir)


def _wait_ready(
    client: object,
    *,
    runner: Callable[..., Any],
    systemctl: Path,
    timeout: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> Optional[PingInfo]:
    deadline = monotonic() + timeout
    while True:
        state = _query(runner, systemctl)
        info = _ping(client)
        if info is not None and (not state.active or state.main_pid != info.pid):
            raise SystemdOperationError(
                "service install: systemd job and daemon pid do not match"
            )
        if info is not None and state.active and state.main_pid == info.pid:
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


@contextlib.contextmanager
def _operation_lock(path: Path, euid: int) -> Iterator[None]:
    if fcntl is None:
        raise SystemdControlError("service control: filesystem locking is unavailable")
    descriptor = -1
    try:
        descriptor = os.open(
            str(path),
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != euid
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise SystemdSecurityError("service control: unsafe operation lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise SystemdControlError(
            "service control: another service operation is in progress"
        ) from error
    except OSError as error:
        raise SystemdSecurityError("service control: cannot open the operation lock") from error
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _service_error_result(error: SystemdError) -> ServiceResult:
    return ServiceResult(error.exit_code, str(error))


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
    config_home: Optional[Path] = None,
    platform: Optional[str] = None,
    systemctl_path: Path = SYSTEMCTL_PATH,
    euid: Optional[int] = None,
    ready_timeout: float = READY_TIMEOUT,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ServiceResult:
    """Install and start the explicit current-user Linux systemd unit."""

    try:
        _selected_platform(platform)
        bounded_ready_timeout = _ready_timeout(ready_timeout)
        uid = _effective_uid() if euid is None else euid
        systemctl = _ensure_supported(sys.platform if platform is None else platform, Path(systemctl_path))
        paths = service_paths(euid=uid, home=home, config_home=config_home)
        _prepare_paths(paths, uid, create=True)
        selected_prefix = _resolve_prefix(prefix)
        desired = build_unit(
            selected_prefix,
            runtime_dir=runtime_dir,
            http=http,
            http_port=http_port,
        )
        desired_runtime = Path(_runtime_directory(runtime_dir))
        with _operation_lock(paths.lock, uid):
            previous = _read_unit(paths.unit, uid)
            if previous is not None and not _looks_managed(previous.payload):
                raise SystemdSecurityError(
                    "service install: refusing to replace a foreign service definition"
                )
            if previous is not None and not _same_unit(previous, desired) and not force:
                raise SystemdControlError(
                    "service install: a different definition exists; use --force"
                )
            state = _query(runner, systemctl)
            if previous is None and state.loaded:
                raise SystemdSecurityError(
                    "service install: refusing to replace a loaded service without a validated definition"
                )
            current_client = _client_for_runtime(client, desired_runtime, client_factory)
            running = _ping(current_client)
            if running is not None and not state.loaded:
                raise SystemdControlError(
                    "service install: a manually started daemon is already running"
                )
            if running is not None and (
                not state.active or state.main_pid != running.pid
            ):
                raise SystemdControlError(
                    "service install: systemd job and daemon pid do not match"
                )
            if _same_unit(previous, desired) and state.active and running is not None:
                return ServiceResult(0, "service already installed and running")

            old_payload = None if previous is None else previous.payload
            try:
                if state.loaded:
                    _control(runner, systemctl, ("disable", "--now", UNIT_NAME))
                _atomic_write(paths.unit, desired, previous)
                _control(runner, systemctl, ("daemon-reload",))
                enabled = _control(
                    runner,
                    systemctl,
                    ("enable", "--now", UNIT_NAME),
                )
                if _returncode(enabled) != 0:
                    raise SystemdOperationError("service install: systemctl enable failed")
                info = _wait_ready(
                    _client_for_runtime(client, desired_runtime, client_factory),
                    runner=runner,
                    systemctl=systemctl,
                    timeout=bounded_ready_timeout,
                    monotonic=monotonic,
                    sleep=sleep,
                )
                if info is None:
                    raise SystemdOperationError("service install: daemon did not become ready")
            except BaseException:
                with contextlib.suppress(BaseException):
                    _control(runner, systemctl, ("disable", "--now", UNIT_NAME))
                with contextlib.suppress(BaseException):
                    current = _read_unit(paths.unit, uid)
                    if old_payload is None:
                        if current is not None and current.payload == desired:
                            paths.unit.unlink()
                    elif current is not None and current.payload == desired:
                        _atomic_write(paths.unit, old_payload, current)
                    _control(runner, systemctl, ("daemon-reload",))
                    if old_payload is not None:
                        _control(runner, systemctl, ("enable", "--now", UNIT_NAME))
                raise
        return ServiceResult(0, "service installed and running")
    except SystemdError as error:
        return _service_error_result(error)
    except (OSError, RuntimeError, TypeError, ValueError):
        return ServiceResult(2, "service control: operation failed safely")


def uninstall_service(
    *,
    runner: Callable[..., Any] = run_bounded,
    client: Optional[object] = None,
    client_factory: Optional[Callable[[Path], object]] = None,
    prefix: Optional[Sequence[str]] = None,
    runtime_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    config_home: Optional[Path] = None,
    platform: Optional[str] = None,
    systemctl_path: Path = SYSTEMCTL_PATH,
    euid: Optional[int] = None,
    ready_timeout: float = READY_TIMEOUT,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ServiceResult:
    """Stop and remove only this installation's validated user unit."""

    del prefix
    try:
        _selected_platform(platform)
        bounded_ready_timeout = _ready_timeout(ready_timeout)
        uid = _effective_uid() if euid is None else euid
        systemctl = _ensure_supported(sys.platform if platform is None else platform, Path(systemctl_path))
        paths = service_paths(euid=uid, home=home, config_home=config_home)
        if not _prepare_paths(paths, uid, create=False):
            return ServiceResult(0, "service is not installed")
        with _operation_lock(paths.lock, uid):
            stored = _read_unit(paths.unit, uid)
            state = _query(runner, systemctl)
            if stored is None:
                if state.loaded:
                    raise SystemdSecurityError(
                        "service uninstall: loaded service has no validated definition"
                    )
                return ServiceResult(0, "service is not installed")
            runtime = _runtime_directory(runtime_dir)
            client_for_runtime = _client_for_runtime(
                client,
                Path(runtime),
                client_factory,
            )
            running = _ping(client_for_runtime)
            if running is not None and state.loaded and (
                not state.active or state.main_pid != running.pid
            ):
                raise SystemdControlError(
                    "service uninstall: systemd job and daemon pid do not match"
                )
            if running is not None and not state.loaded:
                raise SystemdControlError(
                    "service uninstall: a manually started daemon is running"
                )
            if state.loaded:
                result = _control(runner, systemctl, ("disable", "--now", UNIT_NAME))
                if _returncode(result) != 0:
                    raise SystemdOperationError("service uninstall: systemctl disable failed")
            if not _wait_stopped(
                client_for_runtime,
                timeout=bounded_ready_timeout,
                monotonic=monotonic,
                sleep=sleep,
            ):
                raise SystemdOperationError("service uninstall: daemon did not stop")
            current = _read_unit(paths.unit, uid)
            if current is None or current.payload != stored.payload:
                raise SystemdSecurityError(
                    "service uninstall: service definition changed before removal"
                )
            paths.unit.unlink()
            _control(runner, systemctl, ("daemon-reload",))
        return ServiceResult(0, "service uninstalled")
    except SystemdError as error:
        return _service_error_result(error)
    except (OSError, RuntimeError, TypeError, ValueError):
        return ServiceResult(2, "service control: operation failed safely")


def service_status(
    *,
    runner: Callable[..., Any] = run_bounded,
    client: Optional[object] = None,
    client_factory: Optional[Callable[[Path], object]] = None,
    prefix: Optional[Sequence[str]] = None,
    runtime_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    config_home: Optional[Path] = None,
    platform: Optional[str] = None,
    systemctl_path: Path = SYSTEMCTL_PATH,
    euid: Optional[int] = None,
) -> ServiceResult:
    """Return sanitized combined systemd and daemon health."""

    del prefix
    try:
        _selected_platform(platform)
        uid = _effective_uid() if euid is None else euid
        systemctl = _ensure_supported(sys.platform if platform is None else platform, Path(systemctl_path))
        paths = service_paths(euid=uid, home=home, config_home=config_home)
        if not _prepare_paths(paths, uid, create=False):
            info = _ping(_client_for_runtime(client, Path(_runtime_directory(runtime_dir)), client_factory))
            return (
                ServiceResult(1, "service is unloaded; daemon is running manually")
                if info is not None
                else ServiceResult(1, "service is unloaded")
            )
        stored = _read_unit(paths.unit, uid)
        state = _query(runner, systemctl)
        if stored is None:
            if state.loaded:
                raise SystemdSecurityError(
                    "service status: loaded service has no validated definition"
                )
            info = _ping(_client_for_runtime(client, Path(_runtime_directory(runtime_dir)), client_factory))
            return (
                ServiceResult(1, "service is unloaded; daemon is running manually")
                if info is not None
                else ServiceResult(1, "service is unloaded")
            )
        info = _ping(_client_for_runtime(client, Path(_runtime_directory(runtime_dir)), client_factory))
        if state.active and info is not None and state.main_pid == info.pid:
            return ServiceResult(0, "service is running (pid {})".format(info.pid))
        if state.loaded and info is not None:
            return ServiceResult(1, "service is degraded; daemon pid does not match")
        if state.loaded and state.active and state.main_pid:
            # systemd reports a live MainPID for the whole first scan, during
            # which the socket answers nothing. The pid is the stronger
            # evidence.
            return ServiceResult(
                1,
                "service is running (pid {}) but the daemon is not answering "
                "yet".format(state.main_pid),
            )
        if state.loaded:
            return ServiceResult(1, "service is loaded but daemon is not running")
        if info is not None:
            return ServiceResult(1, "service is unloaded; daemon is running manually")
        return ServiceResult(1, "service is unloaded")
    except SystemdError as error:
        return _service_error_result(error)
    except (OSError, RuntimeError, TypeError, ValueError):
        return ServiceResult(2, "service control: operation failed safely")


__all__ = [
    "LOCK_NAME",
    "PATH_VALUE",
    "ServicePaths",
    "ServiceResult",
    "SYSTEMCTL_PATH",
    "SystemdControlError",
    "SystemdError",
    "SystemdOperationError",
    "SystemdSecurityError",
    "UNIT_NAME",
    "build_unit",
    "install_service",
    "service_paths",
    "service_status",
    "uninstall_service",
]
