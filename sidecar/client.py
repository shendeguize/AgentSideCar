"""Canonical SidecarClient API for the local JSONL Unix-socket protocol."""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
)

from sidecar.daemon import REPLAY_MAX_LIMIT, SOCKET_NAME, default_runtime_dir

DEFAULT_TIMEOUT = 1.0
DEFAULT_REPLAY_TIMEOUT = 15.0
CANCEL_POLL_SECONDS = 0.25
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
DEFAULT_RESPONSE_BYTES = MAX_RESPONSE_BYTES


class SidecarClientError(RuntimeError):
    """A connection, protocol, or daemon-declared client error."""

    def __init__(self, message: str, code: str = "client_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class HttpPingInfo:
    """Typed HTTP listener details advertised by the Unix daemon."""

    enabled: bool
    host: Optional[str] = None
    port: Optional[int] = None

    @classmethod
    def from_value(cls, value: object) -> "HttpPingInfo":
        if value is None:
            return cls(enabled=False)
        if not isinstance(value, Mapping) or not isinstance(
            value.get("enabled"),
            bool,
        ):
            raise SidecarClientError(
                "daemon returned invalid HTTP ping information",
                code="invalid_response",
            )
        if value["enabled"] is False:
            return cls(enabled=False)
        host = value.get("host")
        port = value.get("port")
        if (
            not isinstance(host, str)
            or not host
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            raise SidecarClientError(
                "daemon returned invalid HTTP ping information",
                code="invalid_response",
            )
        return cls(enabled=True, host=host, port=port)


@dataclass(frozen=True)
class PingInfo:
    """Typed daemon health information, including optional HTTP state."""

    pid: int
    version: str
    http: HttpPingInfo

    @classmethod
    def from_response(cls, response: object) -> "PingInfo":
        if (
            not isinstance(response, Mapping)
            or response.get("ok") is not True
            or response.get("op") != "ping"
        ):
            raise SidecarClientError(
                "daemon returned an invalid ping",
                code="invalid_response",
            )
        pid = response.get("pid")
        if isinstance(pid, bool):
            raise SidecarClientError(
                "daemon returned an invalid pid",
                code="invalid_response",
            )
        try:
            parsed_pid = int(pid)
        except (TypeError, ValueError) as error:
            raise SidecarClientError(
                "daemon returned an invalid pid",
                code="invalid_response",
            ) from error
        if parsed_pid <= 0:
            raise SidecarClientError(
                "daemon returned an invalid pid",
                code="invalid_response",
            )
        version = response.get("version")
        if version is None:
            rendered_version = ""
        elif isinstance(version, str):
            rendered_version = version
        else:
            raise SidecarClientError(
                "daemon returned an invalid version",
                code="invalid_response",
            )
        return cls(
            pid=parsed_pid,
            version=rendered_version,
            http=HttpPingInfo.from_value(response.get("http")),
        )


class SidecarClient:
    """Bounded client for daemon status, health, and event subscriptions."""

    def __init__(
        self,
        runtime_dir: Optional[Path] = None,
        *,
        socket_path: Optional[Path] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
    ) -> None:
        if (
            timeout <= 0
            or max_response_bytes <= 0
            or max_response_bytes > MAX_RESPONSE_BYTES
        ):
            raise ValueError("client bounds are invalid")
        root = default_runtime_dir() if runtime_dir is None else Path(runtime_dir).expanduser()
        self.socket_path = (
            root / SOCKET_NAME
            if socket_path is None
            else Path(socket_path).expanduser()
        )
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self._scan_errors: Tuple[Dict[str, Any], ...] = ()
        self._tail_errors: Tuple[Dict[str, Any], ...] = ()

    @property
    def scan_errors(self) -> List[Dict[str, Any]]:
        """Return defensive copies of errors from the latest status response."""

        return [dict(error) for error in self._scan_errors]

    @property
    def tail_errors(self) -> List[Dict[str, Any]]:
        """Return defensive copies of tail errors from the latest status."""

        return [dict(error) for error in self._tail_errors]

    def _connect(self, timeout: Optional[float] = None) -> socket.socket:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout if timeout is None else timeout)
        try:
            connection.connect(str(self.socket_path))
        except (OSError, socket.timeout) as error:
            connection.close()
            raise SidecarClientError(
                "cannot connect to daemon at {}: {}".format(self.socket_path, error),
                code="connection_failed",
            ) from error
        return connection

    def _read_response(self, stream: Any) -> Dict[str, Any]:
        try:
            raw = stream.readline(self.max_response_bytes + 1)
        except (OSError, socket.timeout, ValueError) as error:
            raise SidecarClientError(
                "failed reading daemon response: {}".format(error),
                code="read_failed",
            ) from error
        if not raw:
            raise SidecarClientError(
                "daemon closed the connection",
                code="connection_closed",
            )
        if len(raw) > self.max_response_bytes:
            raise SidecarClientError(
                "daemon response exceeds {} bytes".format(self.max_response_bytes),
                code="response_too_large",
            )
        return self._decode_response(raw)

    @staticmethod
    def _decode_response(raw: bytes) -> Dict[str, Any]:
        try:
            response = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as error:
            raise SidecarClientError(
                "daemon returned invalid JSON",
                code="invalid_response",
            ) from error
        if not isinstance(response, dict):
            raise SidecarClientError(
                "daemon response is not an object",
                code="invalid_response",
            )
        return response

    def _read_cancelable_response(
        self,
        connection: socket.socket,
        pending: bytearray,
        cancel_event: threading.Event,
    ) -> Optional[Dict[str, Any]]:
        while not cancel_event.is_set():
            newline = pending.find(b"\n")
            if newline >= 0:
                end = newline + 1
                raw = bytes(pending[:end])
                del pending[:end]
                if len(raw) > self.max_response_bytes:
                    raise SidecarClientError(
                        "daemon response exceeds {} bytes".format(
                            self.max_response_bytes
                        ),
                        code="response_too_large",
                    )
                return self._decode_response(raw)
            if len(pending) > self.max_response_bytes:
                raise SidecarClientError(
                    "daemon response exceeds {} bytes".format(
                        self.max_response_bytes
                    ),
                    code="response_too_large",
                )
            try:
                chunk = connection.recv(
                    min(
                        64 * 1024,
                        self.max_response_bytes + 1 - len(pending),
                    )
                )
            except socket.timeout:
                continue
            except OSError as error:
                raise SidecarClientError(
                    "failed reading daemon response: {}".format(error),
                    code="read_failed",
                ) from error
            if not chunk:
                if pending:
                    raw = bytes(pending)
                    pending.clear()
                    return self._decode_response(raw)
                raise SidecarClientError(
                    "daemon closed the connection",
                    code="connection_closed",
                )
            pending.extend(chunk)
        return None

    @staticmethod
    def _raise_daemon_error(response: Mapping[str, Any]) -> None:
        if response.get("ok") is not False:
            return
        error = response.get("error")
        if isinstance(error, Mapping):
            code = str(error.get("code") or "daemon_error")
            message = str(error.get("message") or code)
        else:
            code = "daemon_error"
            message = str(error or code)
        raise SidecarClientError(message, code=code)

    def _validate_subscribe_acknowledgement(
        self,
        response: object,
    ) -> None:
        if not isinstance(response, Mapping):
            raise SidecarClientError(
                "daemon returned an invalid subscribe acknowledgement",
                code="invalid_response",
            )
        self._raise_daemon_error(response)
        if (
            response.get("ok") is not True
            or response.get("op") != "subscribe"
            or "error" in response
        ):
            raise SidecarClientError(
                "daemon returned an invalid subscribe acknowledgement",
                code="invalid_response",
            )

    def _request(
        self,
        operation: str,
        *,
        timeout: Optional[float] = None,
        **fields: Any
    ) -> Dict[str, Any]:
        connection = self._connect(timeout=timeout)
        try:
            with connection:
                stream = connection.makefile("rwb")
                try:
                    payload: Dict[str, Any] = {"op": operation}
                    payload.update(fields)
                    request = json.dumps(
                        payload,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    stream.write(request + b"\n")
                    stream.flush()
                    response = self._read_response(stream)
                except (OSError, socket.timeout) as error:
                    raise SidecarClientError(
                        "daemon request failed: {}".format(error),
                        code="request_failed",
                    ) from error
                finally:
                    try:
                        stream.close()
                    except OSError:
                        pass
        finally:
            connection.close()
        self._raise_daemon_error(response)
        return response

    def ping(self) -> Dict[str, Any]:
        return self._request("ping")

    def ping_info(self) -> PingInfo:
        """Return typed ping details while keeping ``ping`` wire-compatible."""

        return PingInfo.from_response(self.ping())

    def status(self) -> List[Dict[str, Any]]:
        response = self._request("status")
        sessions = response.get("sessions")
        if not isinstance(sessions, list) or not all(
            isinstance(session, dict) for session in sessions
        ):
            raise SidecarClientError(
                "daemon status response has no valid sessions list",
                code="invalid_response",
            )
        scan_errors = response.get("scan_errors", [])
        if not isinstance(scan_errors, list) or not all(
            isinstance(error, dict) for error in scan_errors
        ):
            raise SidecarClientError(
                "daemon status response has no valid scan_errors list",
                code="invalid_response",
            )
        tail_errors = response.get("tail_errors", [])
        if not isinstance(tail_errors, list) or not all(
            isinstance(error, dict) for error in tail_errors
        ):
            raise SidecarClientError(
                "daemon status response has no valid tail_errors list",
                code="invalid_response",
            )
        self._scan_errors = tuple(dict(error) for error in scan_errors)
        self._tail_errors = tuple(dict(error) for error in tail_errors)
        return sessions

    def replay(
        self,
        session_id: str,
        after_seq: int = 0,
        *,
        limit: Optional[int] = None,
        timeout: Optional[float] = DEFAULT_REPLAY_TIMEOUT,
    ) -> Dict[str, Any]:
        """Return one bounded page of replayed events after ``after_seq``.

        The response dictionary carries ``events``, ``last_seq``, and
        ``truncated`` so callers can page with the returned cursor.
        ``limit`` must be an integer from 1 through the daemon maximum of
        1024. The default per-request ``timeout`` covers the daemon's
        bounded transcript decode; pass ``None`` to use the client timeout
        instead.
        """

        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a nonempty string")
        if (
            isinstance(after_seq, bool)
            or not isinstance(after_seq, int)
            or after_seq < 0
        ):
            raise ValueError("after_seq must be a nonnegative integer")
        fields: Dict[str, Any] = {
            "session_id": session_id,
            "after_seq": after_seq,
        }
        if limit is not None:
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= REPLAY_MAX_LIMIT
            ):
                raise ValueError(
                    "limit must be an integer from 1 through {}".format(
                        REPLAY_MAX_LIMIT
                    )
                )
            fields["limit"] = limit
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        response = self._request("replay", timeout=timeout, **fields)
        events = response.get("events")
        if (
            response.get("op") != "replay"
            or not isinstance(events, list)
            or not all(isinstance(event, dict) for event in events)
        ):
            raise SidecarClientError(
                "daemon replay response has no valid events list",
                code="invalid_response",
            )
        return response

    @staticmethod
    def _subscribe_request(agents: Optional[Iterable[str]]) -> bytes:
        if agents is None:
            return b'{"op":"subscribe"}\n'
        names = list(agents)
        if not names or any(
            not isinstance(name, str) or not name for name in names
        ):
            raise ValueError(
                "agents must be a nonempty collection of nonempty agent names"
            )
        return (
            json.dumps(
                {"op": "subscribe", "agents": names},
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def subscribe(
        self,
        cancel_event: Optional[threading.Event] = None,
        on_ready: Optional[Callable[[], None]] = None,
        agents: Optional[Iterable[str]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield normalized event dictionaries until disconnected or closed.

        An optional ``agents`` allowlist asks the daemon to stream only
        events from those agent names; omitting it keeps the full stream.
        """

        request = self._subscribe_request(agents)
        if cancel_event is not None:
            if cancel_event.is_set():
                return
            yield from self._subscribe_cancelable(
                cancel_event,
                on_ready=on_ready,
                request=request,
            )
            return

        connection = self._connect()
        try:
            with connection:
                stream = connection.makefile("rwb")
                try:
                    try:
                        stream.write(request)
                        stream.flush()
                    except (OSError, socket.timeout) as error:
                        raise SidecarClientError(
                            "subscribe request failed: {}".format(error),
                            code="request_failed",
                        ) from error
                    acknowledgement = self._read_response(stream)
                    self._validate_subscribe_acknowledgement(
                        acknowledgement
                    )
                    if on_ready is not None:
                        on_ready()

                    # The connection may remain idle indefinitely after its bounded
                    # connect/handshake phase.
                    connection.settimeout(None)
                    while True:
                        event = self._read_response(stream)
                        self._raise_daemon_error(event)
                        yield event
                finally:
                    try:
                        stream.close()
                    except OSError:
                        pass
        finally:
            connection.close()

    def _subscribe_cancelable(
        self,
        cancel_event: threading.Event,
        *,
        on_ready: Optional[Callable[[], None]] = None,
        request: bytes = b'{"op":"subscribe"}\n',
    ) -> Iterator[Dict[str, Any]]:
        connection = self._connect()
        pending = bytearray()
        try:
            with connection:
                connection.settimeout(min(self.timeout, CANCEL_POLL_SECONDS))
                if cancel_event.is_set():
                    return
                try:
                    connection.sendall(request)
                except (OSError, socket.timeout) as error:
                    raise SidecarClientError(
                        "subscribe request failed: {}".format(error),
                        code="request_failed",
                    ) from error
                acknowledgement = self._read_cancelable_response(
                    connection,
                    pending,
                    cancel_event,
                )
                if acknowledgement is None:
                    return
                self._validate_subscribe_acknowledgement(acknowledgement)
                if on_ready is not None:
                    on_ready()

                while not cancel_event.is_set():
                    event = self._read_cancelable_response(
                        connection,
                        pending,
                        cancel_event,
                    )
                    if event is None:
                        return
                    self._raise_daemon_error(event)
                    yield event
        finally:
            connection.close()


def ping(**kwargs: Any) -> Dict[str, Any]:
    return SidecarClient(**kwargs).ping()


def status(**kwargs: Any) -> List[Dict[str, Any]]:
    return SidecarClient(**kwargs).status()


def replay(
    session_id: str,
    *,
    after_seq: int = 0,
    limit: Optional[int] = None,
    timeout: Optional[float] = DEFAULT_REPLAY_TIMEOUT,
    **kwargs: Any
) -> Dict[str, Any]:
    return SidecarClient(**kwargs).replay(
        session_id,
        after_seq,
        limit=limit,
        timeout=timeout,
    )


def subscribe(
    *,
    cancel_event: Optional[threading.Event] = None,
    on_ready: Optional[Callable[[], None]] = None,
    agents: Optional[Iterable[str]] = None,
    **kwargs: Any
) -> Iterator[Dict[str, Any]]:
    return SidecarClient(**kwargs).subscribe(
        cancel_event=cancel_event,
        on_ready=on_ready,
        agents=agents,
    )


__all__ = [
    "CANCEL_POLL_SECONDS",
    "DEFAULT_REPLAY_TIMEOUT",
    "DEFAULT_RESPONSE_BYTES",
    "HttpPingInfo",
    "MAX_RESPONSE_BYTES",
    "PingInfo",
    "SidecarClient",
    "SidecarClientError",
    "ping",
    "replay",
    "status",
    "subscribe",
]
