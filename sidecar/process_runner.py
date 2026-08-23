"""Bounded, dependency-free subprocess execution."""

from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from numbers import Real
from typing import Callable, Iterator, Mapping, Optional, Sequence, Set, Tuple, Union


MAX_TIMEOUT_SECONDS = 3600.0
_CHUNK_SIZE = 65536
_POLL_INTERVAL_SECONDS = 0.05
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


class _ProcessRegistry:
    """Thread-safe process ownership for one bounded execution scope."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._closing = False
        self._spawning = 0
        self._processes: Set[subprocess.Popen] = set()

    def begin_spawn(self) -> None:
        with self._condition:
            if self._closing:
                raise RuntimeError("bounded execution scope is closing")
            self._spawning += 1

    def complete_spawn(self, process: subprocess.Popen) -> None:
        with self._condition:
            self._processes.add(process)
            self._spawning -= 1
            closing = self._closing
            self._condition.notify_all()
        if closing:
            _kill_process_group(process)

    def abort_spawn(self) -> None:
        with self._condition:
            self._spawning -= 1
            self._condition.notify_all()

    def unregister(self, process: subprocess.Popen) -> None:
        with self._condition:
            self._processes.discard(process)
            self._condition.notify_all()

    def kill_all(self) -> None:
        with self._condition:
            self._closing = True
            while self._spawning:
                self._condition.wait(_POLL_INTERVAL_SECONDS)
            processes = tuple(self._processes)
        for process in processes:
            _kill_process_group(process)

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


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
        return
    except OSError:
        pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _force_cleanup_process(process: subprocess.Popen) -> None:
    _kill_process_group(process)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    process.wait()


def _run_started_process(
    process: subprocess.Popen,
    arguments: Tuple[str, ...],
    input_data: Optional[bytes],
    *,
    stdout_limit: int,
    stderr_limit: int,
    timeout: float,
    cancel_event: Optional[threading.Event],
    monotonic: Callable[[], float],
    signal_scope: _SignalScope,
) -> BoundedProcessResult:
    streams = tuple(
        stream
        for stream in (process.stdin, process.stdout, process.stderr)
        if stream is not None
    )
    try:
        selector = selectors.DefaultSelector()
    except BaseException:
        _kill_process_group(process)
        for stream in streams:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        process.wait()
        raise
    stdout = bytearray()
    stderr = bytearray()
    overflow: Optional[str] = None
    input_offset = 0
    timed_out = False
    cancelled = False

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

        deadline = monotonic() + timeout
        while process.poll() is None or selector.get_map():
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill_process_group(process)
                close_registered_streams()
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_process_group(process)
                close_registered_streams()
                break

            wait_for = min(_POLL_INTERVAL_SECONDS, remaining)
            if selector.get_map():
                events = selector.select(wait_for)
            else:
                events = ()
                try:
                    process.wait(timeout=wait_for)
                except subprocess.TimeoutExpired:
                    pass
            for key, _mask in events:
                kind, stream = key.data
                if kind == "stdin":
                    assert input_data is not None
                    if input_offset >= len(input_data):
                        close_stream(stream)
                        continue
                    try:
                        written = os.write(
                            stream.fileno(),
                            input_data[input_offset : input_offset + _CHUNK_SIZE],
                        )
                    except (BlockingIOError, BrokenPipeError, OSError):
                        close_stream(stream)
                    else:
                        input_offset += written
                        if input_offset >= len(input_data):
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
                    _kill_process_group(process)
                    close_registered_streams()
                    break
            if overflow is not None:
                break
    except BaseException:
        signal_scope.block_for_cleanup()
        _kill_process_group(process)
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
            if process.poll() is None:
                _kill_process_group(process)
            process.wait()
        finally:
            selector.close()

    if timed_out or cancelled:
        raise subprocess.TimeoutExpired(
            arguments,
            timeout,
            output=bytes(stdout),
            stderr=bytes(stderr),
        )
    return BoundedProcessResult(
        args=arguments,
        returncode=process.returncode,
        stdout=bytes(stdout),
        stderr=bytes(stderr),
        overflow=overflow,
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

    signal_scope = _SignalScope()
    registry: Optional[_ProcessRegistry] = None
    process: Optional[subprocess.Popen] = None
    spawn_pending = False
    process_registered = False
    signal_scope.install_before_spawn()
    try:
        registry = _current_process_registry()
        if registry is not None:
            registry.begin_spawn()
            spawn_pending = True
        process = subprocess.Popen(
            list(arguments),
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd=working_directory,
            shell=False,
            start_new_session=(os.name == "posix"),
        )
        if registry is not None:
            registry.complete_spawn(process)
            spawn_pending = False
            process_registered = True
        return _run_started_process(
            process,
            arguments,
            input_data,
            stdout_limit=stdout_limit,
            stderr_limit=stderr_limit,
            timeout=timeout,
            cancel_event=cancel_event,
            monotonic=monotonic,
            signal_scope=signal_scope,
        )
    except BaseException:
        signal_scope.block_for_cleanup()
        if process is not None and process.returncode is None:
            _force_cleanup_process(process)
        raise
    finally:
        if registry is not None:
            if spawn_pending:
                registry.abort_spawn()
            if process is not None and process_registered:
                registry.unregister(process)
        signal_scope.restore()


__all__ = [
    "BoundedProcessResult",
    "MAX_TIMEOUT_SECONDS",
    "bounded_execution_signal_guard",
    "run_bounded",
]
