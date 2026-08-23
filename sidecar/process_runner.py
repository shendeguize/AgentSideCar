"""Bounded, dependency-free subprocess execution."""

from __future__ import annotations

import errno
import json
import math
import os
import select
import selectors
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from numbers import Real
from typing import BinaryIO, Callable, Dict, Iterator, Mapping, Optional, Sequence, Set, Tuple, Union


MAX_TIMEOUT_SECONDS = 3600.0
MAX_STREAM_INPUT_BYTES = 4 * 1024 * 1024
MAX_STREAM_LINE_BYTES = 4 * 1024 * 1024
MAX_STREAM_STDERR_BYTES = 64 * 1024
_CHUNK_SIZE = 65536
_POLL_INTERVAL_SECONDS = 0.05
_SUPERVISOR_CONFIG_LIMIT = 8 * 1024 * 1024
_POSIX_EXIT_SIGNALS = tuple(
    value
    for value in (
        getattr(signal, "SIGTERM", None),
        getattr(signal, "SIGHUP", None),
    )
    if value is not None
)


def _interrupt_for_exit_signal(_signum: int, _frame: object) -> None:
    raise KeyboardInterrupt


class _SignalScope:
    """Protect child ownership across spawn and restore caller signal state."""

    def __init__(self) -> None:
        self._active = False
        self._installed_handlers: Set[signal.Signals] = set()
        self._previous_handlers = {}
        self._previous_mask: Optional[Set[signal.Signals]] = None

    def install_before_spawn(self) -> None:
        if (
            os.name != "posix"
            or threading.current_thread() is not threading.main_thread()
            or not hasattr(signal, "pthread_sigmask")
        ):
            return
        managed_signals = set(_POSIX_EXIT_SIGNALS)
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK,
            managed_signals,
        )
        previous_handlers = {}
        installed_handlers = set()
        try:
            for managed_signal in _POSIX_EXIT_SIGNALS:
                previous_handler = signal.getsignal(managed_signal)
                previous_handlers[managed_signal] = previous_handler
                if previous_handler == signal.SIG_DFL:
                    signal.signal(managed_signal, _interrupt_for_exit_signal)
                    installed_handlers.add(managed_signal)
        except BaseException:
            try:
                for managed_signal in installed_handlers:
                    signal.signal(
                        managed_signal,
                        previous_handlers[managed_signal],
                    )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            raise
        self._installed_handlers = installed_handlers
        self._previous_handlers = previous_handlers
        self._previous_mask = previous_mask
        self._active = True

    def unblock_after_spawn(self) -> None:
        if self._active:
            assert self._previous_mask is not None
            signal.pthread_sigmask(signal.SIG_SETMASK, self._previous_mask)
            if not self._installed_handlers:
                self._active = False

    def block_for_cleanup(self) -> None:
        if self._active:
            signal.pthread_sigmask(
                signal.SIG_BLOCK,
                set(_POSIX_EXIT_SIGNALS),
            )

    def restore(self) -> None:
        if not self._active:
            return
        assert self._previous_mask is not None
        try:
            for managed_signal in self._installed_handlers:
                signal.signal(
                    managed_signal,
                    self._previous_handlers[managed_signal],
                )
        finally:
            self._active = False
            signal.pthread_sigmask(signal.SIG_SETMASK, self._previous_mask)


