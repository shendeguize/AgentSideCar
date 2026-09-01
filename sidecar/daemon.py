"""Canonical SidecarDaemon API and bounded normalized-event fanout."""

from __future__ import annotations

import errno
import json
import math
import os
import queue
import secrets
import socket
import stat
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

from sidecar import __version__
from sidecar import bus
from sidecar.archive import (
    DEFAULT_AUTO_THRESHOLD_SECONDS,
    DEFAULT_IDLE_THRESHOLD_SECONDS,
    ArchiveError,
    ArchiveStore,
    normalize_statuses,
    normalize_targets,
    parse_duration,
    select_archivable,
    session_target,
)
from sidecar.adapters.replay import ReplayUnsupported
from sidecar.cursor_chat import default_snapshot_broker
from sidecar.daemon_log import DaemonLog, DaemonLogError
from sidecar.index import IncrementalIndex, SessionKey
from sidecar.model import Event, Session, Status
from sidecar.scan import Scanner
from sidecar.tailer_pool import (
    DEFAULT_EVENT_POLLS,
    DEFAULT_TAIL_RECENT_SECONDS,
    TailerFactory,
    TailerPool,
)

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None

RUNTIME_ENV = "AGENT_SIDECAR_RUNTIME_DIR"
LEGACY_RUNTIME_ENV = "AGENT_SIDECAR_HOME"
SOCKET_NAME = "daemon.sock"
PIDFILE_NAME = "daemon.pid"
LOCKFILE_NAME = "daemon.lock"

DEFAULT_ACTIVE_INTERVAL = 2.0
DEFAULT_IDLE_INTERVAL = 5.0
DEFAULT_MAX_IDLE_INTERVAL = 30.0
DEFAULT_REQUEST_BYTES = 64 * 1024
DEFAULT_CLIENT_TIMEOUT = 1.0
DEFAULT_SHUTDOWN_TIMEOUT = 30.0
MAX_LOG_ERROR_DEDUPE = 256
MAX_SHUTDOWN_DIAGNOSTICS = 8
REPLAY_DEFAULT_LIMIT = 256
REPLAY_MAX_LIMIT = 1024
ARCHIVE_TOKEN_TTL_SECONDS = 300.0
MAX_ARCHIVE_TOKENS = 16


class DaemonError(RuntimeError):
    """Base error for local daemon lifecycle failures."""


class DaemonAlreadyRunning(DaemonError):
    """Raised when another listener already owns the configured socket."""


class RuntimePathError(DaemonError):
    """Raised when a runtime path is unsafe to replace."""


def default_runtime_dir() -> Path:
    configured = os.environ.get(RUNTIME_ENV) or os.environ.get(LEGACY_RUNTIME_ENV)
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured)))
    return Path.home() / ".agent_sidecar"


def _socket_is_live(path: Path, timeout: float = 0.1) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(timeout)
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def read_pid(runtime_dir: Optional[Path] = None) -> Optional[int]:
    root = default_runtime_dir() if runtime_dir is None else Path(runtime_dir).expanduser()
    try:
        raw = (root / PIDFILE_NAME).read_text(encoding="ascii").strip()
        pid = int(raw)
    except (OSError, UnicodeError, ValueError):
        return None
    return pid if pid > 0 else None


