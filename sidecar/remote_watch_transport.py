"""Strict streaming SSH transport for remote watch events."""

from __future__ import annotations

import shlex
import threading
from typing import Any, Callable, Iterator, Optional, Sequence, Tuple

from sidecar.json_limits import JSONLimitError, JSONLimits, JSONSyntaxError, parse_json
from sidecar.process_runner import (
    BoundedLineStream,
    BoundedLineStreamCancelledError,
    BoundedLineStreamError,
    BoundedLineStreamOverflowError,
    BoundedLineStreamProcessError,
    BoundedLineStreamTimeoutError,
)
from sidecar.remote_transport import (
    REMOTE_PYTHON_CANDIDATES,
    _bootstrap_python_executable,
    _failure_code,
    _ssh_command_argv,
    _validated_command_executable,
    _validated_probe_candidates,
    probe_remote_python,
)
from sidecar.remote_types import (
    MAX_ARTIFACT_BYTES,
    PROBE_TIMEOUT_SECONDS,
    ProtocolResourceLimitError,
    RemoteFailure,
    RemoteHost,
)
from sidecar.remote_watch_types import (
    MAX_WATCH_JSON_DEPTH,
    MAX_WATCH_JSON_NODES,
    MAX_WATCH_JSON_STRING_BYTES,
    MAX_WATCH_LINE_BYTES,
    MAX_WATCH_STDERR_BYTES,
    RemoteWatchEvent,
    RemoteWatchReady,
    WATCH_STARTUP_TIMEOUT_SECONDS,
    validate_watch_event,
)


READY_FRAME = b'{"type":"ready"}'
END_FRAME = b'{"type":"end"}'
PING_FRAME = b'{"type":"ping"}'
_STREAM_ERROR_CODES = frozenset(("protocol", "resource_limit", "remote"))
_ERROR_FRAMES = {
    ('{{"type":"error","code":"{}"}}'.format(code)).encode("ascii"): code
    for code in _STREAM_ERROR_CODES
}


