"""Canonical SidecarDaemon API and bounded normalized-event fanout."""

from __future__ import annotations

import errno
import json
import math
import os
import queue
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
    Iterable,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

from sidecar import __version__
from sidecar import bus
from sidecar.cursor_chat import default_snapshot_broker
from sidecar.daemon_log import DaemonLog, DaemonLogError
from sidecar.index import IncrementalIndex, SessionKey
from sidecar.model import Session, Status
from sidecar.scan import Scanner
from sidecar.tailer_pool import (
    DEFAULT_EVENT_POLLS,
    DEFAULT_TAIL_RECENT_SECONDS,
    TailerFactory,
    TailerPool,
)

RUNTIME_ENV = "AGENT_SIDECAR_RUNTIME_DIR"
LEGACY_RUNTIME_ENV = "AGENT_SIDECAR_HOME"
SOCKET_NAME = "daemon.sock"
PIDFILE_NAME = "daemon.pid"

DEFAULT_ACTIVE_INTERVAL = 2.0
DEFAULT_IDLE_INTERVAL = 5.0
DEFAULT_MAX_IDLE_INTERVAL = 30.0
DEFAULT_REQUEST_BYTES = 64 * 1024
DEFAULT_CLIENT_TIMEOUT = 1.0
DEFAULT_SHUTDOWN_TIMEOUT = 30.0
MAX_LOG_ERROR_DEDUPE = 256
MAX_SHUTDOWN_DIAGNOSTICS = 8


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
        self._scan_errors: Tuple[Dict[str, Any], ...] = ()
        self._tail_errors: Tuple[Dict[str, Any], ...] = ()
        self._interval_lock = threading.Lock()
        self._current_interval = self.idle_interval
        self._listener: Optional[socket.socket] = None
        self._socket_identity: Optional[Tuple[int, int]] = None
        self._pidfile_owned = False
        self._pidfile_identity: Optional[Tuple[int, int]] = None
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

    def _set_snapshot(self, sessions: Iterable[Session]) -> None:
        snapshot = tuple(session.to_dict() for session in sessions)
        errors = tuple(
            self._serialize_scan_error(error)
            for error in (getattr(self.scanner, "errors", ()) or ())
        )
        with self._snapshot_lock:
            self._snapshot = snapshot
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
                sessions = self.index.sessions()
                self._set_snapshot(sessions)
                tailers_changed = self._tailer_pool.refresh(
                    sessions,
                    changed_keys=delta.changed,
                    initial=initial,
                    now=time.time(),
                )
                self._set_tail_errors()
                working = any(
                    session.status == Status.WORKING for session in sessions
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
            errors = [dict(error) for error in self._scan_errors]
            tail_errors = [dict(error) for error in self._tail_errors]
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
        return {
            "ok": True,
            "op": "status",
            "sessions": sessions,
            "scan_errors": errors,
            "tail_errors": tail_errors,
            "diagnostics": diagnostics,
        }

    def _serve_subscription(self, connection: socket.socket) -> None:
        subscription = self.event_bus.subscribe()
        try:
            self._write_json(connection, {"ok": True, "op": "subscribe"})
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
                        elif operation == "subscribe":
                            self._serve_subscription(connection)
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
                self._log_event(
                    "shutdown",
                    timed_out=self.shutdown_timed_out,
                    durable=True,
                )
                self._close_daemon_log()
                self._remove_owned_paths()
                self._finish_run(run_done)


def run_foreground(
    stop_event: Optional[threading.Event] = None,
    **kwargs: Any
) -> None:
    SidecarDaemon(**kwargs).serve_forever(stop_event)


__all__ = [
    "DEFAULT_SHUTDOWN_TIMEOUT",
    "DaemonAlreadyRunning",
    "DaemonError",
    "RuntimePathError",
    "SidecarDaemon",
    "default_runtime_dir",
    "read_pid",
    "run_foreground",
]
