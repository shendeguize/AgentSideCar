"""Private, bounded JSONL diagnostics for the long-running daemon."""

from __future__ import annotations

import datetime
import errno
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised in an isolated import test
    _fcntl = None

LOG_NAME = "daemon.jsonl"
LOCK_NAME = "daemon.log.lock"
SCHEMA_VERSION = 1
MAX_CURRENT_BYTES = 2 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024
DEFAULT_BACKUPS = 2
MAX_BACKUPS = 3

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_LEVELS = frozenset(("debug", "info", "warning", "error", "critical"))
_STRING_LIMITS = {
    "component": 48,
    "event": 64,
    "level": 16,
    "version": 64,
    "adapter": 64,
    "agent": 64,
    "code": 128,
    "stage": 64,
}
_OPTIONAL_FIELDS = frozenset(
    (
        "component",
        "level",
        "http_enabled",
        "http_port",
        "count",
        "adapter",
        "agent",
        "session_id",
        "code",
        "stage",
        "timed_out",
    )
)
_REQUIRED_FIELDS = frozenset(
    (
        "schema_version",
        "ts",
        "ts_epoch",
        "component",
        "event",
        "level",
        "version",
        "pid",
    )
)
_ALLOWED_FIELDS = _REQUIRED_FIELDS.union(_OPTIONAL_FIELDS)


class DaemonLogError(RuntimeError):
    """A path-free failure to establish a safe daemon log."""

    def __init__(self, code: str) -> None:
        self.code = _safe_code(code, "log_error")
        super().__init__(self.code)


def _safe_code(value: object, fallback: str) -> str:
    if (
        isinstance(value, str)
        and len(value) <= 128
        and _IDENTIFIER.fullmatch(value)
    ):
        return value
    return fallback


def _safe_identifier(value: object, limit: int, fallback: str = "unknown") -> str:
    if not isinstance(value, str):
        return fallback
    if not value or len(value) > limit or not _IDENTIFIER.fullmatch(value):
        return fallback
    return value


def _mode(details: os.stat_result) -> int:
    return stat.S_IMODE(details.st_mode)


def _identity(details: os.stat_result) -> Tuple[int, int]:
    return (int(details.st_dev), int(details.st_ino))