REMOTE_WATCH_BOOTSTRAP = r"""
import errno
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time

MAX_ARTIFACT = 4194304
MAX_LINE = 262144
MAX_ERROR = 65536
HEARTBEAT_INTERVAL = 0.75
EVENT_KEYS = frozenset(("ts", "agent", "session_id", "kind", "text", "extra"))
READY = b'{"type":"ready"}\n'
INTERNAL_READY = b'{"type":"ready"}'
END = b'{"type":"end"}\n'
PING = b'{"type":"ping"}\n'
MANAGED_SIGNALS = tuple(
    value
    for value in (
        getattr(signal, "SIGINT", None),
        getattr(signal, "SIGTERM", None),
        getattr(signal, "SIGHUP", None),
    )
    if value is not None
)
path = None
fd = None
process = None
selector = None
exit_requested = False

class ExitRequested(BaseException):
    pass

class OutputClosed(BaseException):
    pass

def request_exit(_signum, _frame):
    global exit_requested
    if exit_requested:
        return
    exit_requested = True
    raise ExitRequested()

def block_cleanup_signals():
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_BLOCK, set(MANAGED_SIGNALS))
        return
    for signum in MANAGED_SIGNALS:
        signal.signal(signum, signal.SIG_IGN)

def abandon_output():
    replacement = None
    try:
        replacement = os.open(os.devnull, os.O_WRONLY)
        os.dup2(replacement, sys.stdout.fileno())
    except OSError:
        pass
    finally:
        if replacement is not None:
            try:
                os.close(replacement)
            except OSError:
                pass

def emit_bytes(value):
    try:
        sys.stdout.buffer.write(value)
        sys.stdout.buffer.flush()
    except OSError as error:
        if error.errno in (errno.EPIPE, errno.ECONNRESET):
            abandon_output()
            raise OutputClosed()
        raise

def emit_error(code):
    emit_bytes(
        json.dumps(
            {"type": "error", "code": code},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=False,
        ).encode("ascii") + b"\n"
    )

def pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value

def reject_constant(_value):
    raise ValueError("constant")

def event_line_error(raw):
    if not raw or b"\r" in raw or raw != raw.strip():
        return "protocol"
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except RecursionError:
        return "resource_limit"
    except Exception:
        return "protocol"
    if not (
        isinstance(value, dict)
        and set(value) == EVENT_KEYS
        and isinstance(value.get("ts"), str)
        and isinstance(value.get("agent"), str)
        and isinstance(value.get("session_id"), str)
        and isinstance(value.get("kind"), str)
        and isinstance(value.get("text"), str)
        and isinstance(value.get("extra"), dict)
    ):
        return "protocol"
    return None

def kill_group():
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

def reap(timeout):
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True

def close_stream(stream):
    if selector is not None:
        try:
            selector.unregister(stream)
        except (KeyError, OSError, ValueError):
            pass
    try:
        stream.close()
    except (OSError, ValueError):
        pass

def allowed_args(args):
    return args in (
        ["watch", "--all", "--json"],
        ["watch", "--all", "--from-start", "--json"],
    )

def run_child(child_args):
    global process, selector
    process = subprocess.Popen(
        [sys.executable, "-I", path] + child_args + ["--stream-ready"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        raise OSError("pipes")
    selector = selectors.DefaultSelector()
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ, ("stdout", process.stdout))
    selector.register(process.stderr, selectors.EVENT_READ, ("stderr", process.stderr))

    pending = bytearray()
    errors = bytearray()
    stdout_eof = False
    stderr_eof = False
    failure = None
    group_terminated = False
    ready_seen = False
    next_heartbeat = None

    while True:
        if failure is not None:
            break
        events = selector.select(0.05)
        for key, _mask in events:
            kind, stream = key.data
            if kind == "stderr":
                available = MAX_ERROR - len(errors)
                try:
                    chunk = os.read(stream.fileno(), max(1, min(65536, available + 1)))
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""
                if not chunk:
                    stderr_eof = True
                    close_stream(stream)
                    continue
                errors.extend(chunk[:available])
                if len(chunk) > available:
                    failure = "protocol"
                    break
                continue

            try:
                chunk = os.read(stream.fileno(), 65536)
            except BlockingIOError:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                stdout_eof = True
                close_stream(stream)
                continue
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    if len(pending) > MAX_LINE:
                        failure = "resource_limit"
                    break
                if newline > MAX_LINE:
                    failure = "resource_limit"
                    break
                line = bytes(pending[:newline])
                del pending[:newline + 1]
                if not ready_seen:
                    if line != INTERNAL_READY:
                        failure = "protocol"
                        break
                    ready_seen = True
                    emit_bytes(READY)
                    next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
                    continue
                if line == INTERNAL_READY:
                    failure = "protocol"
                    break
                line_error = event_line_error(line)
                if line_error is not None:
                    failure = line_error
                    break
                emit_bytes(line + b"\n")
                next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL
            if failure is not None:
                break
        if failure is not None:
            kill_group()
            break

        returncode = process.poll()
        if returncode is None:
            if ready_seen:
                now = time.monotonic()
                if next_heartbeat is not None and now >= next_heartbeat:
                    emit_bytes(PING)
                    next_heartbeat = now + HEARTBEAT_INTERVAL
            continue
        if not group_terminated:
            kill_group()
            group_terminated = True
        if not stdout_eof or not stderr_eof:
            continue
        if pending:
            failure = "protocol"
        elif returncode != 0:
            failure = "remote"
        elif not ready_seen:
            failure = "remote"
        break

    if process.poll() is None:
        kill_group()
    if not reap(1):
        kill_group()
        reap(1)
    return failure

terminal = None
try:
    for signum in MANAGED_SIGNALS:
        signal.signal(signum, request_exit)
    child_args = sys.argv[1:]
    if not allowed_args(child_args):
        terminal = "protocol"
    else:
        data = sys.stdin.buffer.read(MAX_ARTIFACT + 1)
        if not data or len(data) > MAX_ARTIFACT:
            terminal = "protocol"
        else:
            fd, path = tempfile.mkstemp(prefix="agent-sidecar-watch-", suffix=".pyz")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = None
                stream.write(data)
            terminal = run_child(child_args)
except ExitRequested:
    exit_requested = True
except OutputClosed:
    exit_requested = True
except Exception:
    terminal = "remote"
finally:
    block_cleanup_signals()
    kill_group()
    if process is not None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                close_stream(stream)
        if process.poll() is None and not reap(1):
            kill_group()
            reap(1)
    if selector is not None:
        selector.close()
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

if not exit_requested:
    try:
        if terminal is None:
            emit_bytes(END)
        else:
            emit_error(terminal)
    except OutputClosed:
        pass
""".strip()