class SidecarDaemon:
    """Orchestrate scanning, snapshots, tailer pooling, and the socket API."""

    def __init__(
        self,
        scanner: Optional[Scanner] = None,
        runtime_dir: Optional[Path] = None,
        *,
        socket_path: Optional[Path] = None,
        pidfile_path: Optional[Path] = None,
        active_interval: float = DEFAULT_ACTIVE_INTERVAL,
        idle_interval: float = DEFAULT_IDLE_INTERVAL,
        max_idle_interval: float = DEFAULT_MAX_IDLE_INTERVAL,
        idle_backoff: float = 1.5,
        subscriber_queue_size: int = bus.DEFAULT_SUBSCRIBER_QUEUE,
        client_timeout: float = DEFAULT_CLIENT_TIMEOUT,
        request_bytes: int = DEFAULT_REQUEST_BYTES,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        tail_recent_seconds: float = DEFAULT_TAIL_RECENT_SECONDS,
        max_event_polls: int = DEFAULT_EVENT_POLLS,
        tailer_factory: Optional[TailerFactory] = None,
        http_port: Optional[int] = None,
        http_server_factory: Optional[Callable[..., Any]] = None,
        daemon_log_factory: Optional[Callable[..., Any]] = None,
        archive_store: Optional[ArchiveStore] = None,
        auto_archive: bool = False,
        auto_archive_after: float = DEFAULT_AUTO_THRESHOLD_SECONDS,
    ) -> None:
        if min(active_interval, idle_interval, max_idle_interval) <= 0:
            raise ValueError("scan intervals must be positive")
        if max_idle_interval < idle_interval:
            raise ValueError("max idle interval must not be smaller than idle interval")
        if idle_backoff < 1.0:
            raise ValueError("idle backoff must be at least one")
        try:
            bounded_shutdown_timeout = float(shutdown_timeout)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("shutdown timeout is invalid") from error
        if (
            client_timeout <= 0
            or request_bytes <= 0
            or not math.isfinite(bounded_shutdown_timeout)
            or bounded_shutdown_timeout <= 0
        ):
            raise ValueError("daemon bounds are invalid")
        if tail_recent_seconds < 0 or max_event_polls <= 0:
            raise ValueError("tail bounds are invalid")
        if (
            http_port is not None
            and (
                not isinstance(http_port, int)
                or isinstance(http_port, bool)
                or not 0 <= http_port <= 65535
            )
        ):
            raise ValueError("HTTP port must be an integer from 0 through 65535")

        configured_root = (
            default_runtime_dir()
            if runtime_dir is None
            else Path(runtime_dir).expanduser()
        )
        if socket_path is not None and runtime_dir is None:
            configured_root = Path(socket_path).expanduser().parent
        self.runtime_dir = configured_root
        self.socket_path = (
            self.runtime_dir / SOCKET_NAME
            if socket_path is None
            else Path(socket_path).expanduser()
        )
        self.pidfile_path = (
            self.runtime_dir / PIDFILE_NAME
            if pidfile_path is None
            else Path(pidfile_path).expanduser()
        )
        self.lockfile_path = self.runtime_dir / LOCKFILE_NAME
        self.archive_store = (
            ArchiveStore(self.runtime_dir) if archive_store is None else archive_store
        )
        self.auto_archive = bool(auto_archive)
        self.auto_archive_after = parse_duration(auto_archive_after)
        self.scanner = Scanner() if scanner is None else scanner
        self.index = IncrementalIndex()
        self.event_bus = bus.EventBus(subscriber_queue_size)
        self.active_interval = float(active_interval)
        self.idle_interval = float(idle_interval)
        self.max_idle_interval = float(max_idle_interval)
        self.idle_backoff = float(idle_backoff)
        self.client_timeout = float(client_timeout)
        self.request_bytes = int(request_bytes)
        self.shutdown_timeout = bounded_shutdown_timeout
        self.http_port = http_port
        self._http_server_factory = http_server_factory
        self._daemon_log_factory = daemon_log_factory
        self._tailer_pool = TailerPool(
            self._publish_event,
            tail_recent_seconds=tail_recent_seconds,
            max_event_polls=max_event_polls,
            tailer_factory=tailer_factory,
        )

        self.ready = threading.Event()
        self._stop = threading.Event()
        self._run_done = threading.Event()
        self._run_done.set()
        self._scan_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._snapshot: Tuple[Dict[str, Any], ...] = ()
        self._archived: Tuple[Dict[str, Any], ...] = ()
        self._archive_error: Optional[str] = None
        self._archive_lock = threading.Lock()
        self._archive_tokens: Dict[str, Tuple[float, FrozenSet[Tuple[str, str]]]] = {}
        self._scan_errors: Tuple[Dict[str, Any], ...] = ()
        self._tail_errors: Tuple[Dict[str, Any], ...] = ()
        self._interval_lock = threading.Lock()
        self._current_interval = self.idle_interval
        self._listener: Optional[socket.socket] = None
        self._socket_identity: Optional[Tuple[int, int]] = None
        self._pidfile_owned = False
        self._pidfile_identity: Optional[Tuple[int, int]] = None
        self._runtime_lock_fd: Optional[int] = None
        self._scan_thread: Optional[threading.Thread] = None
        self._client_threads: Set[threading.Thread] = set()
        self._client_sockets: Set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._serving_lock = threading.Lock()
        self._serving = False
        self._serve_thread: Optional[threading.Thread] = None
        self._shutdown_timed_out = False
        self._shutdown_diagnostics: Tuple[str, ...] = ()
        self._http_server: Optional[Any] = None
        self._http_bound_port = 0
        self._daemon_log: Optional[Any] = None
        self._logging_disabled = False
        self._log_error: Optional[str] = None
        self._log_dedupe_lock = threading.Lock()
        self._log_dedupe_order: Deque[Tuple[str, ...]] = deque()
        self._log_dedupe_keys: Set[Tuple[str, ...]] = set()

    @property
    def current_interval(self) -> float:
        with self._interval_lock:
            return self._current_interval

    @property
    def sessions(self) -> List[Dict[str, Any]]:
        with self._snapshot_lock:
            return [dict(session) for session in self._snapshot]

    @property
    def archived_sessions(self) -> List[Dict[str, Any]]:
        with self._snapshot_lock:
            return [dict(row) for row in self._archived]

    @property
    def scan_errors(self) -> List[Dict[str, Any]]:
        with self._snapshot_lock:
            return [dict(error) for error in self._scan_errors]

    @property
    def tail_errors(self) -> List[Dict[str, Any]]:
        with self._snapshot_lock:
            return [dict(error) for error in self._tail_errors]

    @property
    def shutdown_timed_out(self) -> bool:
        with self._serving_lock:
            return self._shutdown_timed_out

    @property
    def shutdown_diagnostics(self) -> List[str]:
        with self._serving_lock:
            return list(self._shutdown_diagnostics)

    @property
    def shutdown_diagnostic(self) -> Optional[str]:
        with self._serving_lock:
            if not self._shutdown_diagnostics:
                return None
            return "; ".join(self._shutdown_diagnostics)

    @property
    def log_error(self) -> Optional[str]:
        with self._serving_lock:
            return self._log_error

    def wait_until_ready(self, timeout: Optional[float] = None) -> bool:
        return self.ready.wait(timeout)

    def stop(self, timeout: Optional[float] = None) -> bool:
        """Request shutdown and wait a bounded time for complete ownership release."""

        if timeout is None:
            wait_timeout = self.shutdown_timeout
        else:
            try:
                wait_timeout = float(timeout)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("shutdown wait is invalid") from error
            if not math.isfinite(wait_timeout) or wait_timeout < 0:
                raise ValueError("shutdown wait is invalid")
        with self._serving_lock:
            self._stop.set()
            serve_thread = self._serve_thread
            run_done = self._run_done
        if serve_thread is threading.current_thread():
            return run_done.is_set()
        return run_done.wait(wait_timeout)

    def start_in_thread(
        self,
        stop_event: Optional[threading.Event] = None,
        timeout: float = 5.0,
    ) -> threading.Thread:
        thread = threading.Thread(
            target=self.serve_forever,
            args=(stop_event,),
            name="agent-sidecar-daemon",
            daemon=True,
        )
        thread.start()
        self.wait_until_ready(timeout)
        return thread

    def _stopping(self, external: Optional[threading.Event] = None) -> bool:
        return self._stop.is_set() or (external is not None and external.is_set())

    def _wait_for_stop(
        self,
        timeout: float,
        external: Optional[threading.Event],
    ) -> bool:
        deadline = time.monotonic() + timeout
        while not self._stopping(external):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._stop.wait(min(0.1, remaining))
        return True

    @staticmethod
    def _safe_log_error_code(value: object) -> str:
        if not isinstance(value, str) or not value:
            return "log_error"
        if len(value) > 128 or not all(
            character.isascii()
            and (character.isalnum() or character in "_.:-")
            for character in value
        ):
            return "log_error"
        return value

    def _record_log_error(self, value: object) -> None:
        code = self._safe_log_error_code(value)
        with self._serving_lock:
            if self._log_error is not None:
                return
            self._log_error = code
            diagnostic = "daemon log unavailable ({})".format(code)
            if len(self._shutdown_diagnostics) < MAX_SHUTDOWN_DIAGNOSTICS:
                self._shutdown_diagnostics += (diagnostic,)
        try:
            sys.stderr.write("agent-sidecar daemon log_error: {}\n".format(code))
            sys.stderr.flush()
        except (AttributeError, OSError, ValueError):
            pass

    def _new_daemon_log(self) -> Any:
        factory = self._daemon_log_factory or DaemonLog
        return factory(self.runtime_dir, version=__version__)

    def _open_daemon_log(self) -> None:
        try:
            logger = self._new_daemon_log()
            opened = logger.open()
            self._daemon_log = logger if opened is None else opened
            self._logging_disabled = False
        except Exception as error:
            code = (
                error.code
                if isinstance(error, DaemonLogError)
                else "log_open_failed"
            )
            self._daemon_log = None
            self._record_log_error(code)

    def _log_event(
        self,
        event: str,
        *,
        durable: bool = False,
        **fields: Any
    ) -> None:
        logger = self._daemon_log
        if logger is None or self._logging_disabled:
            return
        try:
            written = logger.append(event, durable=durable, **fields)
        except Exception:
            written = False
        if written:
            return
        self._logging_disabled = True
        self._record_log_error(
            getattr(logger, "error_code", None) or "log_write_failed"
        )

    def _close_daemon_log(self) -> None:
        logger = self._daemon_log
        self._daemon_log = None
        if logger is None:
            return
        try:
            logger.close()
        except Exception:
            self._record_log_error("log_close_failed")

    def _reset_log_dedupe(self) -> None:
        with self._log_dedupe_lock:
            self._log_dedupe_order.clear()
            self._log_dedupe_keys.clear()

    def _claim_log_diagnostic(self, key: Tuple[str, ...]) -> bool:
        with self._log_dedupe_lock:
            if key in self._log_dedupe_keys:
                return False
            if len(self._log_dedupe_order) >= MAX_LOG_ERROR_DEDUPE:
                oldest = self._log_dedupe_order.popleft()
                self._log_dedupe_keys.discard(oldest)
            self._log_dedupe_order.append(key)
            self._log_dedupe_keys.add(key)
            return True

    def _log_scan_errors(self, errors: Iterable[Mapping[str, Any]]) -> None:
        for error in errors:
            adapter = str(error.get("adapter") or "unknown")[:128]
            stage = str(error.get("stage") or "unknown")[:128]
            code = str(error.get("exception_type") or "scan_error")[:256]
            key = ("scan", adapter, stage, code)
            if self._claim_log_diagnostic(key):
                self._log_event(
                    "scan_error",
                    component="scanner",
                    level="error",
                    adapter=adapter,
                    stage=stage,
                    code=code,
                    durable=True,
                )

    def _log_tail_errors(self, errors: Iterable[Mapping[str, Any]]) -> None:
        for error in errors:
            agent = str(error.get("agent") or "unknown")[:128]
            session_id = str(error.get("session_id") or "")[:512]
            code = str(error.get("code") or "tail_error")[:256]
            key = ("tail", agent, session_id, code)
            if self._claim_log_diagnostic(key):
                self._log_event(
                    "tail_error",
                    component="tailer",
                    level="error",
                    agent=agent,
                    session_id=session_id,
                    code=code,
                    durable=True,
                )

    @staticmethod
    def _serialize_scan_error(error: Any) -> Dict[str, Any]:
        return {
            "adapter": str(getattr(error, "adapter", "")),
            "stage": str(getattr(error, "stage", "")),
            "message": str(getattr(error, "message", error)),
            "exception_type": str(
                getattr(error, "exception_type", error.__class__.__name__)
            ),
            "session_id": getattr(error, "session_id", None),
        }

    def _set_snapshot(
        self,
        sessions: Iterable[Session],
        archived: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        snapshot = tuple(session.to_dict() for session in sessions)
        archived_rows = tuple(dict(row) for row in archived)
        errors = tuple(
            self._serialize_scan_error(error)
            for error in (getattr(self.scanner, "errors", ()) or ())
        )
        with self._snapshot_lock:
            self._snapshot = snapshot
            self._archived = archived_rows
            self._scan_errors = errors
        self._log_scan_errors(errors)

    def _set_tail_errors(self) -> None:
        errors = tuple(self._tailer_pool.tail_errors)
        with self._snapshot_lock:
            self._tail_errors = errors
        self._log_tail_errors(errors)

    def _retain_failed_adapter_sessions(
        self,
        discovered: List[Session],
        previous_keys: Iterable[SessionKey],
    ) -> List[Session]:
        failed_agents = {
            str(name)
            for name in (getattr(self.scanner, "failed_agent_names", ()) or ())
            if str(name)
        }
        if not failed_agents:
            return discovered

        discovered_keys = {
            (session.agent, session.session_id)
            for session in discovered
            if isinstance(session, Session)
        }
        retained = list(discovered)
        for key in previous_keys:
            if key[0] not in failed_agents or key in discovered_keys:
                continue
            session = self.index.get(key)
            if session is not None:
                retained.append(session)
        return retained

    def _publish_event(self, event: Any) -> None:
        self.event_bus.publish(event)

    def _apply_archive(
        self,
        sessions: List[Session],
    ) -> Tuple[List[Session], Tuple[Dict[str, Any], ...]]:
        """Hide archived sessions, running the opt-in auto policy first.

        A registry failure never hides a session: the snapshot degrades to
        the unfiltered scan and the reason is reported as a diagnostic.
        """

        try:
            if self.auto_archive:
                stale = select_archivable(
                    sessions,
                    idle_seconds=self.auto_archive_after,
                )
                if stale:
                    self.archive_store.archive(stale, reason="auto")
            view = self.archive_store.partition(sessions)
        except ArchiveError as error:
            with self._snapshot_lock:
                self._archive_error = error.code
            return sessions, ()
        with self._snapshot_lock:
            self._archive_error = None
        return list(view.visible), tuple(view.archived)

    def _scan_once(self, *, initial: bool = False) -> Tuple[bool, bool]:
        with self._scan_lock:
            with default_snapshot_broker().scan_generation():
                previous_keys = self.index.keys()
                try:
                    discovered = list(self.scanner.scan())
                except Exception as error:
                    serialized = self._serialize_scan_error(error)
                    with self._snapshot_lock:
                        self._scan_errors = (serialized,)
                    self._log_scan_errors((serialized,))
                    return False, False

                discovered = self._retain_failed_adapter_sessions(
                    discovered,
                    previous_keys,
                )
                delta = self.index.update(discovered)
                # The index keeps archived sessions so their detail view and
                # replay stay reachable; only the snapshot and the tailers
                # observe the archive decision.
                sessions = self.index.sessions()
                visible, archived = self._apply_archive(sessions)
                self._set_snapshot(visible, archived)
                tailers_changed = self._tailer_pool.refresh(
                    visible,
                    changed_keys=delta.changed,
                    initial=initial,
                    now=time.time(),
                )
                self._set_tail_errors()
                working = any(
                    session.status == Status.WORKING for session in visible
                )
                return working, tailers_changed

    def scan_once(self) -> Tuple[bool, bool]:
        """Run one refresh, serialized with the background scan owner."""

        return self._scan_once(initial=False)

    def _scan_loop(
        self,
        external_stop: Optional[threading.Event],
        initial_working: bool,
    ) -> None:
        interval = self.active_interval if initial_working else self.idle_interval
        while not self._stopping(external_stop):
            with self._interval_lock:
                self._current_interval = interval
            if self._wait_for_stop(interval, external_stop):
                break
            working, changed = self._scan_once()
            if working:
                interval = self.active_interval
            elif changed:
                interval = self.idle_interval
            else:
                interval = min(self.max_idle_interval, interval * self.idle_backoff)

    def _prepare_runtime_paths(self) -> None:
        for parent in {self.runtime_dir, self.socket_path.parent, self.pidfile_path.parent}:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            runtime_before = self.runtime_dir.lstat()
        except OSError as error:
            raise RuntimePathError("cannot inspect daemon runtime directory") from error
        if (
            not stat.S_ISDIR(runtime_before.st_mode)
            or stat.S_ISLNK(runtime_before.st_mode)
            or runtime_before.st_uid != os.geteuid()
        ):
            raise RuntimePathError("daemon runtime path is not a current-user directory")
        try:
            if stat.S_IMODE(runtime_before.st_mode) != 0o700:
                os.chmod(str(self.runtime_dir), 0o700)
            runtime_after = self.runtime_dir.lstat()
        except OSError as error:
            raise RuntimePathError("cannot secure daemon runtime directory") from error
        if (
            not stat.S_ISDIR(runtime_after.st_mode)
            or stat.S_ISLNK(runtime_after.st_mode)
            or runtime_after.st_uid != os.geteuid()
            or stat.S_IMODE(runtime_after.st_mode) != 0o700
            or (runtime_before.st_dev, runtime_before.st_ino)
            != (runtime_after.st_dev, runtime_after.st_ino)
        ):
            raise RuntimePathError("daemon runtime directory changed while securing it")

        self._acquire_runtime_lock()

        try:
            socket_stat = self.socket_path.lstat()
        except FileNotFoundError:
            socket_stat = None
        except OSError as error:
            raise RuntimePathError("cannot inspect daemon socket: {}".format(error))
        if socket_stat is not None:
            if not stat.S_ISSOCK(socket_stat.st_mode):
                raise RuntimePathError(
                    "refusing to replace non-socket path {}".format(self.socket_path)
                )
            if _socket_is_live(self.socket_path):
                raise DaemonAlreadyRunning(
                    "daemon socket is already accepting connections"
                )
            try:
                self.socket_path.unlink()
            except OSError as error:
                raise RuntimePathError("cannot remove stale socket: {}".format(error))

        try:
            pid_stat = self.pidfile_path.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise RuntimePathError("cannot inspect pidfile: {}".format(error))
        if not stat.S_ISREG(pid_stat.st_mode):
            raise RuntimePathError(
                "refusing to replace non-regular pidfile {}".format(self.pidfile_path)
            )
        try:
            self.pidfile_path.unlink()
        except OSError as error:
            raise RuntimePathError("cannot remove stale pidfile: {}".format(error))

    def _acquire_runtime_lock(self) -> None:
        """Claim the stable runtime lock before inspecting ownership artifacts."""

        if _fcntl is None:
            raise RuntimePathError(
                "daemon ownership locking is unsupported on this platform "
                "(fcntl required)"
            )
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(str(self.lockfile_path), flags, 0o600)
        except OSError as error:
            raise RuntimePathError("cannot open daemon ownership lock") from error
        try:
            opened = os.fstat(descriptor)
            try:
                current = self.lockfile_path.lstat()
            except OSError as error:
                raise RuntimePathError("cannot inspect daemon ownership lock") from error
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.geteuid()
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino)
                != (current.st_dev, current.st_ino)
            ):
                raise RuntimePathError("daemon ownership lock is unsafe")
            if stat.S_IMODE(opened.st_mode) != 0o600:
                os.fchmod(descriptor, 0o600)
                opened = os.fstat(descriptor)
            if stat.S_IMODE(opened.st_mode) != 0o600:
                raise RuntimePathError("daemon ownership lock is not private")
            try:
                _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise DaemonAlreadyRunning(
                        "daemon runtime is already owned"
                    ) from error
                raise RuntimePathError("cannot lock daemon runtime") from error
            current = self.lockfile_path.lstat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise RuntimePathError("daemon ownership lock changed while locking")
        except BaseException:
            os.close(descriptor)
            raise
        self._runtime_lock_fd = descriptor

    def _release_runtime_lock(self) -> None:
        """Release ownership without unlinking the stable lock inode."""

        descriptor = self._runtime_lock_fd
        self._runtime_lock_fd = None
        if descriptor is None:
            return
        try:
            os.close(descriptor)
        except OSError:
            pass

    def _write_pidfile(self) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(str(self.pidfile_path), flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            details = os.fstat(descriptor)
            self._pidfile_identity = (details.st_dev, details.st_ino)
            self._pidfile_owned = True
            payload = "{}\n".format(os.getpid()).encode("ascii")
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short pidfile write")
                offset += written
        finally:
            os.close(descriptor)

    def _bind_listener(self) -> socket.socket:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.socket_path))
            os.chmod(str(self.socket_path), 0o600)
            socket_stat = self.socket_path.lstat()
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            listener.listen(64)
            listener.settimeout(0.2)
            self._write_pidfile()
        except BaseException as error:
            listener.close()
            if (
                isinstance(error, OSError)
                and error.errno in (errno.EADDRINUSE, errno.EEXIST)
            ):
                raise DaemonAlreadyRunning("daemon socket is already in use")
            raise
        return listener

    def _new_http_server(self) -> Any:
        factory = self._http_server_factory
        if factory is None:
            from sidecar.http_server import SidecarHttpServer

            factory = SidecarHttpServer
        return factory(
            self.runtime_dir,
            self.socket_path,
            port=self.http_port,
        )

    def _start_http_server(self) -> None:
        if self.http_port is None:
            return
        try:
            server = self._new_http_server()
            self._http_server = server
            server.start()
            port = server.port
            if (
                not isinstance(port, int)
                or isinstance(port, bool)
                or not 1 <= port <= 65535
            ):
                raise DaemonError("HTTP server returned an invalid bound port")
            self._http_bound_port = port
            self._log_event(
                "http_ready",
                component="http",
                http_enabled=True,
                http_port=port,
            )
        except OSError as error:
            self._log_event(
                "http_error",
                component="http",
                level="error",
                stage="start",
                code=error.__class__.__name__,
                durable=True,
            )
            raise
        except DaemonError as error:
            self._log_event(
                "http_error",
                component="http",
                level="error",
                stage="start",
                code=error.__class__.__name__,
                durable=True,
            )
            raise
        except Exception as error:
            self._log_event(
                "http_error",
                component="http",
                level="error",
                stage="start",
                code=error.__class__.__name__,
                durable=True,
            )
            raise DaemonError("HTTP startup failed: {}".format(error)) from error

    def _record_shutdown_diagnostic(self, message: str) -> None:
        with self._serving_lock:
            self._shutdown_timed_out = True
            if len(self._shutdown_diagnostics) < MAX_SHUTDOWN_DIAGNOSTICS:
                self._shutdown_diagnostics += (message,)

    def _close_http_server(self) -> bool:
        server = self._http_server
        if server is None:
            self._http_bound_port = 0
            return True
        try:
            server.close()
        except Exception as error:
            self._log_event(
                "http_error",
                component="http",
                level="error",
                stage="shutdown",
                code=error.__class__.__name__,
                durable=True,
            )
            self._record_shutdown_diagnostic(
                "HTTP shutdown did not complete within its bound ({})".format(
                    error.__class__.__name__
                )
            )
            return False
        self._http_server = None
        self._http_bound_port = 0
        return True

    def _http_ping_payload(self) -> Dict[str, Any]:
        port = self._http_bound_port
        if port <= 0:
            return {"enabled": False}
        return {
            "enabled": True,
            "host": "127.0.0.1",
            "port": port,
        }

    @staticmethod
    def _write_json(connection: socket.socket, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        connection.sendall(encoded + b"\n")

    @staticmethod
    def _error(code: str, message: str) -> Dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message}}

    def _status_response(self) -> Dict[str, Any]:
        with self._snapshot_lock:
            sessions = [dict(session) for session in self._snapshot]
            archived = [dict(row) for row in self._archived]
            errors = [dict(error) for error in self._scan_errors]
            tail_errors = [dict(error) for error in self._tail_errors]
            archive_error = self._archive_error
        with self._serving_lock:
            log_error = self._log_error
        diagnostics = []
        if log_error is not None:
            diagnostics.append(
                {
                    "component": "daemon_log",
                    "event": "log_error",
                    "code": log_error,
                }
            )
        if archive_error is not None:
            diagnostics.append(
                {
                    "component": "archive",
                    "event": "archive_error",
                    "code": archive_error,
                }
            )
        return {
            "ok": True,
            "op": "status",
            "sessions": sessions,
            "archived": archived,
            "archive_policy": {
                "auto": self.auto_archive,
                "auto_after_seconds": self.auto_archive_after,
                "default_idle_seconds": DEFAULT_IDLE_THRESHOLD_SECONDS,
            },
            "scan_errors": errors,
            "tail_errors": tail_errors,
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _parse_subscribe_agents(
        request: Mapping[str, Any],
    ) -> Optional[FrozenSet[str]]:
        """Return the optional agents allowlist or raise ``ValueError``."""

        agents = request.get("agents")
        if agents is None:
            return None
        if (
            not isinstance(agents, list)
            or not agents
            or any(not isinstance(name, str) or not name for name in agents)
        ):
            raise ValueError(
                "agents must be a nonempty array of nonempty agent names"
            )
        return frozenset(agents)

    def _find_session(self, session_id: str) -> Optional[Session]:
        """Return the newest indexed session matching ``session_id``."""

        for session in self.index.sessions():
            if session.session_id == session_id:
                return session
        return None

    def _replay_response(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        """Return one bounded page of normalized historical events.

        The data source is the session adapter's own bounded transcript
        replay: a compressed-stream decode for ``dsh``, an ordinal-cursor
        scan of the append-only JSONL every other agent writes
        (``adapters/replay.py``). Only events still retained in the local
        transcript and carrying a ``seq`` cursor can be returned.

        ``truncated`` is true whenever more retained events may be fetched
        with the returned ``last_seq`` cursor: the page reached ``limit``,
        or the adapter reported stopping early on its own byte or time
        budget (an ``exhausted=False`` page) before the true end of the
        transcript. An early-stopped page without any cursor progress still
        reports ``truncated: false`` because re-requesting the same page
        cannot retrieve more.
        """

        session_id = request.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return self._error(
                "invalid_request",
                "session_id must be a nonempty string",
            )
        after_seq = request.get("after_seq", 0)
        if (
            isinstance(after_seq, bool)
            or not isinstance(after_seq, int)
            or after_seq < 0
        ):
            return self._error(
                "invalid_request",
                "after_seq must be a nonnegative integer",
            )
        limit = request.get("limit", REPLAY_DEFAULT_LIMIT)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= REPLAY_MAX_LIMIT
        ):
            return self._error(
                "invalid_request",
                "limit must be an integer from 1 through {}".format(
                    REPLAY_MAX_LIMIT
                ),
            )

        session = self._find_session(session_id)
        if session is None:
            return self._error(
                "unknown_session",
                "no session {!r} in the current snapshot".format(session_id),
            )

        from sidecar.adapters import registry

        adapter = registry.get(session.agent)
        replay = getattr(adapter, "replay", None)
        normalize = getattr(adapter, "normalize", None)
        if not callable(replay) or not callable(normalize):
            return self._error(
                "replay_unsupported",
                "agent {!r} has no replay-capable adapter".format(
                    session.agent
                ),
            )

        events: List[Dict[str, Any]] = []
        last_seq: Optional[int] = None
        record_count = 0
        exhausted = True
        try:
            records = replay(session, after_seq=after_seq, max_records=limit)
            # Adapters that page on their own byte/time budgets report the
            # end of the retrievable transcript explicitly (dsh returns a
            # ReplayPage); adapters without the signal keep the plain
            # record-count semantics.
            exhausted = bool(getattr(records, "exhausted", True))
            for record in records:
                record_count += 1
                if not isinstance(record, Mapping):
                    continue
                seq = record.get("seq")
                if (
                    isinstance(seq, int)
                    and not isinstance(seq, bool)
                    and (last_seq is None or seq > last_seq)
                ):
                    last_seq = seq
                for event in normalize(record, session):
                    events.append(
                        event.to_dict()
                        if isinstance(event, Event)
                        else dict(event)
                    )
        except ReplayUnsupported:
            # The adapter replays other sessions but not this transcript
            # shape (a Cursor SQLite chat store, say). That is a capability
            # answer, not a failure: the board stops asking instead of
            # painting the timeline as broken.
            return self._error(
                "replay_unsupported",
                "session {!r} has no replayable transcript".format(session_id),
            )
        except Exception as error:
            return self._error(
                "replay_failed",
                "replay failed: {}".format(error.__class__.__name__),
            )
        return {
            "ok": True,
            "op": "replay",
            "session_id": session_id,
            "agent": session.agent,
            "after_seq": after_seq,
            "events": events,
            "count": len(events),
            "last_seq": last_seq,
            "truncated": record_count >= limit
            or (not exhausted and last_seq is not None),
        }

    def _issue_archive_token(
        self,
        targets: Iterable[Tuple[str, str]],
    ) -> str:
        token = secrets.token_urlsafe(18)
        expiry = time.monotonic() + ARCHIVE_TOKEN_TTL_SECONDS
        with self._archive_lock:
            now = time.monotonic()
            live = {
                key: value
                for key, value in self._archive_tokens.items()
                if value[0] > now
            }
            if len(live) >= MAX_ARCHIVE_TOKENS:
                oldest = min(live, key=lambda key: live[key][0])
                live.pop(oldest, None)
            live[token] = (expiry, frozenset(targets))
            self._archive_tokens = live
        return token

    def _consume_archive_token(
        self,
        token: object,
    ) -> Optional[FrozenSet[Tuple[str, str]]]:
        """Return and retire a live preview token's candidate set."""

        if not isinstance(token, str) or not token:
            return None
        with self._archive_lock:
            entry = self._archive_tokens.pop(token, None)
        if entry is None or entry[0] <= time.monotonic():
            return None
        return entry[1]

    def _archive_preview_response(
        self,
        request: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Return the sessions a batch archive would hide, plus its token."""

        try:
            idle_seconds = parse_duration(
                request.get("idle_seconds", DEFAULT_IDLE_THRESHOLD_SECONDS)
            )
            statuses = normalize_statuses(request.get("statuses"))
        except ArchiveError as error:
            return self._error("invalid_request", str(error))

        with self._snapshot_lock:
            sessions = [dict(session) for session in self._snapshot]
        candidates = select_archivable(
            sessions,
            idle_seconds=idle_seconds,
            statuses=statuses,
        )
        try:
            targets = [session_target(row) for row in candidates]
        except ArchiveError as error:
            return self._error("invalid_request", str(error))
        return {
            "ok": True,
            "op": "archive_preview",
            "idle_seconds": idle_seconds,
            "statuses": list(statuses),
            "candidates": candidates,
            "count": len(candidates),
            "token": self._issue_archive_token(targets),
        }

    def _archive_apply_response(
        self,
        request: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Archive a subset of one preview's candidates."""

        raw_targets = request.get("targets")
        if not isinstance(raw_targets, list) or not raw_targets:
            return self._error(
                "invalid_request",
                "targets must be a nonempty array of {agent, session_id}",
            )
        try:
            targets = normalize_targets(raw_targets)
        except ArchiveError as error:
            return self._error("invalid_request", str(error))

        allowed = self._consume_archive_token(request.get("token"))
        if allowed is None:
            return self._error(
                "invalid_token",
                "archive_apply requires a live archive_preview token",
            )
        unexpected = [target for target in targets if target not in allowed]
        if unexpected:
            return self._error(
                "invalid_request",
                "targets must be a subset of the previewed candidates",
            )

        try:
            added = self.archive_store.archive(targets, reason="batch")
        except ArchiveError as error:
            return self._error(error.code, str(error))
        self.scan_once()
        return {
            "ok": True,
            "op": "archive_apply",
            "archived": [entry.to_dict() for entry in added],
            "count": len(added),
            "requested": len(targets),
        }

    def _unarchive_response(
        self,
        request: Mapping[str, Any],
    ) -> Dict[str, Any]:
        raw_targets = request.get("targets")
        try:
            if request.get("all") is True:
                removed = self.archive_store.unarchive_all()
            elif isinstance(raw_targets, list) and raw_targets:
                removed = self.archive_store.unarchive(
                    normalize_targets(raw_targets)
                )
            else:
                return self._error(
                    "invalid_request",
                    "provide targets or all=true",
                )
        except ArchiveError as error:
            return self._error(error.code, str(error))
        self.scan_once()
        return {
            "ok": True,
            "op": "unarchive",
            "released": [
                {"agent": agent, "session_id": session_id}
                for agent, session_id in removed
            ],
            "count": len(removed),
        }

    def _archive_list_response(self) -> Dict[str, Any]:
        try:
            entries = self.archive_store.entries()
        except ArchiveError as error:
            return self._error(error.code, str(error))
        with self._snapshot_lock:
            archived = [dict(row) for row in self._archived]
        return {
            "ok": True,
            "op": "archive_list",
            "entries": [entry.to_dict() for entry in entries],
            "sessions": archived,
            "count": len(entries),
        }

    def _serve_subscription(
        self,
        connection: socket.socket,
        agents: Optional[FrozenSet[str]] = None,
    ) -> None:
        subscription = self.event_bus.subscribe(agents=agents)
        try:
            acknowledgement: Dict[str, Any] = {"ok": True, "op": "subscribe"}
            if agents is not None:
                acknowledgement["agents"] = sorted(agents)
            self._write_json(connection, acknowledgement)
            while not self._stop.is_set():
                try:
                    event = subscription.get(timeout=0.2)
                except queue.Empty:
                    continue
                except bus.SubscriptionClosed:
                    return
                self._write_json(connection, event)
        except (BrokenPipeError, ConnectionError, OSError, ValueError):
            return
        finally:
            subscription.close()

    def _handle_client(self, connection: socket.socket) -> None:
        current = threading.current_thread()
        with self._clients_lock:
            self._client_sockets.add(connection)
        try:
            connection.settimeout(self.client_timeout)
            with connection:
                stream = connection.makefile("rwb", buffering=0)
                try:
                    while not self._stop.is_set():
                        try:
                            raw = stream.readline(self.request_bytes + 1)
                        except (OSError, ValueError):
                            return
                        if not raw:
                            return
                        if len(raw) > self.request_bytes:
                            self._write_json(
                                connection,
                                self._error(
                                    "request_too_large",
                                    "request exceeds {} bytes".format(
                                        self.request_bytes
                                    ),
                                ),
                            )
                            return
                        try:
                            request = json.loads(raw.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeError):
                            self._write_json(
                                connection,
                                self._error("malformed_json", "request is not valid JSON"),
                            )
                            continue
                        if not isinstance(request, Mapping):
                            self._write_json(
                                connection,
                                self._error("invalid_request", "request must be an object"),
                            )
                            continue
                        operation = request.get("op")
                        if operation == "ping":
                            self._write_json(
                                connection,
                                {
                                    "ok": True,
                                    "op": "ping",
                                    "pid": os.getpid(),
                                    "version": __version__,
                                    "http": self._http_ping_payload(),
                                },
                            )
                        elif operation == "status":
                            self._write_json(connection, self._status_response())
                        elif operation == "replay":
                            self._write_json(
                                connection,
                                self._replay_response(request),
                            )
                        elif operation == "archive_preview":
                            self._write_json(
                                connection,
                                self._archive_preview_response(request),
                            )
                        elif operation == "archive_apply":
                            self._write_json(
                                connection,
                                self._archive_apply_response(request),
                            )
                        elif operation == "unarchive":
                            self._write_json(
                                connection,
                                self._unarchive_response(request),
                            )
                        elif operation == "archive_list":
                            self._write_json(
                                connection,
                                self._archive_list_response(),
                            )
                        elif operation == "subscribe":
                            try:
                                agents = self._parse_subscribe_agents(request)
                            except ValueError as error:
                                self._write_json(
                                    connection,
                                    self._error("invalid_request", str(error)),
                                )
                                continue
                            self._serve_subscription(connection, agents=agents)
                            return
                        else:
                            rendered = operation if isinstance(operation, str) else ""
                            self._write_json(
                                connection,
                                self._error(
                                    "unknown_op",
                                    "unsupported operation {!r}".format(rendered),
                                ),
                            )
                finally:
                    try:
                        stream.close()
                    except OSError:
                        pass
        finally:
            with self._clients_lock:
                self._client_sockets.discard(connection)
                self._client_threads.discard(current)

    def _close_clients(self) -> None:
        with self._clients_lock:
            clients = tuple(self._client_sockets)
            threads = tuple(self._client_threads)
        for connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1.0)

    def _join_scan_thread(self) -> None:
        """Wait for the bounded scan owner before the pool can be closed."""

        thread = self._scan_thread
        if thread is None or thread is threading.current_thread():
            self._scan_thread = None
            return
        thread.join(timeout=self.shutdown_timeout)
        if thread.is_alive():
            with self._serving_lock:
                self._shutdown_timed_out = True
            # A production poll has bounded I/O. If that contract is violated,
            # keep the daemon owner alive and report timeout through stop()
            # rather than returning and racing TailerPool.close().
            while thread.is_alive():
                thread.join(timeout=min(0.1, self.shutdown_timeout))
        self._scan_thread = None

    def _remove_owned_paths(self) -> None:
        try:
            socket_stat = self.socket_path.lstat()
        except OSError:
            socket_stat = None
        if (
            socket_stat is not None
            and stat.S_ISSOCK(socket_stat.st_mode)
            and self._socket_identity
            == (socket_stat.st_dev, socket_stat.st_ino)
        ):
            try:
                self.socket_path.unlink()
            except OSError:
                pass

        if not self._pidfile_owned:
            return
        try:
            pid_stat = self.pidfile_path.lstat()
        except OSError:
            pid_stat = None
        if (
            pid_stat is not None
            and stat.S_ISREG(pid_stat.st_mode)
            and self._pidfile_identity
            == (pid_stat.st_dev, pid_stat.st_ino)
        ):
            try:
                self.pidfile_path.unlink()
            except OSError:
                pass
        self._pidfile_owned = False
        self._pidfile_identity = None

    def _finish_run(self, run_done: threading.Event) -> None:
        """Atomically publish completion for exactly one daemon generation."""

        with self._serving_lock:
            if self._run_done is run_done:
                self._serving = False
                self._serve_thread = None
            run_done.set()

    def serve_forever(
        self,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        """Run in the foreground until either stop event is set."""

        run_done = threading.Event()
        with self._serving_lock:
            if self._serving:
                raise DaemonAlreadyRunning("this daemon instance is already serving")
            self._stop.clear()
            self.ready.clear()
            self._serving = True
            self._serve_thread = threading.current_thread()
            self._shutdown_timed_out = False
            self._shutdown_diagnostics = ()
            self._log_error = None
            self._run_done = run_done
        self._socket_identity = None
        self._pidfile_owned = False
        self._pidfile_identity = None
        self._http_server = None
        self._http_bound_port = 0
        self._daemon_log = None
        self._logging_disabled = False
        self._reset_log_dedupe()
        with self._scan_lock:
            self.index = IncrementalIndex()
            self._tailer_pool.reset()
        with self._snapshot_lock:
            self._snapshot = ()
            self._scan_errors = ()
            self._tail_errors = ()
        self.event_bus = bus.EventBus(self.event_bus.queue_size)
        try:
            self._prepare_runtime_paths()
            self._listener = self._bind_listener()
            self._open_daemon_log()
            self._log_event(
                "startup",
                http_enabled=self.http_port is not None,
                http_port=0 if self.http_port is None else self.http_port,
            )
            initial_working, _ = self._scan_once(initial=True)
            self._scan_thread = threading.Thread(
                target=self._scan_loop,
                args=(stop_event, initial_working),
                name="agent-sidecar-scan",
                daemon=True,
            )
            self._scan_thread.start()
            self._start_http_server()
            self._log_event(
                "ready",
                count=len(self.sessions),
                http_enabled=self._http_bound_port > 0,
                http_port=self._http_bound_port,
            )
            self.ready.set()

            while not self._stopping(stop_event):
                try:
                    connection, _ = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stopping(stop_event):
                        break
                    raise
                thread = threading.Thread(
                    target=self._handle_client,
                    args=(connection,),
                    name="agent-sidecar-client",
                    daemon=True,
                )
                with self._clients_lock:
                    self._client_threads.add(thread)
                thread.start()
        except Exception as error:
            self._log_event(
                "daemon_error",
                level="error",
                stage="serve",
                code=error.__class__.__name__,
                durable=True,
            )
            raise
        finally:
            try:
                self._stop.set()
                self.ready.clear()
                http_closed = self._close_http_server()
                listener = self._listener
                self._listener = None
                if listener is not None:
                    try:
                        listener.close()
                    except OSError:
                        pass
                self.event_bus.close()
                self._close_clients()
                self._join_scan_thread()
                try:
                    self._tailer_pool.close()
                except Exception:
                    pass
                if not http_closed:
                    self._close_http_server()
            finally:
                try:
                    self._log_event(
                        "shutdown",
                        timed_out=self.shutdown_timed_out,
                        durable=True,
                    )
                    self._close_daemon_log()
                    self._remove_owned_paths()
                finally:
                    self._release_runtime_lock()
                    self._finish_run(run_done)


def run_foreground(
    stop_event: Optional[threading.Event] = None,
    **kwargs: Any
) -> None:
    SidecarDaemon(**kwargs).serve_forever(stop_event)


__all__ = [
    "DEFAULT_SHUTDOWN_TIMEOUT",
    "REPLAY_DEFAULT_LIMIT",
    "REPLAY_MAX_LIMIT",
    "DaemonAlreadyRunning",
    "DaemonError",
    "RuntimePathError",
    "SidecarDaemon",
    "default_runtime_dir",
    "read_pid",
    "run_foreground",
]
