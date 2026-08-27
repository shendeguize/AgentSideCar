"""Secure, bounded send audit storage and request-id reservations.

Idempotency is intentionally limited to records retained in ``audit.jsonl``
and its single rotation. Once both retained logs no longer contain a request
ID, that ID is no longer protected from reuse.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX Python
    fcntl = None  # type: ignore[assignment]

try:
    import pwd
except ImportError:  # pragma: no cover - unavailable on non-POSIX Python
    pwd = None  # type: ignore[assignment]


AUDIT_FILE_NAME = "audit.jsonl"
AUDIT_KEY_NAME = "audit.key"
AUDIT_LOCK_NAME = "audit.lock"
AUDIT_NEXT_FILE_NAME = "audit.jsonl.next"
AUDIT_ROTATED_FILE_NAME = "audit.jsonl.1"
AUDIT_ARCHIVE_DIR_NAME = "audit-archive"
MAX_AUDIT_ARCHIVES = 8
AUDIT_SCHEMA_VERSION = 1
NAMESPACE_ANCHOR_NAME = ".agent_sidecar-namespaces"
NAMESPACE_TRANSACTION_SUFFIX = ".audit-lock"
MAX_AUDIT_BYTES = 8 * 1024 * 1024
MAX_AUDIT_LINE_BYTES = 4096
MAX_AUDIT_RECORD_DEPTH = 3
MAX_ACTIVE_PENDING_RECORDS = 4096
MAX_ACTIVE_PENDING_BYTES = 4 * 1024 * 1024
MAX_REQUEST_ID_BYTES = 128
IDEMPOTENCY_RETENTION_NOTICE = (
    "request-id idempotency is limited to the two retained audit logs"
)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_AGENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_ERROR_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EPOCH_RE = re.compile(r"^e_[A-Za-z0-9_-]{32,64}$")
_ARCHIVE_NAME_RE = re.compile(r"^archive-[0-9TZ-]{16}-[0-9a-f]{8}$")
_ARCHIVE_FILE_NAMES = frozenset(
    (
        AUDIT_FILE_NAME,
        AUDIT_ROTATED_FILE_NAME,
        AUDIT_NEXT_FILE_NAME,
        AUDIT_KEY_NAME,
        AUDIT_LOCK_NAME,
        "namespace.marker",
    )
)
_TERMINAL_OUTCOMES = frozenset(
    ("completed", "failed", "timed_out", "overflow")
)
_DELIVERY_STATES = frozenset(("delivered", "unknown"))
_COMMON_KEYS = frozenset(
    (
        "schema_version",
        "timestamp",
        "request_id",
        "namespace_epoch",
        "agent",
        "target_hmac",
        "request_hmac",
        "executable_basename",
        "confirmation_mode",
        "outcome",
    )
)
_TERMINAL_KEYS = frozenset(("delivery", "error", "returncode"))


class AuditError(RuntimeError):
    """A stable, secret-free audit failure."""

    _MESSAGES = {
        "audit_corrupt": "send audit is corrupt",
        "audit_error": "send audit could not be updated",
        "audit_busy": "send audit is active",
        "invalid_request_id": "request ID must be conservative ASCII and at most 128 bytes",
        "request_conflict": "request ID was already used for a different target",
        "unsafe_audit": "send audit path is unsafe",
        "audit_archive_full": "send audit archive limit has been reached",
    }

    def __init__(self, code: str, detail: Optional[str] = None) -> None:
        if code not in self._MESSAGES:
            raise ValueError("invalid audit error code")
        if detail is not None and _ERROR_RE.fullmatch(detail) is None:
            raise ValueError("invalid audit error detail")
        self.code = code
        self.detail = detail
        super().__init__(self._MESSAGES[code])

    def to_dict(self) -> Dict[str, str]:
        payload = {"code": self.code}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True, repr=False)
class AuditIdentity:
    """In-memory request identity; raw values are never persisted."""

    agent: str
    session_id: str
    project: str
    executable_basename: str
    confirmation_mode: str
    message: bytes = field(repr=False)


@dataclass(frozen=True)
class AuditReceipt:
    """A safe receipt reconstructed from retained audit records."""

    request_id: str
    agent: str
    outcome: str
    delivery: str
    returncode: Optional[int]
    error: Optional[str]


def generate_request_id() -> str:
    """Return a cryptographically random, opaque request ID."""

    return "r_" + secrets.token_urlsafe(32)


def validate_request_id(value: object) -> str:
    """Validate an explicit or generated request ID without echoing it."""

    if not isinstance(value, str):
        raise AuditError("invalid_request_id")
    try:
        encoded = value.encode("ascii")
    except (UnicodeEncodeError, UnicodeError) as error:
        raise AuditError("invalid_request_id") from error
    if (
        not encoded
        or len(encoded) > MAX_REQUEST_ID_BYTES
        or _REQUEST_ID_RE.fullmatch(value) is None
    ):
        raise AuditError("invalid_request_id")
    return value


def _hash_value(digest: Any, value: object, depth: int = 0) -> None:
    if depth > 8:
        raise AuditError("audit_error")
    if value is None:
        digest.update(b"n;")
        return
    if isinstance(value, bool):
        digest.update(b"b1;" if value else b"b0;")
        return
    if isinstance(value, int):
        digest.update(b"i" + str(value).encode("ascii") + b";")
        return
    if isinstance(value, float):
        digest.update(b"f" + value.hex().encode("ascii") + b";")
        return
    if isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise AuditError("audit_error") from error
        digest.update(b"s" + str(len(raw)).encode("ascii") + b":")
        digest.update(raw)
        digest.update(b";")
        return
    if isinstance(value, bytes):
        digest.update(b"y" + str(len(value)).encode("ascii") + b":")
        digest.update(value)
        digest.update(b";")
        return
    if isinstance(value, (tuple, list)):
        digest.update(b"[")
        for item in value:
            _hash_value(digest, item, depth + 1)
        digest.update(b"];")
        return
    raise AuditError("audit_error")


def _hmac_identity(key: bytes, label: str, *values: object) -> str:
    digest = hmac.new(key, digestmod=hashlib.sha256)
    _hash_value(digest, label)
    for value in values:
        _hash_value(digest, value)
    return digest.hexdigest()


def make_audit_identity(
    *,
    agent: str,
    session_id: str,
    project: str,
    executable_basename: str,
    confirmation_mode: str,
    message: bytes = b"",
) -> AuditIdentity:
    """Build an in-memory immutable target and message identity."""

    if (
        not isinstance(agent, str)
        or _AGENT_RE.fullmatch(agent) is None
        or not isinstance(session_id, str)
        or not session_id
        or not isinstance(project, str)
        or not project
        or not isinstance(executable_basename, str)
        or _BASENAME_RE.fullmatch(executable_basename) is None
        or confirmation_mode != "allow_write"
        or not isinstance(message, bytes)
    ):
        raise AuditError("audit_error")
    return AuditIdentity(
        agent=agent,
        session_id=session_id,
        project=project,
        executable_basename=executable_basename,
        confirmation_mode=confirmation_mode,
        message=message,
    )


def _record_depth(value: object, depth: int = 0) -> int:
    if depth > MAX_AUDIT_RECORD_DEPTH:
        raise AuditError("audit_corrupt")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditError("audit_corrupt")
            _record_depth(item, depth + 1)
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in value:
            _record_depth(item, depth + 1)
    return depth


def _valid_returncode(value: object) -> bool:
    return value is None or (
        type(value) is int and -(2**31) <= value <= (2**31 - 1)
    )


def _validate_record(value: object) -> Mapping[str, Any]:
    try:
        if not isinstance(value, Mapping):
            raise AuditError("audit_corrupt")
        _record_depth(value)
        record = dict(value)
        outcome = record.get("outcome")
        expected = (
            _COMMON_KEYS
            if outcome == "pending"
            else _COMMON_KEYS | _TERMINAL_KEYS
        )
        if frozenset(record) != expected:
            raise AuditError("audit_corrupt")
        version = record.get("schema_version")
        timestamp = record.get("timestamp")
        request_id = record.get("request_id")
        namespace_epoch = record.get("namespace_epoch")
        agent = record.get("agent")
        target_hmac = record.get("target_hmac")
        request_hmac = record.get("request_hmac")
        executable = record.get("executable_basename")
        error = record.get("error")
        returncode = record.get("returncode")
        if (
            type(version) is not int
            or version != AUDIT_SCHEMA_VERSION
            or type(timestamp) not in (int, float)
            or not math.isfinite(timestamp)
            or timestamp < 0.0
            or type(request_id) is not str
            or type(namespace_epoch) is not str
            or _EPOCH_RE.fullmatch(namespace_epoch) is None
            or type(agent) is not str
            or _AGENT_RE.fullmatch(agent) is None
            or type(target_hmac) is not str
            or _HASH_RE.fullmatch(target_hmac) is None
            or type(request_hmac) is not str
            or _HASH_RE.fullmatch(request_hmac) is None
            or type(executable) is not str
            or _BASENAME_RE.fullmatch(executable) is None
            or record.get("confirmation_mode") != "allow_write"
        ):
            raise AuditError("audit_corrupt")
        try:
            validate_request_id(request_id)
        except AuditError as audit_error:
            raise AuditError("audit_corrupt") from audit_error
        if outcome == "pending":
            return record
        if (
            type(outcome) is not str
            or outcome not in _TERMINAL_OUTCOMES
            or type(record.get("delivery")) is not str
            or record["delivery"] not in _DELIVERY_STATES
            or not _valid_returncode(returncode)
            or (
                error is not None
                and (
                    type(error) is not str
                    or _ERROR_RE.fullmatch(error) is None
                )
            )
        ):
            raise AuditError("audit_corrupt")
        if outcome == "completed":
            if (
                not (
                    record["delivery"] == "delivered"
                    and returncode == 0
                    and error is None
                )
                and not (
                    record["delivery"] == "unknown"
                    and returncode == 0
                    and error == "cleanup_incomplete"
                )
            ):
                raise AuditError("audit_corrupt")
        elif record["delivery"] != "unknown":
            raise AuditError("audit_corrupt")
        return record
    except AuditError:
        raise
    except (
        AttributeError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ) as error:
        raise AuditError("audit_corrupt") from error


def _record_identity(record: Mapping[str, Any]) -> Tuple[object, ...]:
    return (
        record["agent"],
        record["target_hmac"],
        record["request_hmac"],
        record["confirmation_mode"],
    )


def _identity_tuple(identity: AuditIdentity, key: bytes) -> Tuple[object, ...]:
    return (
        identity.agent,
        _hmac_identity(
            key,
            "target",
            identity.agent,
            identity.session_id,
            identity.project,
        ),
        _hmac_identity(
            key,
            "request",
            identity.agent,
            identity.session_id,
            identity.project,
            identity.message,
        ),
        identity.confirmation_mode,
    )


def _validate_histories(
    records: Sequence[Mapping[str, Any]],
) -> Dict[str, List[Mapping[str, Any]]]:
    histories: Dict[str, List[Mapping[str, Any]]] = {}
    for record in records:
        histories.setdefault(record["request_id"], []).append(record)
    compacted: Dict[str, List[Mapping[str, Any]]] = {}
    for request_id, history in histories.items():
        identities = {_record_identity(record) for record in history}
        if len(identities) != 1:
            raise AuditError("audit_corrupt")
        terminal_indexes = [
            index
            for index, record in enumerate(history)
            if record["outcome"] != "pending"
        ]
        if (
            len(terminal_indexes) > 1
            or (terminal_indexes and terminal_indexes[0] != len(history) - 1)
        ):
            raise AuditError("audit_corrupt")
        pending_records = [
            record for record in history if record["outcome"] == "pending"
        ]
        if (
            not pending_records
            or any(
                pending != pending_records[0]
                for pending in pending_records[1:]
            )
        ):
            raise AuditError("audit_corrupt")
        if terminal_indexes:
            if len(history) < 2:
                raise AuditError("audit_corrupt")
            compacted[request_id] = [history[-2], history[-1]]
        else:
            compacted[request_id] = [history[-1]]
    return compacted


def _file_signature(details: os.stat_result) -> Tuple[int, ...]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_uid),
        int(details.st_size),
        int(getattr(details, "st_mtime_ns", int(details.st_mtime * 1e9))),
        int(getattr(details, "st_ctime_ns", int(details.st_ctime * 1e9))),
    )


def _translate_filesystem_error(error: BaseException) -> AuditError:
    code = getattr(error, "code", None)
    if code == "unsafe_lock" or (
        isinstance(error, OSError)
        and error.errno in (errno.ELOOP, errno.ENOTDIR)
    ):
        return AuditError("unsafe_audit")
    return AuditError("audit_error")


def _secure_helpers():
    # The send lock and audit store intentionally share one anchored runtime
    # traversal implementation. The import is lazy to keep this module usable
    # by inject.py without an import cycle.
    from sidecar import inject

    return inject


def _open_named_file(
    runtime_fd: int,
    name: str,
    *,
    create: bool,
) -> Tuple[Optional[int], bool]:
    helpers = _secure_helpers()
    flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    created = False
    try:
        if create:
            try:
                descriptor = os.open(
                    name,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=runtime_fd,
                )
                created = True
            except FileExistsError:
                details = helpers._entry_stat(runtime_fd, name)
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_uid != os.geteuid()
                    or stat.S_IMODE(details.st_mode) != 0o600
                ):
                    raise AuditError("unsafe_audit")
                descriptor = os.open(name, flags, dir_fd=runtime_fd)
        else:
            try:
                details = os.stat(
                    name,
                    dir_fd=runtime_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_uid != os.geteuid()
                    or stat.S_IMODE(details.st_mode) != 0o600
                ):
                    raise AuditError("unsafe_audit")
                descriptor = os.open(name, flags, dir_fd=runtime_fd)
            except FileNotFoundError:
                return None, False
        if created:
            os.fchmod(descriptor, 0o600)
        helpers._validate_lock_file(runtime_fd, name, descriptor)
        return descriptor, created
    except BaseException as error:
        descriptor_value = locals().get("descriptor")
        if isinstance(descriptor_value, int):
            try:
                os.close(descriptor_value)
            except OSError:
                pass
        if isinstance(error, AuditError):
            raise
        if not isinstance(error, Exception):
            raise
        raise _translate_filesystem_error(error) from error


def _validate_named_file(runtime_fd: int, name: str, descriptor: int) -> None:
    try:
        _secure_helpers()._validate_lock_file(runtime_fd, name, descriptor)
    except BaseException as error:
        if isinstance(error, AuditError):
            raise
        if not isinstance(error, Exception):
            raise
        raise _translate_filesystem_error(error) from error


def _read_file(runtime_fd: int, name: str, descriptor: int) -> bytes:
    try:
        _validate_named_file(runtime_fd, name, descriptor)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size < 0
            or before.st_size > MAX_AUDIT_BYTES
        ):
            raise AuditError("audit_corrupt")
        os.lseek(descriptor, 0, os.SEEK_SET)
        remaining = int(before.st_size)
        chunks: List[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise AuditError("audit_corrupt")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        _validate_named_file(runtime_fd, name, descriptor)
        if _file_signature(before) != _file_signature(after):
            raise AuditError("audit_corrupt")
        return b"".join(chunks)
    except AuditError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise _translate_filesystem_error(error) from error


def _parse_file(payload: bytes) -> List[Mapping[str, Any]]:
    if not payload:
        return []
    if not payload.endswith(b"\n"):
        raise AuditError("audit_corrupt")
    records: List[Mapping[str, Any]] = []
    for line in payload.splitlines():
        if not line or len(line) > MAX_AUDIT_LINE_BYTES:
            raise AuditError("audit_corrupt")
        try:
            decoded = line.decode("utf-8", "strict")

            def unique_object(pairs):
                value: Dict[str, Any] = {}
                for key, item in pairs:
                    if key in value:
                        raise AuditError("audit_corrupt")
                    value[key] = item
                return value

            value = json.loads(decoded, object_pairs_hook=unique_object)
        except (
            AuditError,
            UnicodeDecodeError,
            UnicodeError,
            json.JSONDecodeError,
            RecursionError,
        ) as error:
            raise AuditError("audit_corrupt") from error
        records.append(_validate_record(value))
    return records


def _encoded_record(record: Mapping[str, Any]) -> bytes:
    try:
        encoded = (
            json.dumps(
                record,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise AuditError("audit_error") from error
    if len(encoded) > MAX_AUDIT_LINE_BYTES:
        raise AuditError("audit_error")
    return encoded


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short audit write")
        offset += written


def _private_regular(details: os.stat_result) -> bool:
    return (
        stat.S_ISREG(details.st_mode)
        and details.st_uid == os.geteuid()
        and stat.S_IMODE(details.st_mode) == 0o600
    )


def _private_directory(details: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(details.st_mode)
        and details.st_uid == os.geteuid()
        and stat.S_IMODE(details.st_mode) == 0o700
    )


def _archive_entry_name() -> str:
    return "archive-{}-{}".format(
        time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        secrets.token_hex(4),
    )


def _receipt(record: Mapping[str, Any]) -> AuditReceipt:
    if record["outcome"] == "pending":
        return AuditReceipt(
            request_id=record["request_id"],
            agent=record["agent"],
            outcome="request_pending",
            delivery="unknown",
            returncode=None,
            error="request_pending",
        )
    return AuditReceipt(
        request_id=record["request_id"],
        agent=record["agent"],
        outcome=record["outcome"],
        delivery=record["delivery"],
        returncode=record["returncode"],
        error=record["error"],
    )


_MARKER_KEYS = frozenset(
    (
        "schema_version",
        "namespace_epoch",
        "key_fingerprint",
        "runtime_dev",
        "runtime_ino",
        "key_dev",
        "key_ino",
    )
)


def _marker_name(canonical_runtime: str) -> str:
    return hashlib.sha256(canonical_runtime.encode("utf-8")).hexdigest()


def _transaction_name(marker_name: str) -> str:
    return marker_name + NAMESPACE_TRANSACTION_SUFFIX


def _read_descriptor(descriptor: int, maximum: int) -> Tuple[bytes, os.stat_result]:
    before = os.fstat(descriptor)
    if before.st_size < 0 or before.st_size > maximum:
        raise AuditError("audit_corrupt")
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = os.read(descriptor, maximum + 1)
    after = os.fstat(descriptor)
    if (
        len(payload) != before.st_size
        or _file_signature(before) != _file_signature(after)
    ):
        raise AuditError("audit_corrupt")
    return payload, after


def _parse_marker(payload: bytes) -> Mapping[str, Any]:
    if not payload or not payload.endswith(b"\n"):
        raise AuditError("audit_corrupt")
    try:
        value = json.loads(payload.decode("ascii"))
        if not isinstance(value, dict) or frozenset(value) != _MARKER_KEYS:
            raise AuditError("audit_corrupt")
        if (
            type(value.get("schema_version")) is not int
            or value["schema_version"] != AUDIT_SCHEMA_VERSION
            or type(value.get("namespace_epoch")) is not str
            or _EPOCH_RE.fullmatch(value["namespace_epoch"]) is None
            or type(value.get("key_fingerprint")) is not str
            or _HASH_RE.fullmatch(value["key_fingerprint"]) is None
            or any(
                type(value.get(name)) is not int or value[name] < 0
                for name in (
                    "runtime_dev",
                    "runtime_ino",
                    "key_dev",
                    "key_ino",
                )
            )
        ):
            raise AuditError("audit_corrupt")
        return value
    except AuditError:
        raise
    except (
        KeyError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise AuditError("audit_corrupt") from error


class AuditNamespace:
    """A retained anchored runtime namespace used for one whole send."""

    def __init__(
        self,
        store: "SendAuditStore",
        directory_fds: List[int],
        links: List[Tuple[int, str, int, bool]],
        runtime_fd: int,
        parent_fd: int,
        runtime_name: str,
        marker_fd: int,
        marker_name: str,
        transaction_fd: int,
        transaction_name: str,
        marker: Mapping[str, Any],
        marker_signature: Tuple[int, ...],
        key_fd: int,
        key: bytes,
        key_signature: Tuple[int, ...],
        marker_exclusive: bool,
    ) -> None:
        self.store = store
        self.directory_fds = directory_fds
        self.links = links
        self.runtime_fd = runtime_fd
        self.parent_fd = parent_fd
        self.runtime_name = runtime_name
        self.marker_fd: Optional[int] = marker_fd
        self.marker_name = marker_name
        self.transaction_fd: Optional[int] = transaction_fd
        self.transaction_name = transaction_name
        self.marker = dict(marker)
        self.marker_signature = marker_signature
        self.namespace_epoch = marker["namespace_epoch"]
        self.key_fd: Optional[int] = key_fd
        self.key = key
        self.key_signature = key_signature
        self.marker_exclusive = marker_exclusive
        self.closed = False

    def validate(self) -> None:
        if self.closed:
            raise AuditError("unsafe_audit")
        helpers = _secure_helpers()
        try:
            helpers._validate_directory(
                os.fstat(self.directory_fds[0]),
                private=False,
            )
            for _parent_fd, _name, child_fd, private in self.links:
                helpers._validate_directory(
                    os.fstat(child_fd),
                    private=private,
                )
            assert self.marker_fd is not None
            _validate_named_file(
                self.parent_fd,
                self.marker_name,
                self.marker_fd,
            )
            assert self.transaction_fd is not None
            _validate_named_file(
                self.parent_fd,
                self.transaction_name,
                self.transaction_fd,
            )
            marker_payload, marker_details = _read_descriptor(
                self.marker_fd,
                2048,
            )
            if (
                _file_signature(marker_details) != self.marker_signature
                or _parse_marker(marker_payload) != self.marker
            ):
                raise AuditError("audit_corrupt")
            assert self.key_fd is not None
            _validate_named_file(
                self.runtime_fd,
                AUDIT_KEY_NAME,
                self.key_fd,
            )
            key_payload, key_details = _read_descriptor(self.key_fd, 32)
            if (
                key_payload != self.key
                or _file_signature(key_details) != self.key_signature
                or hashlib.sha256(key_payload).hexdigest()
                != self.marker["key_fingerprint"]
            ):
                raise AuditError("audit_corrupt")
            runtime_details = os.fstat(self.runtime_fd)
            if (
                (key_details.st_dev, key_details.st_ino)
                != (self.marker["key_dev"], self.marker["key_ino"])
                or (runtime_details.st_dev, runtime_details.st_ino)
                != (self.marker["runtime_dev"], self.marker["runtime_ino"])
            ):
                raise AuditError("audit_corrupt", detail="namespace_moved")
        except AuditError:
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise _translate_filesystem_error(error) from error

    def reserve(
        self,
        request_id: object,
        identity: AuditIdentity,
    ) -> Optional[AuditReceipt]:
        return self.store._reserve(self, request_id, identity)

    def downgrade_marker(self) -> None:
        if self.closed or self.marker_fd is None:
            raise AuditError("unsafe_audit")
        if self.marker_exclusive:
            try:
                fcntl.flock(self.marker_fd, fcntl.LOCK_SH)
            except OSError as error:
                raise _translate_filesystem_error(error) from error
            self.marker_exclusive = False

    def append_terminal(
        self,
        request_id: object,
        identity: AuditIdentity,
        *,
        outcome: str,
        delivery: str,
        error: Optional[str],
        returncode: Optional[int],
    ) -> AuditReceipt:
        return self.store._append_terminal(
            self,
            request_id,
            identity,
            outcome=outcome,
            delivery=delivery,
            error=error,
            returncode=returncode,
        )

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.key_fd is not None:
            os.close(self.key_fd)
            self.key_fd = None
        if self.marker_fd is not None:
            try:
                fcntl.flock(self.marker_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self.marker_fd)
            self.marker_fd = None
        if self.transaction_fd is not None:
            os.close(self.transaction_fd)
            self.transaction_fd = None
        for descriptor in reversed(self.directory_fds):
            os.close(descriptor)
        self.directory_fds = []


class SendAuditStore:
    """A two-file, fail-closed JSONL audit and idempotency store."""

    def __init__(self, runtime_dir: Optional[os.PathLike] = None) -> None:
        self.runtime_dir = runtime_dir

    @staticmethod
    def _open_anchor(
        helpers: Any,
        runtime_dir: Optional[os.PathLike],
    ) -> Tuple[
        List[int],
        List[Tuple[int, str, int, bool]],
        int,
        str,
    ]:
        if pwd is None:
            raise AuditError("unsafe_audit")
        try:
            account = pwd.getpwuid(os.geteuid())
            configured_home = Path(account.pw_dir)
            if (
                not configured_home.is_absolute()
                or any(
                    part in ("", ".", "..")
                    for part in configured_home.parts[1:]
                )
            ):
                raise AuditError("unsafe_audit")
            home = configured_home.resolve(strict=True)
            if home != configured_home:
                raise AuditError("unsafe_audit")
            canonical_runtime = os.path.normpath(
                os.fspath(helpers._runtime_root(runtime_dir))
            )
        except AuditError:
            raise
        except (
            AttributeError,
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as error:
            raise AuditError("unsafe_audit") from error
        anchor_path = home / NAMESPACE_ANCHOR_NAME
        directory_fds, links, home_fd, anchor_name, _ignored = (
            helpers._open_runtime_parent(anchor_path)
        )
        try:
            home_details = os.fstat(home_fd)
            if (
                not stat.S_ISDIR(home_details.st_mode)
                or home_details.st_uid != os.geteuid()
                or stat.S_IMODE(home_details.st_mode) & 0o022
            ):
                raise AuditError("unsafe_audit")
            anchor_fd, anchor_link = helpers._open_directory_at(
                home_fd,
                anchor_name,
                create=True,
                private=True,
            )
            directory_fds.append(anchor_fd)
            links.append(anchor_link)
            return directory_fds, links, anchor_fd, canonical_runtime
        except BaseException:
            for descriptor in reversed(directory_fds):
                os.close(descriptor)
            raise

    @contextmanager
    def _open_namespace(
        self,
        *,
        hold_exclusive: bool,
    ) -> Iterator[AuditNamespace]:
        if fcntl is None or os.name != "posix":
            raise AuditError("unsafe_audit")
        helpers = _secure_helpers()
        directory_fds: List[int] = []
        marker_fd: Optional[int] = None
        transaction_fd: Optional[int] = None
        key_fd: Optional[int] = None
        marker_locked = False
        marker_exclusive = False
        yielded = False
        try:
            (
                directory_fds,
                links,
                parent_fd,
                canonical_runtime,
            ) = self._open_anchor(helpers, self.runtime_dir)
            marker_name = _marker_name(canonical_runtime)
            transaction_name = _transaction_name(marker_name)
            marker_fd, marker_created = _open_named_file(
                parent_fd,
                marker_name,
                create=True,
            )
            assert marker_fd is not None
            transaction_fd, transaction_created = _open_named_file(
                parent_fd,
                transaction_name,
                create=True,
            )
            assert transaction_fd is not None
            if marker_created or transaction_created:
                os.fsync(parent_fd)
            _validate_named_file(parent_fd, marker_name, marker_fd)
            _validate_named_file(
                parent_fd,
                transaction_name,
                transaction_fd,
            )
            try:
                fcntl.flock(
                    marker_fd,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
                marker_exclusive = True
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                fcntl.flock(marker_fd, fcntl.LOCK_SH)
            marker_locked = True
            marker_payload, _marker_details = _read_descriptor(
                marker_fd,
                2048,
            )
            try:
                marker = _parse_marker(marker_payload)
            except AuditError:
                if not marker_exclusive:
                    fcntl.flock(marker_fd, fcntl.LOCK_UN)
                    marker_locked = False
                    fcntl.flock(marker_fd, fcntl.LOCK_EX)
                    marker_locked = True
                    marker_exclusive = True
                    marker_payload, _marker_details = _read_descriptor(
                        marker_fd,
                        2048,
                    )
                    try:
                        marker = _parse_marker(marker_payload)
                    except AuditError:
                        marker = None
                else:
                    marker = None
            (
                runtime_directory_fds,
                runtime_links,
                runtime_parent_fd,
                runtime_name,
                _ignored,
            ) = helpers._open_runtime_parent(self.runtime_dir)
            directory_fds.extend(runtime_directory_fds)
            links.extend(runtime_links)
            runtime_fd, runtime_link = helpers._open_directory_at(
                runtime_parent_fd,
                runtime_name,
                create=marker is None,
                private=True,
            )
            directory_fds.append(runtime_fd)
            links.append(runtime_link)
            if marker is None:
                if (
                    self._has_nonempty_audit(runtime_fd)
                    or self._existing_sensitive_file(
                        runtime_fd,
                        AUDIT_NEXT_FILE_NAME,
                    )
                    is not None
                ):
                    raise AuditError("audit_corrupt")
                initial_audit_fd, audit_created = _open_named_file(
                    runtime_fd,
                    AUDIT_FILE_NAME,
                    create=True,
                )
                assert initial_audit_fd is not None
                try:
                    if os.fstat(initial_audit_fd).st_size != 0:
                        raise AuditError("audit_corrupt")
                    if audit_created:
                        os.fsync(runtime_fd)
                finally:
                    os.close(initial_audit_fd)
            elif (
                self._existing_sensitive_file(runtime_fd, AUDIT_FILE_NAME)
                is None
                and self._existing_sensitive_file(
                    runtime_fd,
                    AUDIT_NEXT_FILE_NAME,
                )
                is None
            ):
                raise AuditError("audit_corrupt")
            key_fd, key, key_signature = self._open_key(
                runtime_fd,
                allow_create=marker is None,
            )
            runtime_details = os.fstat(runtime_fd)
            key_details = os.fstat(key_fd)
            if marker is None:
                marker = {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "namespace_epoch": "e_" + secrets.token_urlsafe(24),
                    "key_fingerprint": hashlib.sha256(key).hexdigest(),
                    "runtime_dev": int(runtime_details.st_dev),
                    "runtime_ino": int(runtime_details.st_ino),
                    "key_dev": int(key_details.st_dev),
                    "key_ino": int(key_details.st_ino),
                }
                marker_signature = self._write_marker(
                    parent_fd,
                    marker_fd,
                    marker,
                )
            else:
                if hashlib.sha256(key).hexdigest() != marker["key_fingerprint"]:
                    raise AuditError("audit_corrupt")
                if (
                    (runtime_details.st_dev, runtime_details.st_ino)
                    != (marker["runtime_dev"], marker["runtime_ino"])
                    or (key_details.st_dev, key_details.st_ino)
                    != (marker["key_dev"], marker["key_ino"])
                ):
                    raise AuditError("audit_corrupt", detail="namespace_moved")
                marker_signature = _file_signature(os.fstat(marker_fd))
            namespace = AuditNamespace(
                self,
                directory_fds,
                links,
                runtime_fd,
                parent_fd,
                runtime_name,
                marker_fd,
                marker_name,
                transaction_fd,
                transaction_name,
                marker,
                marker_signature,
                key_fd,
                key,
                key_signature,
                marker_exclusive,
            )
            key_fd = None
            marker_fd = None
            transaction_fd = None
            assert namespace.marker_fd is not None
            marker_locked = False
            try:
                namespace.validate()
                if not hold_exclusive:
                    namespace.downgrade_marker()
                yielded = True
                yield namespace
            finally:
                namespace.close()
                directory_fds = []
        except AuditError:
            raise
        except BaseException as error:
            if yielded:
                raise
            if not isinstance(error, Exception):
                raise
            raise _translate_filesystem_error(error) from error
        finally:
            if marker_fd is not None:
                if marker_locked:
                    try:
                        fcntl.flock(marker_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(marker_fd)
            if transaction_fd is not None:
                os.close(transaction_fd)
            if key_fd is not None:
                os.close(key_fd)
            for descriptor in reversed(directory_fds):
                os.close(descriptor)

    @contextmanager
    def open_namespace(self) -> Iterator[AuditNamespace]:
        with self._open_namespace(hold_exclusive=False) as namespace:
            yield namespace

    @contextmanager
    def open_reserved(
        self,
        request_id: object,
        identity: AuditIdentity,
    ) -> Iterator[Tuple[AuditNamespace, Optional[AuditReceipt]]]:
        with self._open_namespace(hold_exclusive=True) as namespace:
            try:
                replay = namespace.reserve(request_id, identity)
            finally:
                namespace.downgrade_marker()
            yield namespace, replay

    @contextmanager
    def _locked(self, namespace: AuditNamespace) -> Iterator[None]:
        transaction_fd = namespace.transaction_fd
        if transaction_fd is None:
            raise AuditError("unsafe_audit")
        locked = False
        try:
            namespace.validate()
            fcntl.flock(transaction_fd, fcntl.LOCK_EX)
            locked = True
            namespace.validate()
            yield
            namespace.validate()
        except AuditError:
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise _translate_filesystem_error(error) from error
        finally:
            if locked:
                try:
                    fcntl.flock(transaction_fd, fcntl.LOCK_UN)
                except OSError:
                    pass

    @staticmethod
    def _open_existing_runtime(
        helpers: Any,
        runtime_dir: Optional[os.PathLike],
    ) -> Tuple[
        List[int],
        List[Tuple[int, str, int, bool]],
        Optional[int],
    ]:
        root = helpers._runtime_root(runtime_dir)
        components = root.parts[1:]
        if not components:
            raise AuditError("unsafe_audit")
        descriptors: List[int] = []
        links: List[Tuple[int, str, int, bool]] = []
        try:
            root_fd = os.open("/", helpers._directory_flags())
            descriptors.append(root_fd)
            helpers._validate_directory(os.fstat(root_fd), private=False)
            parent_fd = root_fd
            for index, component in enumerate(components):
                try:
                    child_fd = os.open(
                        component,
                        helpers._directory_flags(),
                        dir_fd=parent_fd,
                    )
                except FileNotFoundError:
                    for descriptor in reversed(descriptors):
                        os.close(descriptor)
                    return [], [], None
                private = index == len(components) - 1
                link = (parent_fd, component, child_fd, private)
                helpers._verify_directory_link(link)
                descriptors.append(child_fd)
                links.append(link)
                parent_fd = child_fd
            return descriptors, links, parent_fd
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    @staticmethod
    def _archive_files(
        source_fd: int,
        destination_fd: int,
        names: Sequence[str],
    ) -> None:
        for name in names:
            try:
                before = os.stat(
                    name,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if not _private_regular(before):
                raise AuditError("unsafe_audit")
            try:
                os.link(
                    name,
                    name,
                    src_dir_fd=source_fd,
                    dst_dir_fd=destination_fd,
                    follow_symlinks=False,
                )
                linked = os.stat(
                    name,
                    dir_fd=destination_fd,
                    follow_symlinks=False,
                )
                current = os.stat(
                    name,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
                if (
                    (linked.st_dev, linked.st_ino)
                    != (before.st_dev, before.st_ino)
                    or (current.st_dev, current.st_ino)
                    != (before.st_dev, before.st_ino)
                ):
                    raise AuditError("unsafe_audit")
                os.unlink(name, dir_fd=source_fd)
            except FileExistsError as error:
                raise AuditError("audit_error") from error
            except AuditError:
                try:
                    os.unlink(name, dir_fd=destination_fd)
                except OSError:
                    pass
                raise
            except OSError as error:
                try:
                    os.unlink(name, dir_fd=destination_fd)
                except OSError:
                    pass
                raise _translate_filesystem_error(error) from error

    @staticmethod
    def _purge_directory(directory_fd: int) -> None:
        try:
            names = os.listdir(directory_fd)
        except OSError as error:
            raise _translate_filesystem_error(error) from error
        for name in names:
            try:
                details = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise _translate_filesystem_error(error) from error
            if _private_regular(details):
                os.unlink(name, dir_fd=directory_fd)
            elif _private_directory(details):
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    SendAuditStore._purge_directory(child_fd)
                finally:
                    os.close(child_fd)
                os.rmdir(name, dir_fd=directory_fd)
            else:
                raise AuditError("unsafe_audit")

    @staticmethod
    def _validate_archive_entry(directory_fd: int) -> None:
        try:
            names = os.listdir(directory_fd)
        except OSError as error:
            raise _translate_filesystem_error(error) from error
        if len(names) > len(_ARCHIVE_FILE_NAMES):
            raise AuditError("unsafe_audit")
        for name in names:
            if name not in _ARCHIVE_FILE_NAMES:
                raise AuditError("unsafe_audit")
            try:
                details = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise _translate_filesystem_error(error) from error
            if not _private_regular(details):
                raise AuditError("unsafe_audit")

    def _open_archive_directory(
        self,
        helpers: Any,
        runtime_fd: int,
        *,
        create: bool,
    ) -> Tuple[int, bool]:
        try:
            archive_fd = os.open(
                AUDIT_ARCHIVE_DIR_NAME,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | os.O_NOFOLLOW,
                dir_fd=runtime_fd,
            )
            details = os.fstat(archive_fd)
            if not _private_directory(details):
                os.close(archive_fd)
                raise AuditError("unsafe_audit")
            return archive_fd, False
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(
                    AUDIT_ARCHIVE_DIR_NAME,
                    0o700,
                    dir_fd=runtime_fd,
                )
            except FileExistsError:
                pass
            archive_fd, _link = helpers._open_directory_at(
                runtime_fd,
                AUDIT_ARCHIVE_DIR_NAME,
                create=False,
                private=True,
            )
            return archive_fd, True
        except OSError as error:
            raise _translate_filesystem_error(error) from error

    def reset(self, *, purge: bool = False) -> Optional[str]:
        """Archive or purge this runtime's send-audit namespace."""

        if fcntl is None or os.name != "posix":
            raise AuditError("unsafe_audit")
        helpers = _secure_helpers()
        directory_fds: List[int] = []
        marker_fd: Optional[int] = None
        transaction_fd: Optional[int] = None
        archive_fd: Optional[int] = None
        archive_item_fd: Optional[int] = None
        marker_locked = False
        transaction_locked = False
        archive_name: Optional[str] = None
        try:
            (
                directory_fds,
                links,
                anchor_fd,
                canonical_runtime,
            ) = self._open_anchor(helpers, self.runtime_dir)
            marker_name = _marker_name(canonical_runtime)
            transaction_name = _transaction_name(marker_name)
            marker_fd, marker_created = _open_named_file(
                anchor_fd,
                marker_name,
                create=True,
            )
            assert marker_fd is not None
            if marker_created:
                os.fsync(anchor_fd)
            try:
                fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise AuditError("audit_busy") from error
                raise
            marker_locked = True
            transaction_fd, transaction_created = _open_named_file(
                anchor_fd,
                transaction_name,
                create=True,
            )
            assert transaction_fd is not None
            if transaction_created:
                os.fsync(anchor_fd)
            try:
                fcntl.flock(transaction_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise AuditError("audit_busy") from error
                raise
            transaction_locked = True
            runtime_fds, runtime_links, runtime_fd = (
                self._open_existing_runtime(helpers, self.runtime_dir)
            )
            directory_fds.extend(runtime_fds)
            links.extend(runtime_links)
            for link in links:
                helpers._verify_directory_link(link)
            _validate_named_file(anchor_fd, marker_name, marker_fd)
            existing: List[str] = []
            if runtime_fd is not None:
                for name in (
                    AUDIT_FILE_NAME,
                    AUDIT_ROTATED_FILE_NAME,
                    AUDIT_NEXT_FILE_NAME,
                    AUDIT_KEY_NAME,
                    AUDIT_LOCK_NAME,
                ):
                    try:
                        details = os.stat(
                            name,
                            dir_fd=runtime_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        continue
                    if not _private_regular(details):
                        raise AuditError("unsafe_audit")
                    existing.append(name)
            needs_archive = not purge and (bool(existing) or not marker_created)
            try:
                if runtime_fd is not None:
                    archive_fd, _created = self._open_archive_directory(
                        helpers,
                        runtime_fd,
                        create=needs_archive,
                    )
            except FileNotFoundError:
                archive_fd = None
            if archive_fd is not None:
                archive_names = os.listdir(archive_fd)
                for name in archive_names:
                    if _ARCHIVE_NAME_RE.fullmatch(name) is None:
                        raise AuditError("unsafe_audit")
                    details = os.stat(
                        name,
                        dir_fd=archive_fd,
                        follow_symlinks=False,
                    )
                    if not _private_directory(details):
                        raise AuditError("unsafe_audit")
                    archive_child_fd = os.open(
                        name,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | os.O_NOFOLLOW,
                        dir_fd=archive_fd,
                    )
                    try:
                        self._validate_archive_entry(archive_child_fd)
                    finally:
                        os.close(archive_child_fd)
                if purge:
                    self._purge_directory(archive_fd)
                elif len(archive_names) >= MAX_AUDIT_ARCHIVES and existing:
                    raise AuditError("audit_archive_full")
            if not purge and needs_archive and runtime_fd is not None:
                archive_name = _archive_entry_name()
                os.mkdir(archive_name, 0o700, dir_fd=archive_fd)
                archive_item_fd, _link = helpers._open_directory_at(
                    archive_fd,
                    archive_name,
                    create=False,
                    private=True,
                )
                if existing:
                    self._archive_files(
                        runtime_fd,
                        archive_item_fd,
                        existing,
                    )
                if not marker_created:
                    os.link(
                        marker_name,
                        "namespace.marker",
                        src_dir_fd=anchor_fd,
                        dst_dir_fd=archive_item_fd,
                        follow_symlinks=False,
                    )
                os.fsync(archive_item_fd)
                os.fsync(archive_fd)
            elif purge and runtime_fd is not None:
                for name in existing:
                    os.unlink(name, dir_fd=runtime_fd)
                if existing:
                    os.fsync(runtime_fd)
            os.unlink(marker_name, dir_fd=anchor_fd)
            os.fsync(anchor_fd)
            if existing and runtime_fd is not None:
                os.fsync(runtime_fd)
            return (
                os.path.join(canonical_runtime, AUDIT_ARCHIVE_DIR_NAME, archive_name)
                if archive_name is not None
                else None
            )
        except AuditError:
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise _translate_filesystem_error(error) from error
        finally:
            if archive_item_fd is not None:
                os.close(archive_item_fd)
            if archive_fd is not None:
                os.close(archive_fd)
            if transaction_fd is not None:
                if transaction_locked:
                    try:
                        fcntl.flock(transaction_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(transaction_fd)
            if marker_fd is not None:
                if marker_locked:
                    try:
                        fcntl.flock(marker_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(marker_fd)
            for descriptor in reversed(directory_fds):
                os.close(descriptor)

    def rebind(self) -> None:
        """Rebind a moved runtime after strict namespace verification."""

        if fcntl is None or os.name != "posix":
            raise AuditError("unsafe_audit")
        helpers = _secure_helpers()
        directory_fds: List[int] = []
        marker_fd: Optional[int] = None
        transaction_fd: Optional[int] = None
        current_fd: Optional[int] = None
        rotated_fd: Optional[int] = None
        marker_locked = False
        transaction_locked = False
        try:
            (
                directory_fds,
                links,
                anchor_fd,
                canonical_runtime,
            ) = self._open_anchor(helpers, self.runtime_dir)
            marker_name = _marker_name(canonical_runtime)
            transaction_name = _transaction_name(marker_name)
            try:
                marker_fd, _ignored = _open_named_file(
                    anchor_fd,
                    marker_name,
                    create=False,
                )
            except FileNotFoundError as error:
                raise AuditError("audit_corrupt") from error
            if marker_fd is None:
                raise AuditError("audit_corrupt")
            try:
                fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise AuditError("audit_busy") from error
                raise
            marker_locked = True
            transaction_fd, _ignored = _open_named_file(
                anchor_fd,
                transaction_name,
                create=True,
            )
            assert transaction_fd is not None
            try:
                fcntl.flock(transaction_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN):
                    raise AuditError("audit_busy") from error
                raise
            transaction_locked = True
            for link in links:
                helpers._verify_directory_link(link)
            _validate_named_file(anchor_fd, marker_name, marker_fd)
            marker_payload, _marker_details = _read_descriptor(
                marker_fd,
                2048,
            )
            marker = _parse_marker(marker_payload)
            runtime_fds, runtime_links, runtime_fd = self._open_existing_runtime(
                helpers,
                self.runtime_dir,
            )
            if runtime_fd is None:
                raise AuditError("audit_corrupt")
            directory_fds.extend(runtime_fds)
            links.extend(runtime_links)
            for link in links:
                helpers._verify_directory_link(link)
            key_fd, key, key_signature = self._open_key(
                runtime_fd,
                allow_create=False,
            )
            try:
                runtime_details = os.fstat(runtime_fd)
                key_details = os.fstat(key_fd)
                if hashlib.sha256(key).hexdigest() != marker["key_fingerprint"]:
                    raise AuditError("audit_corrupt")
                if (
                    self._existing_sensitive_file(
                        runtime_fd,
                        AUDIT_FILE_NAME,
                    )
                    is None
                    or self._existing_sensitive_file(
                        runtime_fd,
                        AUDIT_NEXT_FILE_NAME,
                    )
                    is not None
                ):
                    raise AuditError("audit_corrupt")
                current_fd, _ignored = _open_named_file(
                    runtime_fd,
                    AUDIT_FILE_NAME,
                    create=False,
                )
                assert current_fd is not None
                rotated_fd, _ignored = _open_named_file(
                    runtime_fd,
                    AUDIT_ROTATED_FILE_NAME,
                    create=False,
                )
                self._load(
                    runtime_fd,
                    current_fd,
                    rotated_fd,
                    marker["namespace_epoch"],
                )
                rebound = dict(marker)
                rebound.update(
                    {
                        "runtime_dev": int(runtime_details.st_dev),
                        "runtime_ino": int(runtime_details.st_ino),
                        "key_dev": int(key_details.st_dev),
                        "key_ino": int(key_details.st_ino),
                    }
                )
                _write_signature = self._write_marker(
                    anchor_fd,
                    marker_fd,
                    rebound,
                )
            finally:
                os.close(key_fd)
            os.fsync(runtime_fd)
            os.fsync(anchor_fd)
        except AuditError:
            raise
        except FileNotFoundError as error:
            raise AuditError("audit_corrupt") from error
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise _translate_filesystem_error(error) from error
        finally:
            if current_fd is not None:
                os.close(current_fd)
            if rotated_fd is not None:
                os.close(rotated_fd)
            if transaction_fd is not None:
                if transaction_locked:
                    try:
                        fcntl.flock(transaction_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(transaction_fd)
            if marker_fd is not None:
                if marker_locked:
                    try:
                        fcntl.flock(marker_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                os.close(marker_fd)
            for descriptor in reversed(directory_fds):
                os.close(descriptor)

    @staticmethod
    def _existing_sensitive_file(
        runtime_fd: int,
        name: str,
    ) -> Optional[os.stat_result]:
        try:
            details = os.stat(
                name,
                dir_fd=runtime_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise AuditError("unsafe_audit")
        return details

    @staticmethod
    def _has_nonempty_audit(runtime_fd: int) -> bool:
        for name in (
            AUDIT_FILE_NAME,
            AUDIT_ROTATED_FILE_NAME,
            AUDIT_NEXT_FILE_NAME,
        ):
            details = SendAuditStore._existing_sensitive_file(runtime_fd, name)
            if details is not None and details.st_size:
                return True
        return False

    @staticmethod
    def _write_marker(
        parent_fd: int,
        marker_fd: int,
        marker: Mapping[str, Any],
    ) -> Tuple[int, ...]:
        payload = (
            json.dumps(
                marker,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
        try:
            os.ftruncate(marker_fd, 0)
            os.lseek(marker_fd, 0, os.SEEK_SET)
            _write_all(marker_fd, payload)
            os.fsync(marker_fd)
            os.fsync(parent_fd)
            readback, details = _read_descriptor(marker_fd, 2048)
            if readback != payload or _parse_marker(readback) != marker:
                raise AuditError("audit_corrupt")
            return _file_signature(details)
        except AuditError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise AuditError("audit_error") from error

    def _open_key(
        self,
        runtime_fd: int,
        *,
        allow_create: bool,
    ) -> Tuple[int, bytes, Tuple[int, ...]]:
        details = self._existing_sensitive_file(runtime_fd, AUDIT_KEY_NAME)
        if details is None:
            if not allow_create:
                raise AuditError("audit_corrupt")
            for name in (AUDIT_FILE_NAME, AUDIT_ROTATED_FILE_NAME):
                audit_details = self._existing_sensitive_file(runtime_fd, name)
                if audit_details is not None and audit_details.st_size:
                    raise AuditError("audit_corrupt")
            key = secrets.token_bytes(32)
            flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(
                    AUDIT_KEY_NAME,
                    flags,
                    0o600,
                    dir_fd=runtime_fd,
                )
                try:
                    os.fchmod(descriptor, 0o600)
                    _write_all(descriptor, key)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(runtime_fd)
            except AuditError:
                raise
            except OSError as error:
                raise _translate_filesystem_error(error) from error
        flags = os.O_RDONLY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            key_fd = os.open(AUDIT_KEY_NAME, flags, dir_fd=runtime_fd)
            _validate_named_file(runtime_fd, AUDIT_KEY_NAME, key_fd)
            key, after = _read_descriptor(key_fd, 32)
            if len(key) != 32:
                raise AuditError("audit_corrupt")
            return key_fd, key, _file_signature(after)
        except BaseException as error:
            key_fd_value = locals().get("key_fd")
            if isinstance(key_fd_value, int):
                os.close(key_fd_value)
            if isinstance(error, AuditError):
                raise
            if not isinstance(error, Exception):
                raise
            raise _translate_filesystem_error(error) from error

    def _recover_rotation(
        self,
        runtime_fd: int,
        namespace_epoch: str,
    ) -> None:
        next_fd, _ignored = _open_named_file(
            runtime_fd,
            AUDIT_NEXT_FILE_NAME,
            create=False,
        )
        if next_fd is None:
            return
        try:
            current = self._existing_sensitive_file(
                runtime_fd,
                AUDIT_FILE_NAME,
            )
            rotated = self._existing_sensitive_file(
                runtime_fd,
                AUDIT_ROTATED_FILE_NAME,
            )
            if current is None and rotated is None:
                raise AuditError("audit_corrupt")
            try:
                records = _parse_file(
                    _read_file(
                        runtime_fd,
                        AUDIT_NEXT_FILE_NAME,
                        next_fd,
                    )
                )
                if (
                    not records
                    or any(
                        record.get("namespace_epoch") != namespace_epoch
                        for record in records
                    )
                ):
                    raise AuditError("audit_corrupt")
                _validate_histories(records)
            except AuditError:
                if current is None:
                    raise
                os.close(next_fd)
                next_fd = -1
                os.unlink(AUDIT_NEXT_FILE_NAME, dir_fd=runtime_fd)
                os.fsync(runtime_fd)
                return
            os.close(next_fd)
            next_fd = -1
            if current is not None:
                os.replace(
                    AUDIT_FILE_NAME,
                    AUDIT_ROTATED_FILE_NAME,
                    src_dir_fd=runtime_fd,
                    dst_dir_fd=runtime_fd,
                )
                os.fsync(runtime_fd)
            os.replace(
                AUDIT_NEXT_FILE_NAME,
                AUDIT_FILE_NAME,
                src_dir_fd=runtime_fd,
                dst_dir_fd=runtime_fd,
            )
            os.fsync(runtime_fd)
        except AuditError:
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise _translate_filesystem_error(error) from error
        finally:
            if next_fd >= 0:
                os.close(next_fd)

    def _open_logs(
        self,
        runtime_fd: int,
        namespace_epoch: str,
    ) -> Tuple[int, Optional[int]]:
        self._recover_rotation(runtime_fd, namespace_epoch)
        current_fd, created = _open_named_file(
            runtime_fd,
            AUDIT_FILE_NAME,
            create=True,
        )
        assert current_fd is not None
        rotated_fd: Optional[int] = None
        try:
            rotated_fd, _ignored = _open_named_file(
                runtime_fd,
                AUDIT_ROTATED_FILE_NAME,
                create=False,
            )
            if created:
                try:
                    os.fsync(runtime_fd)
                except OSError as error:
                    raise AuditError("audit_error") from error
            return current_fd, rotated_fd
        except BaseException:
            os.close(current_fd)
            if rotated_fd is not None:
                os.close(rotated_fd)
            raise

    def _load(
        self,
        runtime_fd: int,
        current_fd: int,
        rotated_fd: Optional[int],
        namespace_epoch: str,
    ) -> Dict[str, List[Mapping[str, Any]]]:
        records: List[Mapping[str, Any]] = []
        if rotated_fd is not None:
            records.extend(
                _parse_file(
                    _read_file(
                        runtime_fd,
                        AUDIT_ROTATED_FILE_NAME,
                        rotated_fd,
                    )
                )
            )
        records.extend(
            _parse_file(_read_file(runtime_fd, AUDIT_FILE_NAME, current_fd))
        )
        if any(
            record.get("namespace_epoch") != namespace_epoch
            for record in records
        ):
            raise AuditError("audit_corrupt")
        return _validate_histories(records)

    @staticmethod
    def _active_pending_payload(
        histories: Mapping[str, Sequence[Mapping[str, Any]]],
        record: Mapping[str, Any],
    ) -> bytes:
        active = {
            request_id: history[-1]
            for request_id, history in histories.items()
            if history[-1]["outcome"] == "pending"
        }
        if record["outcome"] == "pending":
            active[record["request_id"]] = record
        else:
            active.pop(record["request_id"], None)
        if len(active) > MAX_ACTIVE_PENDING_RECORDS:
            raise AuditError("audit_error")
        payloads = [
            _encoded_record(pending)
            for pending in sorted(
                active.values(),
                key=lambda value: (
                    value["timestamp"],
                    value["request_id"],
                ),
            )
        ]
        size = sum(len(payload) for payload in payloads)
        if size > MAX_ACTIVE_PENDING_BYTES:
            raise AuditError("audit_error")
        return b"".join(payloads)

    def _append(
        self,
        runtime_fd: int,
        current_fd: int,
        rotated_fd: Optional[int],
        record: Mapping[str, Any],
        histories: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> None:
        payload = _encoded_record(record)
        pending_payload = self._active_pending_payload(histories, record)
        next_fd: Optional[int] = None
        try:
            current_size = os.fstat(current_fd).st_size
            if current_size + len(payload) > MAX_AUDIT_BYTES:
                rotation_payload = pending_payload
                if record["outcome"] != "pending":
                    history = histories.get(record["request_id"])
                    if (
                        history is None
                        or history[-1]["outcome"] != "pending"
                    ):
                        raise AuditError("audit_corrupt")
                    rotation_payload += _encoded_record(history[-1])
                    rotation_payload += payload
                if (
                    not rotation_payload
                    or len(rotation_payload) > MAX_AUDIT_BYTES
                ):
                    raise AuditError("audit_error")
                if rotated_fd is not None:
                    _validate_named_file(
                        runtime_fd,
                        AUDIT_ROTATED_FILE_NAME,
                        rotated_fd,
                    )
                    os.close(rotated_fd)
                    rotated_fd = None
                _validate_named_file(runtime_fd, AUDIT_FILE_NAME, current_fd)
                next_fd, next_created = _open_named_file(
                    runtime_fd,
                    AUDIT_NEXT_FILE_NAME,
                    create=True,
                )
                assert next_fd is not None
                if not next_created:
                    raise AuditError("audit_corrupt")
                _write_all(next_fd, rotation_payload)
                os.fsync(next_fd)
                _validate_named_file(
                    runtime_fd,
                    AUDIT_NEXT_FILE_NAME,
                    next_fd,
                )
                os.lseek(next_fd, 0, os.SEEK_SET)
                if os.read(next_fd, len(rotation_payload)) != rotation_payload:
                    raise AuditError("audit_corrupt")
                os.fsync(runtime_fd)
                os.replace(
                    AUDIT_FILE_NAME,
                    AUDIT_ROTATED_FILE_NAME,
                    src_dir_fd=runtime_fd,
                    dst_dir_fd=runtime_fd,
                )
                _validate_named_file(
                    runtime_fd,
                    AUDIT_ROTATED_FILE_NAME,
                    current_fd,
                )
                os.fsync(runtime_fd)
                os.close(current_fd)
                current_fd = -1
                os.close(next_fd)
                next_fd = None
                os.replace(
                    AUDIT_NEXT_FILE_NAME,
                    AUDIT_FILE_NAME,
                    src_dir_fd=runtime_fd,
                    dst_dir_fd=runtime_fd,
                )
                os.fsync(runtime_fd)
                return
            _validate_named_file(runtime_fd, AUDIT_FILE_NAME, current_fd)
            before_size = int(os.fstat(current_fd).st_size)
            _write_all(current_fd, payload)
            os.fsync(current_fd)
            _validate_named_file(runtime_fd, AUDIT_FILE_NAME, current_fd)
            after_size = int(os.fstat(current_fd).st_size)
            if after_size != before_size + len(payload):
                raise AuditError("audit_corrupt")
            os.lseek(current_fd, before_size, os.SEEK_SET)
            if os.read(current_fd, len(payload)) != payload:
                raise AuditError("audit_corrupt")
        except AuditError:
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise _translate_filesystem_error(error) from error
        finally:
            if current_fd >= 0:
                os.close(current_fd)
            if rotated_fd is not None:
                os.close(rotated_fd)
            if next_fd is not None:
                os.close(next_fd)

    @staticmethod
    def _base_record(
        request_id: str,
        identity: AuditIdentity,
        key: bytes,
        namespace_epoch: str,
        outcome: str,
    ) -> Dict[str, Any]:
        identity_values = _identity_tuple(identity, key)
        return {
            "schema_version": AUDIT_SCHEMA_VERSION,
            "timestamp": time.time(),
            "request_id": request_id,
            "namespace_epoch": namespace_epoch,
            "agent": identity.agent,
            "target_hmac": identity_values[1],
            "request_hmac": identity_values[2],
            "executable_basename": identity.executable_basename,
            "confirmation_mode": identity.confirmation_mode,
            "outcome": outcome,
        }

    def _reserve(
        self,
        namespace: AuditNamespace,
        request_id: object,
        identity: AuditIdentity,
    ) -> Optional[AuditReceipt]:
        """Reserve once, or return the latest same-target retained receipt."""

        validated_id = validate_request_id(request_id)
        if not isinstance(identity, AuditIdentity):
            raise AuditError("audit_error")
        with self._locked(namespace):
            current_fd, rotated_fd = self._open_logs(
                namespace.runtime_fd,
                namespace.namespace_epoch,
            )
            try:
                histories = self._load(
                    namespace.runtime_fd,
                    current_fd,
                    rotated_fd,
                    namespace.namespace_epoch,
                )
                history = histories.get(validated_id)
                if history is not None:
                    if _record_identity(history[-1]) != _identity_tuple(
                        identity,
                        namespace.key,
                    ):
                        raise AuditError("request_conflict")
                    receipt = _receipt(history[-1])
                    os.close(current_fd)
                    if rotated_fd is not None:
                        os.close(rotated_fd)
                    return receipt
            except BaseException:
                os.close(current_fd)
                if rotated_fd is not None:
                    os.close(rotated_fd)
                raise
            record = self._base_record(
                validated_id,
                identity,
                namespace.key,
                namespace.namespace_epoch,
                "pending",
            )
            _validate_record(record)
            self._append(
                namespace.runtime_fd,
                current_fd,
                rotated_fd,
                record,
                histories,
            )
        return None

    def _append_terminal(
        self,
        namespace: AuditNamespace,
        request_id: object,
        identity: AuditIdentity,
        *,
        outcome: str,
        delivery: str,
        error: Optional[str],
        returncode: Optional[int],
    ) -> AuditReceipt:
        """Append and fsync one terminal record for a reservation."""

        validated_id = validate_request_id(request_id)
        with self._locked(namespace):
            current_fd, rotated_fd = self._open_logs(
                namespace.runtime_fd,
                namespace.namespace_epoch,
            )
            try:
                histories = self._load(
                    namespace.runtime_fd,
                    current_fd,
                    rotated_fd,
                    namespace.namespace_epoch,
                )
                history = histories.get(validated_id)
                if history is None:
                    raise AuditError("audit_corrupt")
                if _record_identity(history[-1]) != _identity_tuple(
                    identity,
                    namespace.key,
                ):
                    raise AuditError("request_conflict")
                if history[-1]["outcome"] != "pending":
                    raise AuditError("audit_corrupt")
                record = self._base_record(
                    validated_id,
                    identity,
                    namespace.key,
                    namespace.namespace_epoch,
                    outcome,
                )
                record.update(
                    {
                        "delivery": delivery,
                        "error": error,
                        "returncode": returncode,
                    }
                )
                record = dict(_validate_record(record))
            except BaseException:
                os.close(current_fd)
                if rotated_fd is not None:
                    os.close(rotated_fd)
                raise
            self._append(
                namespace.runtime_fd,
                current_fd,
                rotated_fd,
                record,
                histories,
            )
        return _receipt(record)

    def reserve(
        self,
        request_id: object,
        identity: AuditIdentity,
    ) -> Optional[AuditReceipt]:
        with self.open_reserved(request_id, identity) as (
            _namespace,
            replay,
        ):
            return replay

    def append_terminal(
        self,
        request_id: object,
        identity: AuditIdentity,
        *,
        outcome: str,
        delivery: str,
        error: Optional[str],
        returncode: Optional[int],
    ) -> AuditReceipt:
        with self.open_namespace() as namespace:
            return namespace.append_terminal(
                request_id,
                identity,
                outcome=outcome,
                delivery=delivery,
                error=error,
                returncode=returncode,
            )


__all__ = [
    "AUDIT_FILE_NAME",
    "AUDIT_KEY_NAME",
    "AUDIT_LOCK_NAME",
    "AUDIT_NEXT_FILE_NAME",
    "AUDIT_ROTATED_FILE_NAME",
    "AUDIT_SCHEMA_VERSION",
    "AuditError",
    "AuditIdentity",
    "AuditNamespace",
    "AuditReceipt",
    "IDEMPOTENCY_RETENTION_NOTICE",
    "MAX_AUDIT_BYTES",
    "MAX_AUDIT_LINE_BYTES",
    "MAX_ACTIVE_PENDING_BYTES",
    "MAX_ACTIVE_PENDING_RECORDS",
    "MAX_REQUEST_ID_BYTES",
    "NAMESPACE_ANCHOR_NAME",
    "SendAuditStore",
    "generate_request_id",
    "make_audit_identity",
    "validate_request_id",
]