def remote_watch_shell_command(
    *,
    from_start: bool = False,
    python_executable: str = "python3",
) -> str:
    """Return the fixed remote bootstrap command for watch-all streaming."""

    if type(from_start) is not bool:
        raise TypeError("from_start must be bool")
    arguments = [
        _validated_command_executable(python_executable),
        "-c",
        REMOTE_WATCH_BOOTSTRAP,
        "watch",
        "--all",
    ]
    if from_start:
        arguments.append("--from-start")
    arguments.append("--json")
    return shlex.join(arguments)


def remote_watch_ssh_argv(
    alias: str,
    *,
    from_start: bool = False,
    python_executable: str = "python3",
) -> Tuple[str, ...]:
    """Build strict direct OpenSSH argv for the watch bootstrap."""

    return _ssh_command_argv(
        alias,
        remote_watch_shell_command(
            from_start=from_start,
            python_executable=python_executable,
        ),
    )


class RemoteWatchTransportError(RuntimeError):
    """A host-scoped transport failure carrying only a stable code."""

    def __init__(self, code: str) -> None:
        if code not in _STREAM_ERROR_CODES and code not in (
            "timeout",
            "unreachable",
            "auth",
            "host_key",
        ):
            code = "remote"
        super().__init__(code)
        self.code = code


def _parsed_line(line: bytes) -> Any:
    if (
        not isinstance(line, bytes)
        or not line
        or len(line) > MAX_WATCH_LINE_BYTES
        or b"\r" in line
        or line != line.strip()
    ):
        raise ValueError("invalid remote watch frame")
    return parse_json(
        line,
        JSONLimits(
            max_bytes=MAX_WATCH_LINE_BYTES,
            max_depth=MAX_WATCH_JSON_DEPTH,
            max_nodes=MAX_WATCH_JSON_NODES,
            max_string_bytes=MAX_WATCH_JSON_STRING_BYTES,
            max_integer_bits=4096,
        ),
    )


def _validated_event_line(
    line: bytes,
    host: str,
) -> Tuple[Optional[RemoteWatchEvent], Optional[str]]:
    try:
        value = _parsed_line(line)
    except JSONLimitError:
        return None, "resource_limit"
    except (JSONSyntaxError, TypeError, UnicodeError, ValueError):
        return None, "protocol"
    if isinstance(value, dict) and "type" in value:
        if (
            set(value) == {"type", "code"}
            and value.get("type") == "error"
            and value.get("code") in _STREAM_ERROR_CODES
        ):
            return None, value["code"]
        return None, "protocol"
    try:
        return validate_watch_event(value, host), None
    except ProtocolResourceLimitError:
        return None, "resource_limit"
    except (TypeError, UnicodeError, ValueError):
        return None, "protocol"


class RemoteWatchHostStream(Iterator[RemoteWatchEvent]):
    """Parse one bounded SSH line stream after its readiness handshake."""

    def __init__(self, host: RemoteHost, stream: object) -> None:
        if not isinstance(host, RemoteHost):
            raise TypeError("host must be a RemoteHost")
        if not hasattr(stream, "__next__"):
            raise TypeError("stream must be an iterator")
        self.host = host
        self._stream = stream
        self._ready = False
        self._ended = False
        self._closed = False

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def ended(self) -> bool:
        return self._ended

    def __enter__(self) -> "RemoteWatchHostStream":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def __iter__(self) -> "RemoteWatchHostStream":
        return self

    def _read_line(self) -> Tuple[Optional[bytes], Optional[str]]:
        try:
            return next(self._stream), None  # type: ignore[arg-type]
        except StopIteration:
            return None, "protocol"
        except BoundedLineStreamError as error:
            return None, _line_stream_error_code(error)
        except OSError:
            return None, "unreachable"
        except BaseException:
            return None, "remote"

    def read_ready(self) -> RemoteWatchReady:
        if self._ready:
            raise RuntimeError("remote watch stream is already ready")
        line, failure = self._read_line()
        if failure is not None:
            self.close()
            raise RemoteWatchTransportError(failure) from None
        if line != READY_FRAME:
            self.close()
            raise RemoteWatchTransportError(
                _ERROR_FRAMES.get(line, "protocol")
            ) from None
        marker = getattr(self._stream, "mark_ready", None)
        if callable(marker):
            marker_failed = False
            try:
                marker()
            except BaseException:
                marker_failed = True
            if marker_failed:
                self.close()
                raise RemoteWatchTransportError("remote") from None
        self._ready = True
        return RemoteWatchReady(self.host.alias)

    def __next__(self) -> RemoteWatchEvent:
        if self._closed or self._ended:
            raise StopIteration
        if not self._ready:
            raise RuntimeError("read_ready must be called before events")
        while True:
            line, failure = self._read_line()
            if failure is not None:
                self.close()
                raise RemoteWatchTransportError(failure) from None
            if line != PING_FRAME:
                break

        if line == END_FRAME:
            self._ended = True
            self.close()
            raise StopIteration
        assert line is not None
        event, failure = _validated_event_line(line, self.host.alias)
        if failure is not None:
            self.close()
            raise RemoteWatchTransportError(failure) from None
        assert event is not None
        return event

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        closer = getattr(self._stream, "close", None)
        if callable(closer):
            try:
                closer()
            except BaseException:
                pass


