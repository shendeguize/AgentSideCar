"""Canonical SidecarDaemon API and bounded normalized-event fanout."""

from __future__ import annotations

import errno
import json
import os
import queue
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sidecar import __version__
from sidecar import bus
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


def daemon_is_running(runtime_dir: Optional[Path] = None) -> bool:
    root = default_runtime_dir() if runtime_dir is None else Path(runtime_dir).expanduser()
    path = root / SOCKET_NAME
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode) and _socket_is_live(path)


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
        tail_recent_seconds: float = DEFAULT_TAIL_RECENT_SECONDS,
        max_event_polls: int = DEFAULT_EVENT_POLLS,
        tailer_factory: Optional[TailerFactory] = None,
    ) -> None:
        if min(active_interval, idle_interval, max_idle_interval) <= 0:
            raise ValueError("scan intervals must be positive")
        if max_idle_interval < idle_interval:
            raise ValueError("max idle interval must not be smaller than idle interval")
        if idle_backoff < 1.0:
            raise ValueError("idle backoff must be at least one")
        if client_timeout <= 0 or request_bytes <= 0:
            raise ValueError("client bounds must be positive")
        if tail_recent_seconds < 0 or max_event_polls <= 0:
            raise ValueError("tail bounds are invalid")

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
        self._tailer_pool = TailerPool(
            self._publish_event,
            tail_recent_seconds=tail_recent_seconds,
            max_event_polls=max_event_polls,
            tailer_factory=tailer_factory,
        )

        self.ready = threading.Event()
        self._stop = threading.Event()
        self._scan_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._snapshot: Tuple[Dict[str, Any], ...] = ()
        self._scan_errors: Tuple[Dict[str, Any], ...] = ()
        self._interval_lock = threading.Lock()
        self._current_interval = self.idle_interval
        self._listener: Optional[socket.socket] = None
        self._socket_identity: Optional[Tuple[int, int]] = None
        self._pidfile_owned = False
        self._scan_thread: Optional[threading.Thread] = None
        self._client_threads: Set[threading.Thread] = set()
        self._client_sockets: Set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self._serving_lock = threading.Lock()
        self._serving = False

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

    def wait_until_ready(self, timeout: Optional[float] = None) -> bool:
        return self.ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

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
            previous_keys = self.index.keys()
            try:
                discovered = list(self.scanner.scan())
            except Exception as error:
                with self._snapshot_lock:
                    self._scan_errors = (self._serialize_scan_error(error),)
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
            working = any(session.status == Status.WORKING for session in sessions)
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
            os.chmod(str(self.runtime_dir), 0o700)
        except OSError:
            pass

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
            os.write(descriptor, "{}\n".format(os.getpid()).encode("ascii"))
        finally:
            os.close(descriptor)
        self._pidfile_owned = True

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
        except OSError as error:
            listener.close()
            if error.errno == errno.EADDRINUSE:
                raise DaemonAlreadyRunning("daemon socket is already in use")
            raise
        return listener

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
        return {
            "ok": True,
            "op": "status",
            "sessions": sessions,
            "scan_errors": errors,
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
            pid_text = self.pidfile_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return
        if pid_text == str(os.getpid()):
            try:
                self.pidfile_path.unlink()
            except OSError:
                pass
        self._pidfile_owned = False

    def serve_forever(
        self,
        stop_event: Optional[threading.Event] = None,
    ) -> None:
        """Run in the foreground until either stop event is set."""

        with self._serving_lock:
            if self._serving:
                raise DaemonAlreadyRunning("this daemon instance is already serving")
            self._serving = True
        self._stop.clear()
        self.ready.clear()
        self._socket_identity = None
        self._pidfile_owned = False
        with self._scan_lock:
            self.index = IncrementalIndex()
            self._tailer_pool.reset()
        with self._snapshot_lock:
            self._snapshot = ()
            self._scan_errors = ()
        self.event_bus = bus.EventBus(self.event_bus.queue_size)
        try:
            self._prepare_runtime_paths()
            self._listener = self._bind_listener()
            initial_working, _ = self._scan_once(initial=True)
            self._scan_thread = threading.Thread(
                target=self._scan_loop,
                args=(stop_event, initial_working),
                name="agent-sidecar-scan",
                daemon=True,
            )
            self._scan_thread.start()
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
        finally:
            self._stop.set()
            self.ready.clear()
            listener = self._listener
            self._listener = None
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            self.event_bus.close()
            self._close_clients()
            if (
                self._scan_thread is not None
                and self._scan_thread is not threading.current_thread()
            ):
                self._scan_thread.join(timeout=2.0)
            self._scan_thread = None
            self._remove_owned_paths()
            with self._serving_lock:
                self._serving = False


def run_foreground(
    stop_event: Optional[threading.Event] = None,
    **kwargs: Any
) -> None:
    SidecarDaemon(**kwargs).serve_forever(stop_event)


__all__ = [
    "DaemonAlreadyRunning",
    "DaemonError",
    "RuntimePathError",
    "SidecarDaemon",
    "daemon_is_running",
    "default_runtime_dir",
    "read_pid",
    "run_foreground",
]