class _ProcessGroupOwnership:
    """Serialize group signaling with ownership release and leader reaping."""

    def __init__(self, process_group_id: Optional[int]) -> None:
        self._lock = threading.Lock()
        self._process_group_id = process_group_id
        self._descendant_tracker: Optional[
            "_DarwinKqueueDescendantTracker"
        ] = None

    def attach_descendant_tracker(
        self,
        tracker: "_DarwinKqueueDescendantTracker",
    ) -> None:
        with self._lock:
            self._descendant_tracker = tracker

    def kill(self, process: subprocess.Popen) -> None:
        signaled_group = False
        with self._lock:
            if os.name == "posix" and self._process_group_id is not None:
                try:
                    os.killpg(self._process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    self._process_group_id = None
                except OSError:
                    pass
                else:
                    signaled_group = True
            if not signaled_group:
                returncode = process.poll()
                if returncode is not None:
                    self._process_group_id = None
                else:
                    try:
                        process.kill()
                    except OSError:
                        pass
            tracker = self._descendant_tracker
        if tracker is not None:
            tracker.terminate()

    def poll(self, process: subprocess.Popen) -> Optional[int]:
        with self._lock:
            returncode = process.poll()
            if returncode is not None:
                self._process_group_id = None
            return returncode

    def wait(
        self,
        process: subprocess.Popen,
        timeout: Optional[float] = None,
    ) -> int:
        with self._lock:
            returncode = process.wait(timeout=timeout)
            self._process_group_id = None
            return returncode

    def release(self) -> None:
        with self._lock:
            self._process_group_id = None


class _ProcessRegistry:
    """Thread-safe process ownership for one bounded execution scope."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._closing = False
        self._spawning = 0
        self._processes: Dict[subprocess.Popen, _ProcessGroupOwnership] = {}

    def begin_spawn(self) -> None:
        with self._condition:
            if self._closing:
                raise RuntimeError("bounded execution scope is closing")
            self._spawning += 1

    def complete_spawn(
        self,
        process: subprocess.Popen,
        ownership: _ProcessGroupOwnership,
    ) -> None:
        with self._condition:
            self._processes[process] = ownership
            self._spawning -= 1
            closing = self._closing
            self._condition.notify_all()
        if closing:
            _kill_process_group(process, ownership)

    def abort_spawn(self) -> None:
        with self._condition:
            self._spawning -= 1
            self._condition.notify_all()

    def unregister(self, process: subprocess.Popen) -> None:
        with self._condition:
            ownership = self._processes.pop(process, None)
            if ownership is not None:
                ownership.release()
            self._condition.notify_all()

    def kill_all(self) -> None:
        with self._condition:
            self._closing = True
            while self._spawning:
                self._condition.wait(_POLL_INTERVAL_SECONDS)
            processes = tuple(self._processes.items())
        for process, ownership in processes:
            _kill_process_group(process, ownership)

    def wait_empty(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._processes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(_POLL_INTERVAL_SECONDS, remaining))
            return True


_REGISTRY_STATE_LOCK = threading.Lock()
_ACTIVE_REGISTRY: Optional[_ProcessRegistry] = None


def _current_process_registry() -> Optional[_ProcessRegistry]:
    with _REGISTRY_STATE_LOCK:
        return _ACTIVE_REGISTRY


@contextmanager
def bounded_execution_signal_guard() -> Iterator[_ProcessRegistry]:
    """Protect bounded subprocesses created by main and worker threads."""

    global _ACTIVE_REGISTRY

    existing = _current_process_registry()
    if existing is not None:
        yield existing
        return

    registry = _ProcessRegistry()
    if os.name != "posix" or threading.current_thread() is not threading.main_thread():
        try:
            yield registry
        finally:
            registry.kill_all()
            registry.wait_empty()
        return

    signal_scope = _SignalScope()
    published = False
    try:
        signal_scope.install_before_spawn()
        with _REGISTRY_STATE_LOCK:
            if _ACTIVE_REGISTRY is not None:
                raise RuntimeError("bounded execution scope changed")
            _ACTIVE_REGISTRY = registry
            published = True
        signal_scope.unblock_after_spawn()
        yield registry
    finally:
        signal_scope.block_for_cleanup()
        registry.kill_all()
        registry.wait_empty()
        if published:
            with _REGISTRY_STATE_LOCK:
                if _ACTIVE_REGISTRY is registry:
                    _ACTIVE_REGISTRY = None
        signal_scope.restore()


@dataclass(frozen=True)
class BoundedProcessResult:
    """Result of a bounded subprocess execution."""

    args: Tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    overflow: Optional[str] = None
    cleanup_incomplete: bool = False


class BoundedLineStreamEndReason(str, Enum):
    """Why a bounded line stream stopped."""

    EOF = "eof"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    STARTUP_TIMEOUT = "startup_timeout"
    INPUT_OVERFLOW = "input_overflow"
    LINE_OVERFLOW = "line_overflow"
    STDERR_OVERFLOW = "stderr_overflow"
    NONZERO_EXIT = "nonzero_exit"


@dataclass(frozen=True)
class BoundedLineStreamResult:
    """Terminal state of a bounded line stream."""

    args: Tuple[str, ...]
    returncode: Optional[int]
    stderr: bytes
    end_reason: BoundedLineStreamEndReason
    lines_yielded: int
    stdout_bytes_read: int
    cleanup_incomplete: bool = False


class BoundedLineStreamError(RuntimeError):
    """Base class for typed bounded line stream failures."""

    def __init__(self, message: str, result: BoundedLineStreamResult) -> None:
        super().__init__(message)
        self.result = result


class BoundedLineStreamOverflowError(BoundedLineStreamError):
    """A configured line, input, or stderr bound was exceeded."""


class BoundedLineStreamTimeoutError(BoundedLineStreamError):
    """The stream did not become ready before its startup deadline."""


class BoundedLineStreamCancelledError(BoundedLineStreamError):
    """The stream was stopped through its cancellation event."""


class BoundedLineStreamProcessError(BoundedLineStreamError):
    """The streamed process reached EOF with a nonzero return code."""


def _validated_argv(argv: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(argv, (str, bytes)):
        raise ValueError("argv must be a nonempty sequence of strings")
    try:
        arguments = tuple(argv)
    except TypeError as error:
        raise ValueError("argv must be a nonempty sequence of strings") from error
    if not arguments or any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in arguments
    ):
        raise ValueError("argv must be a nonempty sequence of strings")
    return arguments


def _positive_limit(value: int, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("{} must be a positive integer".format(name))
    return value


def _bounded_timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("timeout must be finite and positive")
    try:
        timeout = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("timeout must be finite and positive") from error
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must be finite and positive")
    return timeout


def _working_directory(
    cwd: Optional[Union[str, os.PathLike]],
) -> Optional[str]:
    if cwd is None:
        return None
    try:
        path = os.fspath(cwd)
    except TypeError as error:
        raise TypeError("cwd must be a string or path-like value") from error
    if not isinstance(path, str):
        raise TypeError("cwd must resolve to a string path")
    return path


class DescendantContainmentUnsupportedError(RuntimeError):
    """Required deterministic descendant containment is unavailable."""


class _DarwinKqueueDescendantTracker:
    """Kernel proof of whether the gated Darwin root ever forked."""

    def __init__(self, root_pid: int) -> None:
        if not self.supported():
            raise DescendantContainmentUnsupportedError(
                "deterministic descendant containment is unsupported"
            )
        self._lock = threading.RLock()
        self._kqueue = select.kqueue()
        self._root_pid = root_pid
        self._root_live = True
        self._fork_activity = False
        self._uncertain = False
        event = select.kevent(
            root_pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT,
        )
        try:
            self._kqueue.control([event], 0, 0)
        except OSError as error:
            self._kqueue.close()
            if error.errno in (errno.ENOTSUP, errno.EINVAL):
                raise DescendantContainmentUnsupportedError(
                    "Darwin kqueue NOTE_FORK is unsupported"
                ) from None
            raise
        except BaseException:
            self._kqueue.close()
            raise

    @staticmethod
    def supported() -> bool:
        names = (
            "kqueue",
            "kevent",
            "KQ_FILTER_PROC",
            "KQ_EV_ADD",
            "KQ_EV_ENABLE",
            "KQ_EV_CLEAR",
            "KQ_NOTE_FORK",
            "KQ_NOTE_EXIT",
        )
        return sys.platform == "darwin" and all(
            hasattr(select, name) for name in names
        )

    @property
    def reliable(self) -> bool:
        with self._lock:
            return not self._uncertain

    @property
    def cleanup_incomplete(self) -> bool:
        with self._lock:
            self._drain()
            return self._uncertain or self._fork_activity

    def _drain(self) -> None:
        events = self._kqueue.control(None, 16, 0)
        if len(events) >= 16:
            self._uncertain = True
        for event in events:
            flags = event.fflags
            if flags & select.KQ_NOTE_FORK:
                self._fork_activity = True
            if flags & select.KQ_NOTE_EXIT:
                self._root_live = False

    def sample(self, *, force: bool = False) -> Tuple[int, ...]:
        del force
        with self._lock:
            self._drain()
            return (self._root_pid,) if self._root_live else ()

    def terminate(self) -> bool:
        with self._lock:
            self._drain()
            return not self._uncertain and not self._fork_activity

    def close(self) -> None:
        with self._lock:
            self._kqueue.close()


def _kill_process_group(
    process: subprocess.Popen,
    ownership: _ProcessGroupOwnership,
) -> None:
    ownership.kill(process)


def _force_cleanup_process(
    process: subprocess.Popen,
    ownership: _ProcessGroupOwnership,
) -> None:
    _kill_process_group(process, ownership)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    ownership.wait(process)


@dataclass
class _StartedProcess:
    process: subprocess.Popen
    ownership: _ProcessGroupOwnership
    lease: Optional[BinaryIO]
    descendant_tracker: Optional[_DarwinKqueueDescendantTracker]
    containment_required: bool
    signal_scope: _SignalScope
    registry: Optional[_ProcessRegistry]
    process_registered: bool
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            if self.registry is not None and self.process_registered:
                self.registry.unregister(self.process)
        finally:
            if self.descendant_tracker is not None:
                try:
                    self.descendant_tracker.close()
                except (OSError, ValueError):
                    pass
            if self.lease is not None:
                try:
                    self.lease.close()
                except (OSError, ValueError):
                    pass
            self.signal_scope.restore()


_SUPERVISOR_SOURCE = """\
import json
import os
import sys

config_fd = int(sys.argv[1])
gate_fd = int(sys.argv[2])
chunks = []
size = 0
while True:
    chunk = os.read(config_fd, 65536)
    if not chunk:
        break
    size += len(chunk)
    if size > 8388608:
        os._exit(124)
    chunks.append(chunk)
os.close(config_fd)
try:
    config = json.loads(b"".join(chunks).decode("ascii"))
except BaseException:
    os._exit(125)
gate = os.read(gate_fd, 1)
os.close(gate_fd)
if gate != b"1":
    os._exit(126)
try:
    cwd = config["cwd"]
    if cwd is not None:
        os.chdir(cwd)
    os.execvpe(config["argv"][0], config["argv"], config["env"])
except BaseException:
    os._exit(127)
"""


def _supervisor_config(
    arguments: Tuple[str, ...],
    environment: Optional[Dict[str, str]],
    working_directory: Optional[str],
) -> bytes:
    target_environment = dict(os.environ) if environment is None else environment
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or "\x00" in key
        or "=" in key
        or "\x00" in value
        for key, value in target_environment.items()
    ):
        raise ValueError("env keys and values must be valid strings")
    payload = json.dumps(
        {
            "argv": arguments,
            "cwd": working_directory,
            "env": target_environment,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(payload) > _SUPERVISOR_CONFIG_LIMIT:
        raise OSError("supervisor configuration exceeds bounded pipe limit")
    return payload


def _write_all_fd(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        try:
            written = os.write(descriptor, data[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("private supervisor pipe closed")
        offset += written


def _start_bounded_process(
    arguments: Tuple[str, ...],
    input_data: Optional[bytes],
    *,
    environment: Optional[Dict[str, str]],
    working_directory: Optional[str],
    pre_spawn: Optional[Callable[[], None]],
    pre_exec: Optional[Callable[[], None]],
    require_descendant_containment: bool,
) -> _StartedProcess:
    """Spawn once under the shared signal and registry ownership contract."""

    signal_scope = _SignalScope()
    registry: Optional[_ProcessRegistry] = None
    process: Optional[subprocess.Popen] = None
    ownership: Optional[_ProcessGroupOwnership] = None
    descendant_tracker: Optional[_DarwinKqueueDescendantTracker] = None
    lease_read_fd = -1
    lease_write_fd = -1
    config_read_fd = -1
    config_write_fd = -1
    gate_read_fd = -1
    gate_write_fd = -1
    lease: Optional[BinaryIO] = None
    spawn_pending = False
    process_registered = False
    if (
        require_descendant_containment
        and not _DarwinKqueueDescendantTracker.supported()
    ):
        raise DescendantContainmentUnsupportedError(
            "deterministic descendant containment is unsupported"
        )
    signal_scope.install_before_spawn()
    try:
        registry = _current_process_registry()
        if registry is not None:
            registry.begin_spawn()
            spawn_pending = True
        if pre_spawn is not None:
            pre_spawn()
        if os.name == "posix":
            lease_read_fd, lease_write_fd = os.pipe()
        if require_descendant_containment:
            config = _supervisor_config(
                arguments,
                environment,
                working_directory,
            )
            config_read_fd, config_write_fd = os.pipe()
            gate_read_fd, gate_write_fd = os.pipe()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _SUPERVISOR_SOURCE,
                    str(config_read_fd),
                    str(gate_read_fd),
                ],
                stdin=(
                    subprocess.PIPE
                    if input_data is not None
                    else subprocess.DEVNULL
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                cwd=None,
                shell=False,
                start_new_session=True,
                pass_fds=(
                    lease_write_fd,
                    config_read_fd,
                    gate_read_fd,
                ),
            )
            os.close(config_read_fd)
            config_read_fd = -1
            os.close(gate_read_fd)
            gate_read_fd = -1
        else:
            popen_kwargs = {
                "stdin": (
                    subprocess.PIPE
                    if input_data is not None
                    else subprocess.DEVNULL
                ),
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "env": environment,
                "cwd": working_directory,
                "shell": False,
                "start_new_session": os.name == "posix",
            }
            if os.name == "posix":
                popen_kwargs["pass_fds"] = (lease_write_fd,)
            process = subprocess.Popen(list(arguments), **popen_kwargs)
        if lease_write_fd >= 0:
            os.close(lease_write_fd)
            lease_write_fd = -1
        if lease_read_fd >= 0:
            os.set_blocking(lease_read_fd, False)
            lease = os.fdopen(lease_read_fd, "rb", buffering=0)
            lease_read_fd = -1
        ownership = _ProcessGroupOwnership(
            process.pid if os.name == "posix" else None
        )
        if require_descendant_containment:
            descendant_tracker = _DarwinKqueueDescendantTracker(process.pid)
            ownership.attach_descendant_tracker(descendant_tracker)
        if registry is not None:
            registry.complete_spawn(process, ownership)
            spawn_pending = False
            process_registered = True
        if require_descendant_containment:
            _write_all_fd(config_write_fd, config)
            os.close(config_write_fd)
            config_write_fd = -1
            if pre_exec is not None:
                pre_exec()
            _write_all_fd(gate_write_fd, b"1")
            os.close(gate_write_fd)
            gate_write_fd = -1
        return _StartedProcess(
            process=process,
            ownership=ownership,
            lease=lease,
            descendant_tracker=descendant_tracker,
            containment_required=require_descendant_containment,
            signal_scope=signal_scope,
            registry=registry,
            process_registered=process_registered,
        )
    except BaseException:
        signal_scope.block_for_cleanup()
        if process is not None:
            if ownership is None:
                ownership = _ProcessGroupOwnership(
                    process.pid if os.name == "posix" else None
                )
            _force_cleanup_process(process, ownership)
        if lease is not None:
            try:
                lease.close()
            except (OSError, ValueError):
                pass
        if descendant_tracker is not None:
            try:
                descendant_tracker.close()
            except (OSError, ValueError):
                pass
        for descriptor in (
            lease_read_fd,
            lease_write_fd,
            config_read_fd,
            config_write_fd,
            gate_read_fd,
            gate_write_fd,
        ):
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass
        if registry is not None:
            if spawn_pending:
                registry.abort_spawn()
            elif process is not None and process_registered:
                registry.unregister(process)
        signal_scope.restore()
        raise


def _upload_stdin_chunk(
    stream: object,
    input_data: bytes,
    input_offset: int,
) -> Tuple[int, bool]:
    """Upload one nonblocking stdin chunk without losing would-block writes."""

    if input_offset >= len(input_data):
        return input_offset, True
    try:
        written = os.write(
            stream.fileno(),  # type: ignore[attr-defined]
            input_data[input_offset : input_offset + _CHUNK_SIZE],
        )
    except BlockingIOError:
        return input_offset, False
    except (BrokenPipeError, OSError):
        return input_offset, True
    input_offset += written
    return input_offset, input_offset >= len(input_data)


def _lease_is_at_eof(lease: Optional[BinaryIO]) -> bool:
    if lease is None:
        return os.name == "posix"
    while True:
        try:
            chunk = os.read(lease.fileno(), _CHUNK_SIZE)
        except BlockingIOError:
            return False
        except (OSError, ValueError):
            return False
        if not chunk:
            return True


def _wait_for_containment(
    started: _StartedProcess,
    lease_eof: bool,
    deadline: float,
    monotonic: Callable[[], float],
) -> Tuple[bool, bool]:
    while True:
        lease_eof = lease_eof or _lease_is_at_eof(started.lease)
        tracker = started.descendant_tracker
        live_descendants = (
            tracker.sample(force=True) if tracker is not None else ()
        )
        if tracker is not None and tracker.cleanup_incomplete:
            return lease_eof, False
        complete = (
            os.name == "posix"
            and lease_eof
            and (tracker is None or tracker.reliable)
            and not live_descendants
        )
        if complete:
            return lease_eof, True
        remaining = deadline - monotonic()
        if remaining <= 0:
            return lease_eof, False
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def _run_started_process(
    started: _StartedProcess,
    arguments: Tuple[str, ...],
    input_data: Optional[bytes],
    *,
    stdout_limit: int,
    stderr_limit: int,
    timeout: float,
    cancel_event: Optional[threading.Event],
    monotonic: Callable[[], float],
) -> BoundedProcessResult:
    process = started.process
    ownership = started.ownership
    signal_scope = started.signal_scope
    streams = tuple(
        stream
        for stream in (process.stdin, process.stdout, process.stderr, started.lease)
        if stream is not None
    )
    try:
        selector = selectors.DefaultSelector()
    except BaseException:
        _kill_process_group(process, ownership)
        for stream in streams:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        ownership.wait(process)
        raise
    stdout = bytearray()
    stderr = bytearray()
    overflow: Optional[str] = None
    input_offset = 0
    timed_out = False
    cancelled = False
    lease_eof = started.lease is None
    cleanup_incomplete = os.name != "posix"

    def close_stream(stream: object) -> None:
        try:
            selector.unregister(stream)
        except (KeyError, OSError, ValueError):
            pass
        try:
            stream.close()  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass

    def close_registered_streams() -> None:
        for key in list(selector.get_map().values()):
            close_stream(key.fileobj)

    try:
        signal_scope.unblock_after_spawn()
        if process.stdout is None or process.stderr is None:
            raise OSError("bounded process pipes are unavailable")
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        selector.register(
            process.stdout,
            selectors.EVENT_READ,
            ("stdout", process.stdout),
        )
        selector.register(
            process.stderr,
            selectors.EVENT_READ,
            ("stderr", process.stderr),
        )
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(
                process.stdin,
                selectors.EVENT_WRITE,
                ("stdin", process.stdin),
            )
        if started.lease is not None:
            selector.register(
                started.lease,
                selectors.EVENT_READ,
                ("lease", started.lease),
            )

        deadline = monotonic() + timeout
        while True:
            tracker = started.descendant_tracker
            live_descendants = tracker.sample() if tracker is not None else ()
            root_returncode = process.poll()
            if (
                not selector.get_map()
                and root_returncode is not None
                and not live_descendants
                and (tracker is None or tracker.reliable)
            ):
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill_process_group(process, ownership)
                lease_eof = lease_eof or _lease_is_at_eof(started.lease)
                close_registered_streams()
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_group(process, ownership)
                lease_eof = lease_eof or _lease_is_at_eof(started.lease)
                close_registered_streams()
                break

            wait_for = min(_POLL_INTERVAL_SECONDS, remaining)
            if selector.get_map():
                events = selector.select(wait_for)
            else:
                events = ()
                time.sleep(wait_for)
            for key, _mask in events:
                kind, stream = key.data
                if kind == "stdin":
                    assert input_data is not None
                    input_offset, upload_done = _upload_stdin_chunk(
                        stream,
                        input_data,
                        input_offset,
                    )
                    if upload_done:
                        close_stream(stream)
                    continue
                if kind == "lease":
                    try:
                        chunk = os.read(stream.fileno(), _CHUNK_SIZE)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if not chunk:
                        lease_eof = True
                        close_stream(stream)
                    continue

                target = stdout if kind == "stdout" else stderr
                limit = stdout_limit if kind == "stdout" else stderr_limit
                available = max(0, limit - len(target))
                try:
                    chunk = os.read(
                        stream.fileno(),
                        max(1, min(_CHUNK_SIZE, available + 1)),
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
                    _kill_process_group(process, ownership)
                    lease_eof, containment_complete = _wait_for_containment(
                        started,
                        lease_eof,
                        deadline,
                        monotonic,
                    )
                    cleanup_incomplete = not containment_complete
                    close_registered_streams()
                    break
            if overflow is not None:
                break
    except BaseException:
        signal_scope.block_for_cleanup()
        _kill_process_group(process, ownership)
        raise
    finally:
        signal_scope.block_for_cleanup()
        try:
            close_registered_streams()
            for stream in streams:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            ownership.wait(process)
        finally:
            selector.close()

    cleanup_incomplete = (
        cleanup_incomplete
        or not lease_eof
        or (
            started.descendant_tracker is not None
            and (
                not started.descendant_tracker.reliable
                or started.descendant_tracker.cleanup_incomplete
                or bool(started.descendant_tracker.sample(force=True))
            )
        )
    )
    if timed_out or cancelled:
        error = subprocess.TimeoutExpired(
            arguments,
            timeout,
            output=bytes(stdout),
            stderr=bytes(stderr),
        )
        error.cleanup_incomplete = cleanup_incomplete
        raise error
    return BoundedProcessResult(
        args=arguments,
        returncode=process.returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
        overflow=overflow,
        cleanup_incomplete=cleanup_incomplete,
    )


def run_bounded(
    argv: Sequence[str],
    input_data: Optional[bytes] = None,
    *,
    input_limit: int,
    stdout_limit: int,
    stderr_limit: int,
    timeout: float,
    env: Optional[Mapping[str, str]] = None,
    cwd: Optional[Union[str, os.PathLike]] = None,
    cancel_event: Optional[threading.Event] = None,
    pre_spawn: Optional[Callable[[], None]] = None,
    pre_exec: Optional[Callable[[], None]] = None,
    require_descendant_containment: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
) -> BoundedProcessResult:
    """Run a child while bounding all pipe data and execution time."""

    arguments = _validated_argv(argv)
    input_limit = _positive_limit(input_limit, "input_limit")
    stdout_limit = _positive_limit(stdout_limit, "stdout_limit")
    stderr_limit = _positive_limit(stderr_limit, "stderr_limit")
    timeout = _bounded_timeout(timeout)
    working_directory = _working_directory(cwd)
    environment = None if env is None else dict(env)
    if pre_spawn is not None and not callable(pre_spawn):
        raise TypeError("pre_spawn must be callable")
    if pre_exec is not None and not callable(pre_exec):
        raise TypeError("pre_exec must be callable")
    if type(require_descendant_containment) is not bool:
        raise TypeError("require_descendant_containment must be bool")
    if pre_exec is not None and not require_descendant_containment:
        raise ValueError("pre_exec requires descendant containment")
    if (
        require_descendant_containment
        and not _DarwinKqueueDescendantTracker.supported()
    ):
        raise DescendantContainmentUnsupportedError(
            "deterministic descendant containment is unsupported"
        )
    if input_data is not None and not isinstance(input_data, bytes):
        raise TypeError("input_data must be bytes")
    if input_data is not None and len(input_data) > input_limit:
        return BoundedProcessResult(
            args=arguments,
            returncode=-1,
            stdout=b"",
            stderr=b"",
            overflow="input",
        )
    if cancel_event is not None and cancel_event.is_set():
        raise subprocess.TimeoutExpired(
            arguments,
            timeout,
            output=b"",
            stderr=b"",
        )
    started: Optional[_StartedProcess] = None
    try:
        started = _start_bounded_process(
            arguments,
            input_data,
            environment=environment,
            working_directory=working_directory,
            pre_spawn=pre_spawn,
            pre_exec=pre_exec,
            require_descendant_containment=require_descendant_containment,
        )
        return _run_started_process(
            started,
            arguments,
            input_data,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            timeout=timeout,
            cancel_event=cancel_event,
            monotonic=monotonic,
        )
    except BaseException:
        if (
            started is not None
            and started.process.returncode is None
        ):
            started.signal_scope.block_for_cleanup()
            _force_cleanup_process(started.process, started.ownership)
        raise
    finally:
        if started is not None:
            started.release()


class BoundedLineStream(Iterator[bytes]):
    """Context-managed, indefinite subprocess stdout line stream."""

    def __init__(
        self,
        argv: Sequence[str],
        input_data: Optional[bytes] = None,
        *,
        line_limit: int,
        startup_timeout: float,
        stderr_limit: int = MAX_STREAM_STDERR_BYTES,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[Union[str, os.PathLike]] = None,
        cancel_event: Optional[threading.Event] = None,
        pre_spawn: Optional[Callable[[], None]] = None,
        pre_exec: Optional[Callable[[], None]] = None,
        require_descendant_containment: bool = False,
        ready_on_first_line: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        arguments = _validated_argv(argv)
        line_limit = _positive_limit(line_limit, "line_limit")
        stderr_limit = _positive_limit(stderr_limit, "stderr_limit")
        startup_timeout = _bounded_timeout(startup_timeout)
        if line_limit > MAX_STREAM_LINE_BYTES:
            raise ValueError(
                "line_limit must not exceed {}".format(MAX_STREAM_LINE_BYTES)
            )
        if stderr_limit > MAX_STREAM_STDERR_BYTES:
            raise ValueError(
                "stderr_limit must not exceed {}".format(MAX_STREAM_STDERR_BYTES)
            )
        if input_data is not None and not isinstance(input_data, bytes):
            raise TypeError("input_data must be bytes")
        if pre_spawn is not None and not callable(pre_spawn):
            raise TypeError("pre_spawn must be callable")
        if pre_exec is not None and not callable(pre_exec):
            raise TypeError("pre_exec must be callable")
        if type(require_descendant_containment) is not bool:
            raise TypeError("require_descendant_containment must be bool")
        if pre_exec is not None and not require_descendant_containment:
            raise ValueError("pre_exec requires descendant containment")
        if (
            require_descendant_containment
            and not _DarwinKqueueDescendantTracker.supported()
        ):
            raise DescendantContainmentUnsupportedError(
                "deterministic descendant containment is unsupported"
            )
        if type(ready_on_first_line) is not bool:
            raise TypeError("ready_on_first_line must be bool")

        self._arguments = arguments
        self._line_limit = line_limit
        self._stderr_limit = stderr_limit
        self._startup_timeout = startup_timeout
        self._cancel_event = cancel_event
        self._ready_on_first_line = ready_on_first_line
        self._monotonic = monotonic
        self._input_data = input_data
        self._input_offset = 0
        self._stdout_buffer = bytearray()
        self._stderr_buffer = bytearray()
        self._stdout_eof = False
        self._stderr_eof = False
        self._lease_eof = False
        self._lines_yielded = 0
        self._stdout_bytes_read = 0
        self._ready = False
        self._closed = False
        self._result: Optional[BoundedLineStreamResult] = None
        self._selector: Optional[selectors.BaseSelector] = None
        self._started: Optional[_StartedProcess] = None

        if input_data is not None and len(input_data) > MAX_STREAM_INPUT_BYTES:
            result = self._unspawned_result(
                BoundedLineStreamEndReason.INPUT_OVERFLOW
            )
            raise BoundedLineStreamOverflowError(
                "input exceeds the 4 MiB stream limit",
                result,
            )
        if cancel_event is not None and cancel_event.is_set():
            result = self._unspawned_result(BoundedLineStreamEndReason.CANCELLED)
            raise BoundedLineStreamCancelledError(
                "line stream was cancelled before spawn",
                result,
            )

        working_directory = _working_directory(cwd)
        environment = None if env is None else dict(env)
        started = _start_bounded_process(
            arguments,
            input_data,
            environment=environment,
            working_directory=working_directory,
            pre_spawn=pre_spawn,
            pre_exec=pre_exec,
            require_descendant_containment=require_descendant_containment,
        )
        self._started = started
        self._lease_eof = started.lease is None
        try:
            self._selector = selectors.DefaultSelector()
            self._configure_streams()
            self._startup_deadline: Optional[float] = (
                monotonic() + startup_timeout
            )
            started.signal_scope.unblock_after_spawn()
        except BaseException:
            started.signal_scope.block_for_cleanup()
            _force_cleanup_process(started.process, started.ownership)
            if self._selector is not None:
                self._selector.close()
            started.release()
            self._started = None
            raise

    def _unspawned_result(
        self,
        reason: BoundedLineStreamEndReason,
    ) -> BoundedLineStreamResult:
        return BoundedLineStreamResult(
            args=self._arguments,
            returncode=None,
            stderr=b"",
            end_reason=reason,
            lines_yielded=0,
            stdout_bytes_read=0,
        )

    def _configure_streams(self) -> None:
        assert self._started is not None
        assert self._selector is not None
        process = self._started.process
        if process.stdout is None or process.stderr is None:
            raise OSError("bounded process pipes are unavailable")
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stderr.fileno(), False)
        self._selector.register(
            process.stdout,
            selectors.EVENT_READ,
            ("stdout", process.stdout),
        )
        self._selector.register(
            process.stderr,
            selectors.EVENT_READ,
            ("stderr", process.stderr),
        )
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
            self._selector.register(
                process.stdin,
                selectors.EVENT_WRITE,
                ("stdin", process.stdin),
            )
        if self._started.lease is not None:
            self._selector.register(
                self._started.lease,
                selectors.EVENT_READ,
                ("lease", self._started.lease),
            )

    @property
    def result(self) -> Optional[BoundedLineStreamResult]:
        return self._result

    @property
    def returncode(self) -> Optional[int]:
        if self._result is not None:
            return self._result.returncode
        if self._started is None:
            return None
        return self._started.process.poll()

    @property
    def end_reason(self) -> Optional[BoundedLineStreamEndReason]:
        return None if self._result is None else self._result.end_reason

    @property
    def stderr(self) -> bytes:
        if self._result is not None:
            return self._result.stderr
        return bytes(self._stderr_buffer)

    @property
    def ready(self) -> bool:
        return self._ready

    def mark_ready(self) -> None:
        """Disable the startup deadline after caller-defined readiness."""

        if self._closed:
            raise RuntimeError("line stream is closed")
        self._ready = True
        self._startup_deadline = None

    def reset_startup_timeout(self, timeout: Optional[float] = None) -> None:
        """Restart the startup deadline using a validated relative timeout."""

        if self._closed:
            raise RuntimeError("line stream is closed")
        selected = self._startup_timeout if timeout is None else _bounded_timeout(timeout)
        self._startup_deadline = self._monotonic() + selected
        self._ready = False

    def disable_startup_timeout(self) -> None:
        """Disable startup expiry without marking a protocol ready."""

        if self._closed:
            raise RuntimeError("line stream is closed")
        self._startup_deadline = None

    def __enter__(self) -> "BoundedLineStream":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def __iter__(self) -> "BoundedLineStream":
        return self

    def _close_stream(self, stream: object) -> None:
        if self._selector is not None:
            try:
                self._selector.unregister(stream)
            except (KeyError, OSError, ValueError):
                pass
        try:
            stream.close()  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass

    def _close_all_streams(self) -> None:
        if self._selector is not None:
            for key in list(self._selector.get_map().values()):
                self._close_stream(key.fileobj)
        if self._started is not None:
            process = self._started.process
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass

    def _finish(
        self,
        reason: BoundedLineStreamEndReason,
        *,
        kill_group: bool,
    ) -> BoundedLineStreamResult:
        if self._result is not None:
            return self._result
        assert self._started is not None
        started = self._started
        started.signal_scope.block_for_cleanup()
        try:
            if kill_group:
                _kill_process_group(started.process, started.ownership)
                self._lease_eof = self._lease_eof or _lease_is_at_eof(
                    started.lease
                )
            self._close_all_streams()
            returncode = started.ownership.wait(started.process)
            tracker = started.descendant_tracker
            cleanup_incomplete = (
                os.name != "posix"
                or not self._lease_eof
                or (
                    tracker is not None
                    and (
                        not tracker.reliable
                        or tracker.cleanup_incomplete
                        or bool(tracker.sample(force=True))
                    )
                )
            )
        finally:
            if self._selector is not None:
                self._selector.close()
                self._selector = None
            started.release()
            self._closed = True
        self._result = BoundedLineStreamResult(
            args=self._arguments,
            returncode=returncode,
            stderr=bytes(self._stderr_buffer),
            end_reason=reason,
            lines_yielded=self._lines_yielded,
            stdout_bytes_read=self._stdout_bytes_read,
            cleanup_incomplete=cleanup_incomplete,
        )
        return self._result

    def _fail(
        self,
        reason: BoundedLineStreamEndReason,
        error_type: type,
        message: str,
    ) -> None:
        result = self._finish(reason, kill_group=True)
        raise error_type(message, result)

    def _check_controls(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            self._fail(
                BoundedLineStreamEndReason.CANCELLED,
                BoundedLineStreamCancelledError,
                "line stream was cancelled",
            )
        if (
            self._startup_deadline is not None
            and self._monotonic() >= self._startup_deadline
        ):
            self._fail(
                BoundedLineStreamEndReason.STARTUP_TIMEOUT,
                BoundedLineStreamTimeoutError,
                "line stream startup timed out",
            )

    def _take_stdout_line(self) -> Optional[bytes]:
        newline = self._stdout_buffer.find(b"\n")
        if newline >= 0:
            line = bytes(self._stdout_buffer[:newline])
            del self._stdout_buffer[: newline + 1]
        elif self._stdout_eof and self._stdout_buffer:
            line = bytes(self._stdout_buffer)
            self._stdout_buffer.clear()
        else:
            return None
        self._lines_yielded += 1
        if self._ready_on_first_line and not self._ready:
            self.mark_ready()
        return line

    def _read_stdout(self, stream: object) -> None:
        available = self._line_limit - len(self._stdout_buffer)
        try:
            chunk = os.read(
                stream.fileno(),  # type: ignore[attr-defined]
                max(1, min(_CHUNK_SIZE, available + 1)),
            )
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._stdout_eof = True
            self._close_stream(stream)
            return
        self._stdout_bytes_read += len(chunk)
        self._stdout_buffer.extend(chunk)
        if b"\n" not in self._stdout_buffer and len(self._stdout_buffer) > self._line_limit:
            self._fail(
                BoundedLineStreamEndReason.LINE_OVERFLOW,
                BoundedLineStreamOverflowError,
                "stdout line exceeds the configured limit",
            )

    def _read_stderr(self, stream: object) -> None:
        available = self._stderr_limit - len(self._stderr_buffer)
        try:
            chunk = os.read(
                stream.fileno(),  # type: ignore[attr-defined]
                max(1, min(_CHUNK_SIZE, available + 1)),
            )
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._stderr_eof = True
            self._close_stream(stream)
            return
        self._stderr_buffer.extend(chunk[:available])
        if len(chunk) > available:
            self._fail(
                BoundedLineStreamEndReason.STDERR_OVERFLOW,
                BoundedLineStreamOverflowError,
                "stderr exceeds the configured limit",
            )

    def _read_lease(self, stream: object) -> None:
        try:
            chunk = os.read(
                stream.fileno(),  # type: ignore[attr-defined]
                _CHUNK_SIZE,
            )
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            self._lease_eof = True
            self._close_stream(stream)

    def _pump_once(self) -> None:
        assert self._started is not None
        assert self._selector is not None
        deadline = self._startup_deadline
        wait_for = _POLL_INTERVAL_SECONDS
        if deadline is not None:
            wait_for = min(wait_for, max(0.0, deadline - self._monotonic()))
        if self._selector.get_map():
            events = self._selector.select(wait_for)
        else:
            time.sleep(wait_for)
            events = ()
        for key, _mask in events:
            kind, stream = key.data
            if kind == "stdin":
                assert self._input_data is not None
                self._input_offset, upload_done = _upload_stdin_chunk(
                    stream,
                    self._input_data,
                    self._input_offset,
                )
                if upload_done:
                    self._close_stream(stream)
            elif kind == "stdout":
                self._read_stdout(stream)
            elif kind == "stderr":
                self._read_stderr(stream)
            else:
                self._read_lease(stream)

        process = self._started.process
        returncode = process.poll()
        tracker = self._started.descendant_tracker
        live_descendants = tracker.sample() if tracker is not None else ()
        if returncode is not None and process.stdin is not None:
            self._close_stream(process.stdin)
        if (
            returncode is None
            or not self._stdout_eof
            or not self._stderr_eof
            or not self._lease_eof
            or live_descendants
        ):
            return
        if self._stdout_buffer:
            return
        reason = (
            BoundedLineStreamEndReason.EOF
            if returncode == 0
            else BoundedLineStreamEndReason.NONZERO_EXIT
        )
        result = self._finish(reason, kill_group=True)
        if returncode != 0:
            raise BoundedLineStreamProcessError(
                "line stream process exited with status {}".format(returncode),
                result,
            )

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            while True:
                self._check_controls()
                line = self._take_stdout_line()
                if line is not None:
                    return line
                self._pump_once()
                if self._closed:
                    raise StopIteration
        except (BoundedLineStreamError, StopIteration):
            raise
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Kill and reap the owned process group, retaining terminal metadata."""

        if self._closed:
            return
        self._finish(BoundedLineStreamEndReason.CLOSED, kill_group=True)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


__all__ = [
    "BoundedLineStream",
    "BoundedLineStreamCancelledError",
    "BoundedLineStreamEndReason",
    "BoundedLineStreamError",
    "BoundedLineStreamOverflowError",
    "BoundedLineStreamProcessError",
    "BoundedLineStreamResult",
    "BoundedLineStreamTimeoutError",
    "BoundedProcessResult",
    "DescendantContainmentUnsupportedError",
    "MAX_STREAM_INPUT_BYTES",
    "MAX_STREAM_LINE_BYTES",
    "MAX_STREAM_STDERR_BYTES",
    "MAX_TIMEOUT_SECONDS",
    "bounded_execution_signal_guard",
    "run_bounded",
]