def _line_stream_error_code(error: BoundedLineStreamError) -> str:
    if isinstance(error, BoundedLineStreamTimeoutError):
        return "timeout"
    if isinstance(error, BoundedLineStreamCancelledError):
        return "timeout"
    if isinstance(error, BoundedLineStreamOverflowError):
        reason = error.result.end_reason.value
        return "resource_limit" if reason == "line_overflow" else "protocol"
    if isinstance(error, BoundedLineStreamProcessError):
        return _failure_code(error.result)
    return "remote"


def open_remote_watch_host(
    host: RemoteHost,
    artifact: bytes,
    *,
    from_start: bool = False,
    python_candidates: Sequence[str] = REMOTE_PYTHON_CANDIDATES,
    runner: Optional[Callable[..., object]] = None,
    stream_factory: Optional[Callable[..., object]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Tuple[Optional[RemoteWatchHostStream], Optional[RemoteFailure]]:
    """Probe and open one host watch stream without reconnecting.

    Bootstrap readiness is emitted only after the child watch command emits
    its exact hidden ``--stream-ready`` control frame.
    """

    if not isinstance(host, RemoteHost):
        raise TypeError("host must be a RemoteHost")
    if (
        not isinstance(artifact, bytes)
        or not artifact
        or len(artifact) > MAX_ARTIFACT_BYTES
    ):
        raise ValueError("invalid zipapp artifact")
    if type(from_start) is not bool:
        raise TypeError("from_start must be bool")
    probe_candidates = _validated_probe_candidates(python_candidates)
    hit, failure = probe_remote_python(
        host,
        candidates=probe_candidates,
        runner=runner,
        timeout=PROBE_TIMEOUT_SECONDS,
        cancel_event=cancel_event,
    )
    if failure is not None:
        return None, failure
    if hit is None:
        return None, RemoteFailure(host.alias, "protocol")
    bootstrap_executable = _bootstrap_python_executable(probe_candidates, hit)
    if cancel_event is not None and cancel_event.is_set():
        return None, RemoteFailure(host.alias, "timeout")

    factory = BoundedLineStream if stream_factory is None else stream_factory
    try:
        stream = factory(
            remote_watch_ssh_argv(
                host.alias,
                from_start=from_start,
                python_executable=bootstrap_executable,
            ),
            artifact,
            line_limit=MAX_WATCH_LINE_BYTES,
            stderr_limit=MAX_WATCH_STDERR_BYTES,
            startup_timeout=WATCH_STARTUP_TIMEOUT_SECONDS,
            cancel_event=cancel_event,
            ready_on_first_line=False,
        )
    except BoundedLineStreamError as error:
        return None, RemoteFailure(host.alias, _line_stream_error_code(error))
    except OSError:
        return None, RemoteFailure(host.alias, "unreachable")
    return RemoteWatchHostStream(host, stream), None


__all__ = [
    "END_FRAME",
    "PING_FRAME",
    "READY_FRAME",
    "REMOTE_WATCH_BOOTSTRAP",
    "RemoteWatchHostStream",
    "RemoteWatchTransportError",
    "open_remote_watch_host",
    "remote_watch_shell_command",
    "remote_watch_ssh_argv",
]
