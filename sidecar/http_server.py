"""Hardened, read-only loopback HTTP adapter for Agent Sidecar."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hmac
import json
import os
import re
import secrets
import select
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Set, Tuple

from sidecar.client import MAX_RESPONSE_BYTES, SidecarClient, SidecarClientError
from sidecar.web_panel import PANEL_HTML, render_panel

TOKEN_NAME = "http.token"
PORT_NAME = "http.port"

MAX_HEADER_BYTES = 16 * 1024
MAX_REQUEST_LINE_BYTES = 4096
REQUEST_TIMEOUT_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 5.0
CLOSE_TIMEOUT_SECONDS = 10.0
MAX_CLIENTS = 16
MAX_EVENT_STREAMS = 4
ACCEPT_BACKLOG = 32

_PANEL = PANEL_HTML

_HEADER_NAME = re.compile(br"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_METHOD = re.compile(br"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_STATUS_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Content Too Large",
    414: "URI Too Long",
    417: "Expectation Failed",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
    505: "HTTP Version Not Supported",
}


class HttpRuntimeError(RuntimeError):
    """The HTTP runtime directory or one of its private files is unsafe."""


class HttpServerCloseError(RuntimeError):
    """The HTTP server could not stop every owned thread within its bound."""


class _RequestError(Exception):
    def __init__(self, status: int, *, head_only: bool = False) -> None:
        super().__init__(_STATUS_REASONS[status])
        self.status = status
        self.head_only = head_only


@dataclass(frozen=True)
class _Request:
    method: str
    target: str
    headers: Dict[str, Tuple[bytes, ...]]


@dataclass(frozen=True)
class _PrivateFile:
    data: bytes
    identity: Tuple[int, int]


@dataclass(frozen=True)
class _PortRecord:
    port: int
    nonce: str
    identity: Tuple[int, int]


def _mode(details: os.stat_result) -> int:
    return stat.S_IMODE(details.st_mode)


def _identity(details: os.stat_result) -> Tuple[int, int]:
    return details.st_dev, details.st_ino


def _open_runtime_dir(path: Path) -> int:
    created = False
    try:
        details = path.lstat()
    except FileNotFoundError:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.mkdir(str(path), 0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise HttpRuntimeError("cannot create HTTP runtime directory") from error
        try:
            details = path.lstat()
        except OSError as error:
            raise HttpRuntimeError("cannot inspect HTTP runtime directory") from error
    except OSError as error:
        raise HttpRuntimeError("cannot inspect HTTP runtime directory") from error

    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise HttpRuntimeError("HTTP runtime path is not a real directory")
    if created:
        try:
            os.chmod(str(path), 0o700)
            details = path.lstat()
        except OSError as error:
            raise HttpRuntimeError("cannot secure HTTP runtime directory") from error
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise HttpRuntimeError("HTTP runtime directory changed during creation")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as error:
        raise HttpRuntimeError("cannot securely open HTTP runtime directory") from error
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or _mode(details) != 0o700
        ):
            raise HttpRuntimeError(
                "HTTP runtime directory must be owned by the current user "
                "with mode 0700"
            )
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_private_file(
    directory_fd: int,
    name: str,
    *,
    maximum: int,
) -> Optional[_PrivateFile]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise HttpRuntimeError("cannot securely open private HTTP metadata") from error

    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or _mode(details) != 0o600
            or details.st_nlink != 1
        ):
            raise HttpRuntimeError(
                "private HTTP metadata must be a current-user regular file "
                "with mode 0600"
            )
        chunks = bytearray()
        while len(chunks) <= maximum:
            piece = os.read(descriptor, min(4096, maximum + 1 - len(chunks)))
            if not piece:
                break
            chunks.extend(piece)
        if len(chunks) > maximum:
            raise HttpRuntimeError("private HTTP metadata is malformed")
        return _PrivateFile(bytes(chunks), _identity(details))
    except OSError as error:
        raise HttpRuntimeError("cannot read private HTTP metadata") from error
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _create_token_file(directory_fd: int, payload: bytes) -> None:
    temporary = ".http-token-{}".format(secrets.token_hex(16))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor: Optional[int] = None
    linked = False
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary,
                TOKEN_NAME,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            linked = True
        except FileExistsError:
            pass
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise HttpRuntimeError("cannot create private HTTP token") from error

    if not linked:
        return


def _decode_token(raw: bytes) -> str:
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise HttpRuntimeError("private HTTP token is malformed")
    encoded = raw[:-1]
    if not encoded or b"=" in encoded:
        raise HttpRuntimeError("private HTTP token is malformed")
    try:
        text = encoded.decode("ascii")
        padding = b"=" * ((4 - len(encoded) % 4) % 4)
        decoded = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeError, ValueError, binascii.Error) as error:
        raise HttpRuntimeError("private HTTP token is malformed") from error
    if len(decoded) < 32 or base64.urlsafe_b64encode(decoded).rstrip(b"=") != encoded:
        raise HttpRuntimeError("private HTTP token is malformed")
    return text


def _load_or_create_token(directory_fd: int) -> str:
    existing = _read_private_file(directory_fd, TOKEN_NAME, maximum=512)
    if existing is None:
        generated = secrets.token_urlsafe(32).encode("ascii") + b"\n"
        _create_token_file(directory_fd, generated)
        existing = _read_private_file(directory_fd, TOKEN_NAME, maximum=512)
    if existing is None:
        raise HttpRuntimeError("private HTTP token was not created")
    return _decode_token(existing.data)


def _port_payload(port: int, nonce: str) -> bytes:
    return (
        json.dumps(
            {"port": port, "nonce": nonce},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        + b"\n"
    )


def _decode_instance_nonce(value: object) -> str:
    if not isinstance(value, str) or not value or "=" in value:
        raise HttpRuntimeError("private HTTP port metadata is malformed")
    encoded = value.encode("ascii", "strict")
    try:
        decoded = base64.b64decode(
            encoded + b"=" * ((4 - len(encoded) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeError, ValueError, binascii.Error) as error:
        raise HttpRuntimeError("private HTTP port metadata is malformed") from error
    if len(decoded) < 16 or base64.urlsafe_b64encode(decoded).rstrip(b"=") != encoded:
        raise HttpRuntimeError("private HTTP port metadata is malformed")
    return value


def _load_port_record(directory_fd: int) -> Optional[_PortRecord]:
    existing = _read_private_file(directory_fd, PORT_NAME, maximum=512)
    if existing is None:
        return None
    try:
        if not existing.data.endswith(b"\n") or existing.data.count(b"\n") != 1:
            raise ValueError
        value = json.loads(existing.data[:-1].decode("ascii"))
        if (
            not isinstance(value, dict)
            or set(value) != {"port", "nonce"}
            or not isinstance(value["port"], int)
            or isinstance(value["port"], bool)
        ):
            raise ValueError
        port = value["port"]
        nonce = _decode_instance_nonce(value["nonce"])
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise HttpRuntimeError("private HTTP port metadata is malformed") from error
    if not 1 <= port <= 65535 or existing.data != _port_payload(port, nonce):
        raise HttpRuntimeError("private HTTP port metadata is malformed")
    return _PortRecord(port, nonce, existing.identity)


def _current_identity(directory_fd: int, name: str) -> Optional[Tuple[int, int]]:
    try:
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise HttpRuntimeError("cannot inspect private HTTP metadata") from error
    return _identity(details)


def _publish_port(
    directory_fd: int,
    port: int,
    nonce: str,
    expected: Optional[Tuple[int, int]],
) -> Tuple[int, int]:
    temporary = ".http-port-{}".format(secrets.token_hex(16))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, _port_payload(port, nonce))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if _current_identity(directory_fd, PORT_NAME) != expected:
            raise HttpRuntimeError("private HTTP port metadata changed during startup")
        os.replace(
            temporary,
            PORT_NAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        details = os.stat(PORT_NAME, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or _mode(details) != 0o600
        ):
            raise HttpRuntimeError("private HTTP port metadata is unsafe")
        return _identity(details)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise


def _remove_owned_port(
    directory_fd: int,
    owned_nonce: str,
    owned_identity: Optional[Tuple[int, int]],
) -> None:
    if not owned_nonce or owned_identity is None:
        return
    try:
        current = _load_port_record(directory_fd)
        if (
            current is not None
            and current.identity == owned_identity
            and hmac.compare_digest(current.nonce, owned_nonce)
        ):
            os.unlink(PORT_NAME, dir_fd=directory_fd)
            os.fsync(directory_fd)
    except (OSError, HttpRuntimeError):
        pass


def _encode_json_line(payload: Mapping[str, Any]) -> bytes:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )
    encoded = bytearray()
    for piece in encoder.iterencode(dict(payload)):
        chunk = piece.encode("utf-8")
        if len(encoded) + len(chunk) + 1 > MAX_RESPONSE_BYTES:
            raise ValueError("HTTP JSON response exceeds limit")
        encoded.extend(chunk)
    encoded.append(0x0A)
    return bytes(encoded)


class SidecarHttpServer:
    """Serve the read-only Sidecar protocol on numeric IPv4 loopback."""

    def __init__(
        self,
        runtime_dir: Path,
        socket_path: Path,
        port: int = 0,
        *,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 0 <= port <= 65535
        ):
            raise ValueError("HTTP port must be an integer from 0 through 65535")
        self.runtime_dir = Path(runtime_dir).expanduser()
        self.socket_path = Path(socket_path).expanduser()
        self._configured_port = port
        self._actual_port = 0
        self._client_factory = SidecarClient if client_factory is None else client_factory

        self._state_lock = threading.RLock()
        self._clients_lock = threading.Lock()
        self._listener: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._directory_fd: Optional[int] = None
        self._port_identity: Optional[Tuple[int, int]] = None
        self._instance_nonce = ""
        self._token = ""
        self._running = False
        self._closing = False
        self._stop = threading.Event()
        self._client_slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self._event_slots = threading.BoundedSemaphore(MAX_EVENT_STREAMS)
        self._connections: Set[socket.socket] = set()
        self._client_threads: Set[threading.Thread] = set()
        self._monitor_threads: Set[threading.Thread] = set()
        self._stream_cancels: Set[threading.Event] = set()

    @property
    def token_path(self) -> Path:
        return self.runtime_dir / TOKEN_NAME

    @property
    def port_path(self) -> Path:
        return self.runtime_dir / PORT_NAME

    @property
    def port(self) -> int:
        with self._state_lock:
            return self._actual_port if self._actual_port else self._configured_port

    @property
    def url(self) -> str:
        with self._state_lock:
            if not self._running or not self._actual_port:
                raise RuntimeError("HTTP server is not running")
            return "http://127.0.0.1:{}".format(self._actual_port)

    def start(self) -> "SidecarHttpServer":
        with self._state_lock:
            if self._running:
                raise RuntimeError("HTTP server is already running")
            if self._closing:
                raise RuntimeError("HTTP server is still closing")

            directory_fd = _open_runtime_dir(self.runtime_dir)
            listener: Optional[socket.socket] = None
            owned_port: Optional[Tuple[int, int]] = None
            instance_nonce = ""
            lock_acquired = False
            try:
                try:
                    fcntl.flock(
                        directory_fd,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    lock_acquired = True
                except BlockingIOError as error:
                    raise HttpRuntimeError(
                        "another HTTP server owns the runtime directory"
                    ) from error
                token = _load_or_create_token(directory_fd)
                previous = _load_port_record(directory_fd)

                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", self._configured_port))
                listener.listen(ACCEPT_BACKLOG)
                listener.settimeout(0.2)
                actual_port = int(listener.getsockname()[1])
                instance_nonce = secrets.token_urlsafe(24)
                owned_port = _publish_port(
                    directory_fd,
                    actual_port,
                    instance_nonce,
                    None if previous is None else previous.identity,
                )

                self._directory_fd = directory_fd
                self._port_identity = owned_port
                self._instance_nonce = instance_nonce
                self._listener = listener
                self._actual_port = actual_port
                self._token = token
                self._stop.clear()
                self._client_slots = threading.BoundedSemaphore(MAX_CLIENTS)
                self._event_slots = threading.BoundedSemaphore(MAX_EVENT_STREAMS)
                self._closing = False
                self._running = True
                thread = threading.Thread(
                    target=self._accept_loop,
                    name="agent-sidecar-http-accept",
                    daemon=True,
                )
                self._accept_thread = thread
                thread.start()
                return self
            except BaseException:
                if listener is not None:
                    listener.close()
                if lock_acquired:
                    _remove_owned_port(
                        directory_fd,
                        instance_nonce,
                        owned_port,
                    )
                    try:
                        fcntl.flock(directory_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(directory_fd)
                self._directory_fd = None
                self._port_identity = None
                self._instance_nonce = ""
                self._listener = None
                self._accept_thread = None
                self._actual_port = 0
                self._token = ""
                self._running = False
                self._closing = False
                raise

    def close(self) -> None:
        with self._state_lock:
            if not self._running and not self._closing:
                return
            self._closing = True
            self._running = False
            self._stop.set()
            listener = self._listener
            self._listener = None
            if listener is not None:
                try:
                    listener.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                listener.close()

            deadline = time.monotonic() + CLOSE_TIMEOUT_SECONDS
            accept_thread = self._accept_thread
            if (
                accept_thread is not None
                and accept_thread is not threading.current_thread()
            ):
                accept_thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if accept_thread is not None and accept_thread.is_alive():
                raise HttpServerCloseError(
                    "HTTP accept thread did not stop within the close bound"
                )

            while True:
                with self._clients_lock:
                    cancellations = tuple(self._stream_cancels)
                    connections = tuple(self._connections)
                    workers = tuple(self._client_threads)
                    monitors = tuple(self._monitor_threads)
                owned_threads = workers + monitors
                joinable = tuple(
                    worker
                    for worker in owned_threads
                    if worker is not threading.current_thread()
                )
                if (
                    not cancellations
                    and not connections
                    and not owned_threads
                ):
                    break
                for cancellation in cancellations:
                    cancellation.set()
                for connection in connections:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        connection.close()
                    except OSError:
                        pass
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HttpServerCloseError(
                        "HTTP owned threads did not stop within the close bound"
                    )
                for worker in joinable:
                    worker.join(timeout=min(0.2, remaining))
                if not joinable:
                    time.sleep(min(0.01, remaining))

            directory_fd = self._directory_fd
            if directory_fd is not None:
                _remove_owned_port(
                    directory_fd,
                    self._instance_nonce,
                    self._port_identity,
                )
                try:
                    fcntl.flock(directory_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(directory_fd)
            self._directory_fd = None
            self._port_identity = None
            self._instance_nonce = ""
            self._accept_thread = None
            self._actual_port = 0
            self._token = ""
            self._closing = False

    def __enter__(self) -> "SidecarHttpServer":
        return self.start()

    def __exit__(self, *unused: Any) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return

            if self._closing or self._stop.is_set():
                connection.close()
                return

            if not self._client_slots.acquire(blocking=False):
                try:
                    self._send_response(
                        connection,
                        503,
                        self._error_body("unavailable"),
                        content_type="application/json; charset=utf-8",
                    )
                except OSError:
                    pass
                finally:
                    connection.close()
                continue

            thread = threading.Thread(
                target=self._handle_client,
                args=(connection,),
                name="agent-sidecar-http-client",
                daemon=True,
            )
            with self._clients_lock:
                self._connections.add(connection)
                self._client_threads.add(thread)
            try:
                thread.start()
            except BaseException:
                with self._clients_lock:
                    self._connections.discard(connection)
                    self._client_threads.discard(thread)
                connection.close()
                self._client_slots.release()

    def _handle_client(self, connection: socket.socket) -> None:
        current = threading.current_thread()
        try:
            request = self._read_request(connection)
            if request is None:
                return
            self._dispatch(connection, request)
        except _RequestError as error:
            try:
                self._send_response(
                    connection,
                    error.status,
                    self._error_body("bad_request"),
                    head_only=error.head_only,
                    content_type="application/json; charset=utf-8",
                )
            except OSError:
                pass
        except (OSError, SidecarClientError):
            return
        except Exception:
            try:
                self._send_response(
                    connection,
                    500,
                    self._error_body("internal_error"),
                    content_type="application/json; charset=utf-8",
                )
            except OSError:
                pass
        finally:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
            with self._clients_lock:
                self._connections.discard(connection)
                self._client_threads.discard(current)
            self._client_slots.release()

    @staticmethod
    def _read_request(connection: socket.socket) -> Optional[_Request]:
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        buffered = bytearray()
        request_line_end = -1
        while request_line_end < 0:
            newline = buffered.find(b"\n")
            if newline >= 0:
                request_line_end = newline + 1
                if request_line_end > MAX_REQUEST_LINE_BYTES:
                    raise _RequestError(414)
                if newline == 0 or buffered[newline - 1] != 0x0D:
                    raise _RequestError(400)
                break
            if len(buffered) > MAX_REQUEST_LINE_BYTES:
                raise _RequestError(414)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _RequestError(408)
            connection.settimeout(remaining)
            try:
                capacity = MAX_REQUEST_LINE_BYTES - len(buffered)
                chunk = connection.recv(min(4096, capacity + 1))
            except socket.timeout as error:
                raise _RequestError(408) from error
            if not chunk:
                return None
            buffered.extend(chunk)

        marker = buffered.find(b"\r\n\r\n")
        while marker < 0:
            if len(buffered) > MAX_HEADER_BYTES:
                raise _RequestError(431)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _RequestError(408)
            connection.settimeout(remaining)
            try:
                capacity = MAX_HEADER_BYTES - len(buffered)
                chunk = connection.recv(min(4096, capacity + 1))
            except socket.timeout as error:
                raise _RequestError(408) from error
            if not chunk:
                return None
            buffered.extend(chunk)
            marker = buffered.find(b"\r\n\r\n")

        header_end = marker + 4
        if header_end > MAX_HEADER_BYTES:
            raise _RequestError(431)
        if len(buffered) != header_end:
            raise _RequestError(413)

        lines = bytes(buffered[:marker]).split(b"\r\n")
        if not lines or len(lines[0]) + 2 > MAX_REQUEST_LINE_BYTES:
            raise _RequestError(400)
        request_parts = lines[0].split(b" ")
        if (
            len(request_parts) != 3
            or not request_parts[0]
            or not request_parts[1]
            or not request_parts[2]
            or not _METHOD.fullmatch(request_parts[0])
        ):
            raise _RequestError(400)
        try:
            method = request_parts[0].decode("ascii")
            target = request_parts[1].decode("ascii")
            version = request_parts[2].decode("ascii")
        except UnicodeError as error:
            raise _RequestError(400) from error
        head_only = method == "HEAD"
        if version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise _RequestError(505, head_only=head_only)
        if not target.startswith("/") or target.startswith("//") or "#" in target:
            raise _RequestError(400, head_only=head_only)

        parsed: Dict[str, list] = {}
        for line in lines[1:]:
            if (
                not line
                or line[:1] in {b" ", b"\t"}
                or b":" not in line
                or b"\x00" in line
            ):
                raise _RequestError(400, head_only=head_only)
            name, value = line.split(b":", 1)
            if not _HEADER_NAME.fullmatch(name):
                raise _RequestError(400, head_only=head_only)
            value = value.strip(b" \t")
            if any(byte < 0x20 and byte != 0x09 for byte in value) or any(
                byte == 0x7F for byte in value
            ):
                raise _RequestError(400, head_only=head_only)
            key = name.decode("ascii").lower()
            parsed.setdefault(key, []).append(value)

        headers = {key: tuple(values) for key, values in parsed.items()}
        for singular in ("host", "authorization", "content-length", "origin"):
            if len(headers.get(singular, ())) > 1:
                raise _RequestError(400, head_only=head_only)
        if "transfer-encoding" in headers:
            raise _RequestError(400, head_only=head_only)
        if "expect" in headers:
            raise _RequestError(417, head_only=head_only)
        lengths = headers.get("content-length", ())
        if lengths:
            if not lengths[0].isdigit():
                raise _RequestError(400, head_only=head_only)
            if int(lengths[0]) != 0:
                raise _RequestError(413, head_only=head_only)
        if method not in {"GET", "HEAD"}:
            raise _RequestError(405)
        return _Request(method, target, headers)

    def _dispatch(self, connection: socket.socket, request: _Request) -> None:
        head_only = request.method == "HEAD"
        expected_host = "127.0.0.1:{}".format(self._actual_port).encode("ascii")
        if request.headers.get("host") != (expected_host,):
            self._send_response(
                connection,
                400,
                self._error_body("bad_request"),
                head_only=head_only,
                content_type="application/json; charset=utf-8",
            )
            return

        origins = request.headers.get("origin", ())
        expected_origin = "http://127.0.0.1:{}".format(self._actual_port).encode(
            "ascii"
        )
        if origins and origins != (expected_origin,):
            self._send_response(
                connection,
                403,
                self._error_body("forbidden"),
                head_only=head_only,
                content_type="application/json; charset=utf-8",
            )
            return

        path, separator, _ = request.target.partition("?")
        if path.startswith("/api/") and separator:
            self._send_response(
                connection,
                400,
                self._error_body("bad_request"),
                head_only=head_only,
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/" and not separator:
            nonce = secrets.token_urlsafe(18)
            body = render_panel(nonce).encode("utf-8")
            self._send_response(
                connection,
                200,
                body,
                head_only=head_only,
                content_type="text/html; charset=utf-8",
                nonce=nonce,
            )
            return
        if path == "/api/v1/health":
            self._send_response(
                connection,
                200,
                b'{"ok":true}\n',
                head_only=head_only,
                content_type="application/json; charset=utf-8",
            )
            return
        if path not in {"/api/v1/status", "/api/v1/events"}:
            self._send_response(
                connection,
                404,
                self._error_body("not_found"),
                head_only=head_only,
                content_type="application/json; charset=utf-8",
            )
            return
        if not self._authorized(request):
            self._send_response(
                connection,
                401,
                self._error_body("unauthorized"),
                head_only=head_only,
                content_type="application/json; charset=utf-8",
            )
            return
        if path == "/api/v1/status":
            self._serve_status(connection, head_only)
            return
        self._serve_events(connection, head_only)

    def _authorized(self, request: _Request) -> bool:
        values = request.headers.get("authorization", ())
        if len(values) != 1:
            return False
        expected = ("Bearer " + self._token).encode("ascii")
        return hmac.compare_digest(values[0], expected)

    def _new_client(self) -> Any:
        return self._client_factory(socket_path=self.socket_path)

    def _serve_status(self, connection: socket.socket, head_only: bool) -> None:
        try:
            client = self._new_client()
            sessions = client.status()
            body = _encode_json_line(
                {
                    "ok": True,
                    "op": "status",
                    "sessions": sessions,
                    "scan_errors": client.scan_errors,
                    "tail_errors": client.tail_errors,
                }
            )
        except (OSError, SidecarClientError, ValueError, TypeError):
            self._send_response(
                connection,
                502,
                self._error_body("upstream_error"),
                head_only=head_only,
                content_type="application/json; charset=utf-8",
            )
            return
        self._send_response(
            connection,
            200,
            body,
            head_only=head_only,
            content_type="application/json; charset=utf-8",
        )

    def _serve_events(self, connection: socket.socket, head_only: bool) -> None:
        if head_only:
            self._send_stream_headers(connection)
            return
        if not self._event_slots.acquire(blocking=False):
            self._send_response(
                connection,
                503,
                self._error_body("unavailable"),
                content_type="application/json; charset=utf-8",
            )
            return

        cancellation = threading.Event()
        finished = threading.Event()
        monitor: Optional[threading.Thread] = None
        iterator: Any = None
        with self._clients_lock:
            self._stream_cancels.add(cancellation)
        try:
            client = self._new_client()
            iterator = iter(client.subscribe(cancel_event=cancellation))
            self._send_stream_headers(connection)
            connection.settimeout(WRITE_TIMEOUT_SECONDS)
            connection.sendall(_encode_json_line({"ok": True, "op": "subscribe"}))

            monitor = threading.Thread(
                target=self._monitor_disconnect,
                args=(connection, cancellation, finished),
                name="agent-sidecar-http-disconnect",
                daemon=True,
            )
            with self._clients_lock:
                self._monitor_threads.add(monitor)
            try:
                monitor.start()
            except BaseException:
                with self._clients_lock:
                    self._monitor_threads.discard(monitor)
                monitor = None
                raise
            for event in iterator:
                if cancellation.is_set() or self._stop.is_set():
                    break
                if not isinstance(event, Mapping):
                    break
                connection.settimeout(WRITE_TIMEOUT_SECONDS)
                connection.sendall(_encode_json_line(event))
        except (OSError, SidecarClientError, ValueError, TypeError):
            pass
        finally:
            cancellation.set()
            finished.set()
            close_iterator = getattr(iterator, "close", None)
            if callable(close_iterator):
                try:
                    close_iterator()
                except Exception:
                    pass
            if monitor is not None and monitor is not threading.current_thread():
                monitor.join()
            with self._clients_lock:
                if monitor is not None:
                    self._monitor_threads.discard(monitor)
                self._stream_cancels.discard(cancellation)
            self._event_slots.release()

    @staticmethod
    def _monitor_disconnect(
        connection: socket.socket,
        cancellation: threading.Event,
        finished: threading.Event,
    ) -> None:
        while not finished.is_set() and not cancellation.is_set():
            try:
                readable, _, _ = select.select([connection], [], [], 0.2)
                if not readable:
                    continue
                pending = connection.recv(1, socket.MSG_PEEK)
                if not pending:
                    cancellation.set()
                    return
                # A second request or an unframed body is never accepted.
                cancellation.set()
                return
            except (OSError, ValueError):
                cancellation.set()
                return

    @staticmethod
    def _error_body(code: str) -> bytes:
        return _encode_json_line({"ok": False, "error": code})

    @staticmethod
    def _security_headers(nonce: str) -> Tuple[Tuple[str, str], ...]:
        policy = (
            "default-src 'none'; "
            "script-src 'nonce-{}'; "
            "style-src 'nonce-{}'; "
            "connect-src 'self'; "
            "img-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "form-action 'self'"
        ).format(nonce, nonce)
        return (
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"),
            ("Referrer-Policy", "no-referrer"),
            ("Content-Security-Policy", policy),
        )

    def _send_response(
        self,
        connection: socket.socket,
        status: int,
        body: bytes,
        *,
        head_only: bool = False,
        content_type: str,
        nonce: Optional[str] = None,
    ) -> None:
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("HTTP response exceeds limit")
        response_nonce = secrets.token_urlsafe(18) if nonce is None else nonce
        headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Connection", "close"),
        ]
        headers.extend(self._security_headers(response_nonce))
        self._write_headers(connection, status, tuple(headers))
        if not head_only and body:
            connection.settimeout(WRITE_TIMEOUT_SECONDS)
            connection.sendall(body)

    def _send_stream_headers(self, connection: socket.socket) -> None:
        nonce = secrets.token_urlsafe(18)
        headers = [
            ("Content-Type", "application/x-ndjson; charset=utf-8"),
            ("Connection", "close"),
        ]
        headers.extend(self._security_headers(nonce))
        self._write_headers(connection, 200, tuple(headers))

    @staticmethod
    def _write_headers(
        connection: socket.socket,
        status: int,
        headers: Tuple[Tuple[str, str], ...],
    ) -> None:
        reason = _STATUS_REASONS[status]
        lines = ["HTTP/1.1 {} {}".format(status, reason)]
        lines.extend("{}: {}".format(name, value) for name, value in headers)
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        connection.settimeout(WRITE_TIMEOUT_SECONDS)
        connection.sendall(payload)


__all__ = [
    "ACCEPT_BACKLOG",
    "CLOSE_TIMEOUT_SECONDS",
    "MAX_CLIENTS",
    "MAX_EVENT_STREAMS",
    "MAX_HEADER_BYTES",
    "MAX_REQUEST_LINE_BYTES",
    "PORT_NAME",
    "TOKEN_NAME",
    "HttpRuntimeError",
    "HttpServerCloseError",
    "SidecarHttpServer",
]