def _is_private_regular(details: os.stat_result) -> bool:
    return (
        stat.S_ISREG(details.st_mode)
        and details.st_uid == os.geteuid()
        and _mode(details) == 0o600
        and details.st_nlink == 1
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        offset += written


class DaemonLog:
    """Own one secure daemon log generation and its rotation lock."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        version: str = "",
        max_bytes: int = MAX_CURRENT_BYTES,
        backups: int = DEFAULT_BACKUPS,
        line_bytes: int = MAX_LINE_BYTES,
    ) -> None:
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes <= 0
            or not isinstance(line_bytes, int)
            or isinstance(line_bytes, bool)
            or line_bytes <= 0
            or line_bytes > MAX_LINE_BYTES
            or not isinstance(backups, int)
            or isinstance(backups, bool)
            or not 0 <= backups <= MAX_BACKUPS
        ):
            raise ValueError("daemon log bounds are invalid")
        self.runtime_dir = Path(runtime_dir).expanduser()
        self.version = _safe_identifier(version, _STRING_LIMITS["version"], "")
        self.max_bytes = max_bytes
        self.backups = backups
        self.line_bytes = line_bytes
        self._directory_fd: Optional[int] = None
        self._lock_fd: Optional[int] = None
        self._log_fd: Optional[int] = None
        self._log_identity: Optional[Tuple[int, int]] = None
        self._opened = False
        self._disabled = False
        self._closed = False
        self._error_code: Optional[str] = None
        self._state_lock = threading.RLock()

    @property
    def error_code(self) -> Optional[str]:
        with self._state_lock:
            return self._error_code

    @property
    def disabled(self) -> bool:
        with self._state_lock:
            return self._disabled

    def _open_directory(self) -> int:
        if (
            _fcntl is None
            or getattr(os, "O_NOFOLLOW", None) is None
            or os.open not in os.supports_dir_fd
            or os.stat not in os.supports_dir_fd
            or os.unlink not in os.supports_dir_fd
        ):
            raise DaemonLogError("unsupported_platform")
        flags = os.O_RDONLY | os.O_NOFOLLOW
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(str(self.runtime_dir), flags)
        except OSError as error:
            raise DaemonLogError("unsafe_runtime") from error
        try:
            details = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(details.st_mode)
                or details.st_uid != os.geteuid()
                or _mode(details) != 0o700
            ):
                raise DaemonLogError("unsafe_runtime")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _entry_stat(directory_fd: int, name: str) -> Optional[os.stat_result]:
        try:
            return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise DaemonLogError("unsafe_log") from error

    @classmethod
    def _validate_entry(
        cls,
        directory_fd: int,
        name: str,
        *,
        maximum: Optional[int] = None,
    ) -> Optional[os.stat_result]:
        details = cls._entry_stat(directory_fd, name)
        if details is None:
            return None
        if not _is_private_regular(details):
            raise DaemonLogError("unsafe_log")
        if maximum is not None and details.st_size > maximum:
            raise DaemonLogError("unsafe_log_size")
        return details

    @classmethod
    def _open_private_file(
        cls,
        directory_fd: int,
        name: str,
        *,
        maximum: Optional[int] = None,
    ) -> Tuple[int, Tuple[int, int]]:
        flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        created = False
        try:
            descriptor = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            created = True
        except FileExistsError:
            before = cls._validate_entry(directory_fd, name, maximum=maximum)
            assert before is not None
            try:
                descriptor = os.open(name, flags, dir_fd=directory_fd)
            except OSError as error:
                raise DaemonLogError("unsafe_log") from error
        except OSError as error:
            raise DaemonLogError("log_open_failed") from error
        try:
            if created:
                os.fchmod(descriptor, 0o600)
            opened = os.fstat(descriptor)
            current = cls._validate_entry(directory_fd, name, maximum=maximum)
            if (
                current is None
                or not _is_private_regular(opened)
                or _identity(opened) != _identity(current)
            ):
                raise DaemonLogError("unsafe_log")
            return descriptor, _identity(opened)
        except BaseException:
            os.close(descriptor)
            raise

    def _validate_record_line(self, raw: bytes) -> None:
        if not raw or len(raw) + 1 > self.line_bytes:
            raise DaemonLogError("invalid_log")
        try:
            record = json.loads(raw.decode("ascii"))
        except (UnicodeError, ValueError) as error:
            raise DaemonLogError("invalid_log") from error
        if (
            not isinstance(record, dict)
            or not _REQUIRED_FIELDS.issubset(record)
            or not set(record).issubset(_ALLOWED_FIELDS)
            or type(record.get("schema_version")) is not int
            or record["schema_version"] != SCHEMA_VERSION
            or type(record.get("pid")) is not int
            or record["pid"] <= 0
            or not isinstance(record.get("ts"), str)
            or not isinstance(record.get("ts_epoch"), (int, float))
            or isinstance(record.get("ts_epoch"), bool)
            or not math.isfinite(float(record["ts_epoch"]))
        ):
            raise DaemonLogError("invalid_log")
        for name in ("component", "event", "level", "version"):
            if not isinstance(record.get(name), str):
                raise DaemonLogError("invalid_log")
        for name, value in record.items():
            if name in _REQUIRED_FIELDS:
                continue
            if name in ("http_enabled", "timed_out"):
                valid = isinstance(value, bool)
            elif name in ("http_port", "count"):
                valid = type(value) is int
            else:
                valid = isinstance(value, str)
            if not valid:
                raise DaemonLogError("invalid_log")

    def _repair_current(self, descriptor: int) -> None:
        """Validate complete JSONL and remove one bounded crash suffix."""

        try:
            size = int(os.fstat(descriptor).st_size)
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks = bytearray()
            while len(chunks) < size:
                piece = os.read(descriptor, min(64 * 1024, size - len(chunks)))
                if not piece:
                    raise DaemonLogError("invalid_log")
                chunks.extend(piece)
            if int(os.fstat(descriptor).st_size) != size:
                raise DaemonLogError("invalid_log")
        except DaemonLogError:
            raise
        except OSError as error:
            raise DaemonLogError("log_open_failed") from error

        payload = bytes(chunks)
        complete_end = payload.rfind(b"\n") + 1
        complete = payload[:complete_end]
        suffix = payload[complete_end:]
        for raw in complete.split(b"\n")[:-1]:
            self._validate_record_line(raw)
        if not suffix:
            return
        if len(suffix) >= self.line_bytes:
            raise DaemonLogError("invalid_log")
        try:
            os.ftruncate(descriptor, complete_end)
            os.fsync(descriptor)
        except OSError as error:
            raise DaemonLogError("log_repair_failed") from error

    def open(self) -> "DaemonLog":
        with self._state_lock:
            if self._opened and not self._closed:
                return self
            if self._closed:
                raise DaemonLogError("log_closed")
            directory_fd: Optional[int] = None
            lock_fd: Optional[int] = None
            log_fd: Optional[int] = None
            try:
                directory_fd = self._open_directory()
                self._validate_entry(directory_fd, LOCK_NAME)
                self._validate_entry(directory_fd, LOG_NAME, maximum=self.max_bytes)
                for index in range(1, MAX_BACKUPS + 1):
                    self._validate_entry(
                        directory_fd,
                        "{}.{}".format(LOG_NAME, index),
                        maximum=self.max_bytes,
                    )
                lock_fd, _lock_identity = self._open_private_file(
                    directory_fd,
                    LOCK_NAME,
                )
                try:
                    assert _fcntl is not None
                    _fcntl.flock(
                        lock_fd,
                        _fcntl.LOCK_EX | _fcntl.LOCK_NB,
                    )
                except (BlockingIOError, OSError) as error:
                    raise DaemonLogError("log_locked") from error
                removed_extra = False
                for index in range(self.backups + 1, MAX_BACKUPS + 1):
                    name = "{}.{}".format(LOG_NAME, index)
                    if self._entry_stat(directory_fd, name) is not None:
                        os.unlink(name, dir_fd=directory_fd)
                        removed_extra = True
                if removed_extra:
                    os.fsync(directory_fd)
                log_fd, log_identity = self._open_private_file(
                    directory_fd,
                    LOG_NAME,
                    maximum=self.max_bytes,
                )
                self._repair_current(log_fd)
                self._directory_fd = directory_fd
                self._lock_fd = lock_fd
                self._log_fd = log_fd
                self._log_identity = log_identity
                self._opened = True
                return self
            except BaseException as error:
                if log_fd is not None:
                    os.close(log_fd)
                if lock_fd is not None:
                    try:
                        getattr(
                            _fcntl,
                            "flock",
                            lambda *_unused: None,
                        )(
                            lock_fd,
                            getattr(_fcntl, "LOCK_UN", 0),
                        )
                    except OSError:
                        pass
                    os.close(lock_fd)
                if directory_fd is not None:
                    os.close(directory_fd)
                if isinstance(error, DaemonLogError):
                    raise
                if not isinstance(error, Exception):
                    raise
                raise DaemonLogError("log_open_failed") from error

    def _validate_current(self) -> os.stat_result:
        directory_fd = self._directory_fd
        descriptor = self._log_fd
        expected = self._log_identity
        if directory_fd is None or descriptor is None or expected is None:
            raise DaemonLogError("log_closed")
        current = self._validate_entry(
            directory_fd,
            LOG_NAME,
            maximum=self.max_bytes,
        )
        opened = os.fstat(descriptor)
        if (
            current is None
            or not _is_private_regular(opened)
            or _identity(current) != expected
            or _identity(opened) != expected
        ):
            raise DaemonLogError("unsafe_log")
        return opened

    def _rotate(self) -> None:
        directory_fd = self._directory_fd
        descriptor = self._log_fd
        if directory_fd is None or descriptor is None:
            raise DaemonLogError("log_closed")
        self._validate_current()
        os.fsync(descriptor)
        for index in range(1, self.backups + 1):
            self._validate_entry(
                directory_fd,
                "{}.{}".format(LOG_NAME, index),
                maximum=self.max_bytes,
            )
        if self.backups:
            for index in range(self.backups, 1, -1):
                source = "{}.{}".format(LOG_NAME, index - 1)
                destination = "{}.{}".format(LOG_NAME, index)
                if self._entry_stat(directory_fd, source) is None:
                    if self._entry_stat(directory_fd, destination) is not None:
                        os.unlink(destination, dir_fd=directory_fd)
                else:
                    os.replace(
                        source,
                        destination,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
            os.replace(
                LOG_NAME,
                "{}.1".format(LOG_NAME),
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        else:
            os.unlink(LOG_NAME, dir_fd=directory_fd)
        os.fsync(directory_fd)
        os.close(descriptor)
        self._log_fd = None
        self._log_identity = None
        replacement, replacement_identity = self._open_private_file(
            directory_fd,
            LOG_NAME,
            maximum=self.max_bytes,
        )
        self._log_fd = replacement
        self._log_identity = replacement_identity
        os.fsync(directory_fd)

    @staticmethod
    def _session_identifier(value: object) -> str:
        if not isinstance(value, str) or not value:
            return "unknown"
        encoded = value.encode("utf-8", "surrogatepass")
        return "sha256:{}".format(hashlib.sha256(encoded).hexdigest()[:16])

    def _record(self, event: object, fields: Mapping[str, Any]) -> Dict[str, Any]:
        now = time.time()
        iso = datetime.datetime.fromtimestamp(
            now,
            tz=datetime.timezone.utc,
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        record: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ts": iso,
            "ts_epoch": round(now, 3),
            "component": "daemon",
            "event": _safe_identifier(event, _STRING_LIMITS["event"]),
            "level": "info",
            "version": self.version,
            "pid": os.getpid(),
        }
        for name in _OPTIONAL_FIELDS:
            if name not in fields:
                continue
            value = fields[name]
            if name in _STRING_LIMITS:
                normalized = _safe_identifier(value, _STRING_LIMITS[name])
                if name == "level" and normalized not in _LEVELS:
                    normalized = "info"
                record[name] = normalized
            elif name == "session_id":
                record[name] = self._session_identifier(value)
            elif name in ("http_enabled", "timed_out"):
                if isinstance(value, bool):
                    record[name] = value
            elif name == "http_port":
                if isinstance(value, int) and not isinstance(value, bool):
                    record[name] = min(65535, max(0, value))
            elif (
                name == "count"
                and isinstance(value, int)
                and not isinstance(value, bool)
            ):
                record[name] = min(1_000_000_000, max(0, value))
        return record

    def _encode(self, event: object, fields: Mapping[str, Any]) -> bytes:
        payload = (
            json.dumps(
                self._record(event, fields),
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
        if len(payload) > min(self.line_bytes, self.max_bytes):
            raise DaemonLogError("log_line_too_large")
        return payload

    def _disable(self, code: str) -> None:
        if self._disabled:
            return
        self._disabled = True
        self._error_code = _safe_code(code, "log_write_failed")
        descriptor = self._log_fd
        self._log_fd = None
        self._log_identity = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def append(
        self,
        event: object,
        *,
        durable: bool = False,
        **fields: Any
    ) -> bool:
        """Append one allowlisted record; disable once on any write failure."""

        with self._state_lock:
            if not self._opened or self._closed or self._disabled:
                return False
            descriptor: Optional[int] = None
            before: Optional[int] = None
            write_started = False
            try:
                payload = self._encode(event, fields)
                details = self._validate_current()
                if details.st_size + len(payload) > self.max_bytes:
                    self._rotate()
                    details = self._validate_current()
                descriptor = self._log_fd
                if descriptor is None:
                    raise DaemonLogError("log_closed")
                before = int(details.st_size)
                write_started = True
                _write_all(descriptor, payload)
                after = os.fstat(descriptor)
                if int(after.st_size) != before + len(payload):
                    raise DaemonLogError("log_write_failed")
                if durable or fields.get("level") in ("error", "critical"):
                    os.fsync(descriptor)
                return True
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                if isinstance(error, DaemonLogError):
                    code = error.code
                else:
                    code = "log_write_failed"
                if write_started and descriptor is not None and before is not None:
                    try:
                        os.ftruncate(descriptor, before)
                        os.fsync(descriptor)
                    except OSError:
                        code = "log_repair_failed"
                self._disable(code)
                return False

    log = append

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            log_fd = self._log_fd
            lock_fd = self._lock_fd
            directory_fd = self._directory_fd
            self._log_fd = None
            self._lock_fd = None
            self._directory_fd = None
            self._log_identity = None
            if log_fd is not None:
                try:
                    os.close(log_fd)
                except OSError:
                    pass
            if lock_fd is not None:
                try:
                    if _fcntl is not None:
                        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    os.close(lock_fd)
                except OSError:
                    pass
            if directory_fd is not None:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass

    def __enter__(self) -> "DaemonLog":
        return self.open()

    def __exit__(self, *unused: Any) -> None:
        self.close()


__all__ = [
    "DEFAULT_BACKUPS",
    "DaemonLog",
    "DaemonLogError",
    "LOCK_NAME",
    "LOG_NAME",
    "MAX_BACKUPS",
    "MAX_CURRENT_BYTES",
    "MAX_LINE_BYTES",
    "SCHEMA_VERSION",
]
