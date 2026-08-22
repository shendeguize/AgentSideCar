"""Canonical SidecarClient API for the local JSONL Unix-socket protocol."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from sidecar.daemon import SOCKET_NAME, default_runtime_dir

DEFAULT_TIMEOUT = 1.0
DEFAULT_RESPONSE_BYTES = 2 * 1024 * 1024


class SidecarClientError(RuntimeError):
    """A connection, protocol, or daemon-declared client error."""

    def __init__(self, message: str, code: str = "client_error") -> None:
        super().__init__(message)
        self.code = code


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
        if timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("client bounds must be positive")
        root = default_runtime_dir() if runtime_dir is None else Path(runtime_dir).expanduser()
        self.socket_path = (
            root / SOCKET_NAME
            if socket_path is None
            else Path(socket_path).expanduser()
        )
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self._scan_errors: Tuple[Dict[str, Any], ...] = ()

    @property
    def scan_errors(self) -> List[Dict[str, Any]]:
        """Return defensive copies of errors from the latest status response."""

        return [dict(error) for error in self._scan_errors]

    def _connect(self) -> socket.socket:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
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

    def _request(self, operation: str) -> Dict[str, Any]:
        connection = self._connect()
        try:
            with connection:
                stream = connection.makefile("rwb", buffering=0)
                try:
                    request = json.dumps(
                        {"op": operation},
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
        self._scan_errors = tuple(dict(error) for error in scan_errors)
        return sessions

    def subscribe(self) -> Iterator[Dict[str, Any]]:
        """Yield normalized event dictionaries until disconnected or closed."""

        connection = self._connect()
        try:
            with connection:
                stream = connection.makefile("rwb", buffering=0)
                try:
                    try:
                        stream.write(b'{"op":"subscribe"}\n')
                        stream.flush()
                    except (OSError, socket.timeout) as error:
                        raise SidecarClientError(
                            "subscribe request failed: {}".format(error),
                            code="request_failed",
                        ) from error
                    acknowledgement = self._read_response(stream)
                    self._raise_daemon_error(acknowledgement)
                    if acknowledgement.get("op") != "subscribe":
                        raise SidecarClientError(
                            "daemon returned an invalid subscribe acknowledgement",
                            code="invalid_response",
                        )

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


def ping(**kwargs: Any) -> Dict[str, Any]:
    return SidecarClient(**kwargs).ping()


def status(**kwargs: Any) -> List[Dict[str, Any]]:
    return SidecarClient(**kwargs).status()


def subscribe(**kwargs: Any) -> Iterator[Dict[str, Any]]:
    return SidecarClient(**kwargs).subscribe()


__all__ = [
    "SidecarClient",
    "SidecarClientError",
    "ping",
    "status",
    "subscribe",
]
