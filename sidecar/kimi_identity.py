"""Fail-closed filesystem identity for mutation of native Kimi sessions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

from sidecar.json_limits import JSONLimits, validate_json
from sidecar.model import Session

KIMI_IDENTITY_SCHEMA_VERSION = 1

_INDEX_BYTES = 4 * 1024 * 1024
_DOCUMENT_BYTES = 512 * 1024
_WIRE_BYTES = 16 * 1024 * 1024
_JSONL_LINE_BYTES = 4 * 1024 * 1024
_JSONL_RECORDS = 8192
_PATH_BYTES = 4096
_PATH_COMPONENTS = 64
_JSON_LIMITS = JSONLimits(
    max_depth=32,
    max_items=8192,
    max_nodes=16384,
    max_string_bytes=_JSONL_LINE_BYTES,
    max_integer_bits=256,
)
_NUMBER_TOKEN_CHARS = 128
_NATIVE_ROOT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,511}$")


class KimiIdentityError(ValueError):
    """A display-safe Kimi identity failure."""

    def __init__(self, code: str = "invalid_session") -> None:
        if code not in (
            "child_session",
            "invalid_session",
            "remote_session",
            "session_changed",
        ):
            code = "invalid_session"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class DirectoryIdentity:
    canonical_path: str
    dev: int
    ino: int
    mode: int
    uid: int


@dataclass(frozen=True, repr=False)
class FileIdentity:
    canonical_path: str
    dev: int
    ino: int
    mode: int
    uid: int


@dataclass(frozen=True)
class FileGeneration:
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, repr=False)
class _TailRead:
    payload: bytes = field(repr=False)
    offset: int
    truncated: bool
    starts_at_boundary: bool


@dataclass(frozen=True, repr=False)
class ProjectSource:
    kind: str
    lexical_digest: str
    identity: DirectoryIdentity


class _Anchors:
    def __init__(
        self,
        descriptors: Sequence[int],
        named: Optional[Mapping[str, int]] = None,
    ) -> None:
        self._descriptors = tuple(descriptors)
        self._named = dict(named or {})
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in reversed(self._descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def descriptor(self, name: str) -> int:
        if self._closed or name not in self._named:
            _raise("session_changed")
        return self._named[name]


@dataclass(frozen=True, repr=False)
class KimiIdentityEvidence:
    """Anchored, repr-hidden evidence for one exact native root session."""

    schema_version: int
    native_root_id: str
    index_session_id: str
    state_session_id: str
    directory_session_id: str
    agent_id: str
    native_root: bool
    home_origin: str
    home_value_digest: str
    home: DirectoryIdentity
    session_dir: DirectoryIdentity
    project: DirectoryIdentity
    project_sources: Tuple[ProjectSource, ...]
    index_file: FileIdentity
    index_generation: FileGeneration
    index_content_offset: int
    index_content_size: int
    index_content_digest: str
    index_row_digest: str
    workspaces_file: Optional[FileIdentity]
    workspaces_generation: Optional[FileGeneration]
    workspace_row_digest: Optional[str]
    state_file: FileIdentity
    state_generation: FileGeneration
    state_identity_digest: str
    root_wire: FileIdentity
    root_wire_generation: FileGeneration
    root_wire_content_digest: str
    _anchors: _Anchors = field(compare=False, repr=False)

    @property
    def ids_agree(self) -> bool:
        return (
            self.native_root_id
            == self.index_session_id
            == self.state_session_id
            == self.directory_session_id
        )

    @property
    def projects_agree(self) -> bool:
        expected = (self.project.dev, self.project.ino)
        return bool(self.project_sources) and all(
            (source.identity.dev, source.identity.ino) == expected
            for source in self.project_sources
        )

    @property
    def closed(self) -> bool:
        return self._anchors.closed

    def close(self) -> None:
        """Release held descriptors. Repeated calls are harmless."""

        self._anchors.close()

    def __enter__(self) -> "KimiIdentityEvidence":
        if self.closed:
            raise KimiIdentityError("session_changed")
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()

    def revalidate(
        self,
        session: Session,
        *,
        home: Optional[Path] = None,
    ) -> "KimiIdentityEvidence":
        return revalidate_kimi_identity(self, session, home=home)

    def revalidate_after_kimi_start(
        self,
        session: Session,
        *,
        home: Optional[Path] = None,
    ) -> "KimiIdentityEvidence":
        return revalidate_kimi_identity_after_kimi_start(self, session, home=home)


def _raise(code: str = "invalid_session") -> None:
    raise KimiIdentityError(code)


def _digest(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError):
        _raise()
    return hashlib.sha256(payload).hexdigest()


def _mtime_ns(details: os.stat_result) -> int:
    return int(getattr(details, "st_mtime_ns", int(details.st_mtime * 1e9)))


def _ctime_ns(details: os.stat_result) -> int:
    return int(getattr(details, "st_ctime_ns", int(details.st_ctime * 1e9)))


def _generation(details: os.stat_result) -> FileGeneration:
    return FileGeneration(
        int(details.st_size),
        _mtime_ns(details),
        _ctime_ns(details),
    )


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        _raise()
    return int(getter())


def _owner_safe(details: os.stat_result, *, directory: bool) -> bool:
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        expected_kind(details.st_mode)
        and int(details.st_uid) == _effective_uid()
        and not bool(stat.S_IMODE(details.st_mode) & 0o022)
    )


def _directory_identity(path: str, details: os.stat_result) -> DirectoryIdentity:
    return DirectoryIdentity(
        path,
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_uid),
    )


def _file_identity(path: str, details: os.stat_result) -> FileIdentity:
    return FileIdentity(
        path,
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_uid),
    )


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        _raise()
    return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        int(left.st_dev),
        int(left.st_ino),
        int(left.st_mode),
        int(left.st_uid),
    ) == (
        int(right.st_dev),
        int(right.st_ino),
        int(right.st_mode),
        int(right.st_uid),
    )


class _Capture:
    def __init__(self) -> None:
        self.descriptors: List[int] = []

    def keep(self, descriptor: int) -> int:
        self.descriptors.append(descriptor)
        return descriptor

    def release(self, named: Optional[Mapping[str, int]] = None) -> _Anchors:
        anchors = _Anchors(self.descriptors, named)
        self.descriptors = []
        return anchors

    def close(self) -> None:
        _Anchors(self.descriptors).close()
        self.descriptors = []


def _open_absolute_directory(
    capture: _Capture,
    lexical: str,
    *,
    reject_symlink_leaf: bool,
) -> Tuple[int, DirectoryIdentity]:
    if (
        not lexical
        or "\x00" in lexical
        or not os.path.isabs(lexical)
        or len(os.fsencode(lexical)) > _PATH_BYTES
    ):
        _raise()
    try:
        flags = _directory_flags()
        lexical_details: Optional[os.stat_result] = None
        if reject_symlink_leaf:
            lexical_fd = capture.keep(os.open(lexical, flags))
            lexical_details = os.fstat(lexical_fd)
            if not _owner_safe(lexical_details, directory=True):
                _raise()
        canonical = os.path.realpath(lexical)
        components = Path(canonical).parts[1:]
        if (
            not canonical
            or not os.path.isabs(canonical)
            or len(os.fsencode(canonical)) > _PATH_BYTES
            or len(components) > _PATH_COMPONENTS
        ):
            _raise()
        current = capture.keep(os.open(os.path.sep, flags))
        details = os.fstat(current)
        for component in components:
            if not component or component in (".", ".."):
                _raise()
            descriptor = capture.keep(os.open(component, flags, dir_fd=current))
            linked = os.stat(component, dir_fd=current, follow_symlinks=False)
            opened = os.fstat(descriptor)
            if not _same_object(linked, opened):
                _raise()
            current = descriptor
            details = opened
        if not _owner_safe(details, directory=True):
            _raise()
        if lexical_details is not None and not _same_object(lexical_details, details):
            _raise()
        if os.path.realpath(lexical) != canonical:
            _raise()
        return current, _directory_identity(canonical, details)
    except KimiIdentityError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        _raise()


def _contained_components(root: str, value: Any) -> Tuple[str, ...]:
    candidate = _text(value).strip()
    if not candidate or "\x00" in candidate:
        _raise()
    try:
        expanded = os.path.expandvars(os.path.expanduser(candidate))
    except (OSError, RuntimeError, ValueError):
        _raise()
    raw_parts = Path(expanded).parts
    if ".." in raw_parts:
        _raise()
    normalized_root = os.path.normpath(root)
    if os.path.isabs(expanded):
        normalized = os.path.normpath(expanded)
        try:
            if os.path.commonpath((normalized_root, normalized)) != normalized_root:
                _raise()
            relative = os.path.relpath(normalized, normalized_root)
        except ValueError:
            _raise()
    else:
        relative = os.path.normpath(expanded)
    components = Path(relative).parts
    if (
        not components
        or components == (".",)
        or len(components) > _PATH_COMPONENTS
        or any(
            not component or component in (".", "..") or os.path.sep in component
            for component in components
        )
    ):
        _raise()
    return tuple(components)


def _open_contained_directory(
    capture: _Capture,
    root_fd: int,
    root: DirectoryIdentity,
    components: Sequence[str],
) -> Tuple[int, DirectoryIdentity]:
    current_fd = root_fd
    current_identity = root
    for component in components:
        current_fd, current_identity = _open_directory_at(
            capture,
            current_fd,
            current_identity,
            component,
        )
    return current_fd, current_identity


def _open_directory_at(
    capture: _Capture,
    parent_fd: int,
    parent: DirectoryIdentity,
    name: str,
) -> Tuple[int, DirectoryIdentity]:
    if not name or name in (".", "..") or os.path.sep in name:
        _raise()
    try:
        descriptor = capture.keep(
            os.open(name, _directory_flags(), dir_fd=parent_fd)
        )
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if not _same_object(linked, opened) or not _owner_safe(
            opened, directory=True
        ):
            _raise()
        path = os.path.join(parent.canonical_path, name)
        return descriptor, _directory_identity(path, opened)
    except KimiIdentityError:
        raise
    except (OSError, TypeError, ValueError):
        _raise()


def _open_regular_at(
    capture: _Capture,
    parent_fd: int,
    parent: DirectoryIdentity,
    name: str,
    limit: int,
) -> Tuple[int, FileIdentity, FileGeneration, bytes]:
    if not name or name in (".", "..") or os.path.sep in name:
        _raise()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if not getattr(os, "O_NOFOLLOW", 0):
        _raise()
    try:
        descriptor = capture.keep(os.open(name, flags, dir_fd=parent_fd))
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        before = os.fstat(descriptor)
        if (
            not _same_object(linked, before)
            or not _owner_safe(before, directory=False)
            or before.st_size < 0
            or before.st_size > limit
        ):
            _raise()
        chunks: List[bytes] = []
        remaining = int(before.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                _raise()
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            _raise()
        after = os.fstat(descriptor)
        if not _same_object(before, after) or _generation(before) != _generation(after):
            _raise()
        payload = b"".join(chunks)
        if len(payload) != int(after.st_size):
            _raise()
        path = os.path.join(parent.canonical_path, name)
        return (
            descriptor,
            _file_identity(path, after),
            _generation(after),
            payload,
        )
    except KimiIdentityError:
        raise
    except (OSError, TypeError, ValueError):
        _raise()


def _read_fd_tail(descriptor: int, size: int, limit: int) -> _TailRead:
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
    ):
        _raise()
    offset = max(0, size - limit)
    read_offset = offset - 1 if offset else 0
    expected = size - read_offset
    chunks: List[bytes] = []
    consumed = 0
    try:
        while consumed < expected:
            chunk = os.pread(
                descriptor,
                min(expected - consumed, 1024 * 1024),
                read_offset + consumed,
            )
            if not chunk:
                _raise()
            chunks.append(chunk)
            consumed += len(chunk)
    except KimiIdentityError:
        raise
    except (AttributeError, OSError, TypeError, ValueError):
        _raise()
    raw = b"".join(chunks)
    if len(raw) != expected:
        _raise()
    if offset:
        return _TailRead(
            raw[1:],
            offset,
            True,
            raw[:1] == b"\n",
        )
    return _TailRead(raw, 0, False, True)


def _open_regular_tail_at(
    capture: _Capture,
    parent_fd: int,
    parent: DirectoryIdentity,
    name: str,
    limit: int,
) -> Tuple[int, FileIdentity, FileGeneration, _TailRead]:
    if not name or name in (".", "..") or os.path.sep in name:
        _raise()
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    if not getattr(os, "O_NOFOLLOW", 0):
        _raise()
    try:
        descriptor = capture.keep(os.open(name, flags, dir_fd=parent_fd))
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        before = os.fstat(descriptor)
        if (
            not _same_object(linked, before)
            or not _owner_safe(before, directory=False)
            or before.st_size < 0
        ):
            _raise()
        tail = _read_fd_tail(descriptor, int(before.st_size), limit)
        after = os.fstat(descriptor)
        if not _same_object(before, after) or _generation(before) != _generation(after):
            _raise()
        path = os.path.join(parent.canonical_path, name)
        return (
            descriptor,
            _file_identity(path, after),
            _generation(after),
            tail,
        )
    except KimiIdentityError:
        raise
    except (OSError, TypeError, ValueError):
        _raise()


def _optional_regular_at(
    capture: _Capture,
    parent_fd: int,
    parent: DirectoryIdentity,
    name: str,
    limit: int,
) -> Optional[Tuple[int, FileIdentity, FileGeneration, bytes]]:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError):
        _raise()
    return _open_regular_at(capture, parent_fd, parent, name, limit)


def _descriptor_range_digest(descriptor: int, offset: int, size: int) -> str:
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
    ):
        _raise("session_changed")
    digest = hashlib.sha256()
    consumed = 0
    try:
        while consumed < size:
            chunk = os.pread(
                descriptor,
                min(size - consumed, 1024 * 1024),
                offset + consumed,
            )
            if not chunk:
                _raise("session_changed")
            digest.update(chunk)
            consumed += len(chunk)
    except KimiIdentityError:
        raise
    except (AttributeError, OSError, TypeError, ValueError):
        _raise("session_changed")
    return digest.hexdigest()


def _json_pairs(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _raise()
        value[key] = item
    return value


def _parse_integer(value: str) -> int:
    if len(value) > _NUMBER_TOKEN_CHARS:
        _raise()
    try:
        parsed = int(value)
    except ValueError:
        _raise()
    if parsed.bit_length() > 256:
        _raise()
    return parsed


def _parse_float(value: str) -> float:
    if len(value) > _NUMBER_TOKEN_CHARS:
        _raise()
    try:
        parsed = float(value)
    except ValueError:
        _raise()
    if not math.isfinite(parsed):
        _raise()
    return parsed


def _reject_constant(unused: str) -> None:
    del unused
    _raise()


def _strict_json(payload: bytes) -> Any:
    if not payload:
        _raise()
    try:
        text = payload.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_json_pairs,
            parse_constant=_reject_constant,
            parse_int=_parse_integer,
            parse_float=_parse_float,
        )
        validate_json(value, _JSON_LIMITS)
        return value
    except KimiIdentityError:
        raise
    except (RecursionError, UnicodeError, ValueError):
        _raise()


def _strict_jsonl(payload: bytes, *, allow_empty: bool) -> Tuple[Any, ...]:
    if not payload:
        return () if allow_empty else _raise()
    if not payload.endswith(b"\n"):
        _raise()
    records: List[Any] = []
    for line in payload.splitlines():
        if (
            not line
            or len(line) > _JSONL_LINE_BYTES
            or len(records) >= _JSONL_RECORDS
        ):
            _raise()
        records.append(_strict_json(line))
    return tuple(records)


def _parse_index_tail(tail: _TailRead) -> Tuple[Tuple[Mapping[str, Any], ...], bool]:
    payload = tail.payload
    valid = True
    if tail.truncated and not tail.starts_at_boundary:
        boundary = payload.find(b"\n")
        if boundary < 0:
            return (), False
        payload = payload[boundary + 1 :]
    if payload and not payload.endswith(b"\n"):
        valid = False
        boundary = payload.rfind(b"\n")
        payload = payload[: boundary + 1] if boundary >= 0 else b""
    records: Deque[Mapping[str, Any]] = deque(maxlen=_JSONL_RECORDS)
    for line in payload.splitlines():
        if not line or len(line) > _JSONL_LINE_BYTES:
            valid = False
            continue
        try:
            records.append(_mapping(_strict_json(line)))
        except KimiIdentityError:
            valid = False
    return tuple(records), valid


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _raise()
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        _raise()
    return value


def _native_id(value: Any) -> str:
    candidate = _text(value)
    try:
        encoded = candidate.encode("ascii", "strict")
    except UnicodeError:
        _raise()
    if len(encoded) > 512 or _NATIVE_ROOT_ID.fullmatch(candidate) is None:
        _raise()
    return candidate


def _has_remote_provenance(extra: Mapping[str, Any]) -> bool:
    return (
        "host" in extra
        or ("remote" in extra and extra.get("remote") is not False)
        or extra.get("source") == "remote"
    )


def _validate_root_session(session: Session) -> Tuple[str, str]:
    if not isinstance(session, Session) or session.agent != "kimi":
        _raise()
    if not isinstance(session.extra, Mapping):
        _raise()
    if (
        session.parent_id is not None
        or session.extra.get("sidechain", False) is not False
        or session.extra.get("subagent", False) is not False
    ):
        _raise("child_session")
    agent_id = session.extra.get("agent_id", "main")
    if agent_id != "main":
        _raise("child_session")
    if _has_remote_provenance(session.extra):
        _raise("remote_session")
    try:
        session_id = _native_id(session.session_id)
    except KimiIdentityError:
        _raise("child_session" if ":" in str(session.session_id) else "invalid_session")
    return session_id, agent_id


def _configured_home(home: Optional[Path]) -> Tuple[str, str, str]:
    configured = os.environ.get("KIMI_CODE_HOME")
    if configured:
        origin = "environment"
        raw = configured
    else:
        origin = "default"
        try:
            raw = str((Path.home() if home is None else Path(home)) / ".kimi-code")
        except (TypeError, ValueError):
            _raise()
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        _raise()
    try:
        expanded = os.path.expandvars(os.path.expanduser(raw))
    except (OSError, RuntimeError, ValueError):
        _raise()
    if not expanded or not os.path.isabs(expanded):
        _raise()
    return origin, expanded, _digest((origin, raw, expanded))


def _project_source(
    capture: _Capture,
    kind: str,
    value: Any,
) -> ProjectSource:
    lexical = _text(value).strip()
    if not lexical or "\x00" in lexical:
        _raise()
    try:
        expanded = os.path.expandvars(os.path.expanduser(lexical))
    except (OSError, RuntimeError, ValueError):
        _raise()
    if not os.path.isabs(expanded):
        _raise()
    unused, identity = _open_absolute_directory(
        capture,
        expanded,
        reject_symlink_leaf=False,
    )
    del unused
    return ProjectSource(kind, _digest((kind, lexical, expanded)), identity)


def _present_project_value(mapping: Mapping[str, Any], key: str) -> Optional[str]:
    if key not in mapping:
        return None
    value = mapping.get(key)
    if value is None or value == "":
        return None
    return _text(value)


def _bind_session_transcript(
    capture: _Capture,
    value: Any,
    wire: FileIdentity,
) -> None:
    lexical = _text(value).strip()
    if not lexical or "\x00" in lexical:
        _raise()
    try:
        expanded = os.path.expandvars(os.path.expanduser(lexical))
        if not os.path.isabs(expanded):
            _raise()
        canonical = os.path.realpath(expanded)
        if canonical != wire.canonical_path:
            _raise()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        if not getattr(os, "O_NOFOLLOW", 0):
            _raise()
        descriptor = capture.keep(os.open(canonical, flags))
        details = os.fstat(descriptor)
        if (
            not _owner_safe(details, directory=False)
            or _file_identity(canonical, details) != wire
            or os.path.realpath(expanded) != canonical
        ):
            _raise()
    except KimiIdentityError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        _raise()


def _capture(session: Session, *, home: Optional[Path]) -> KimiIdentityEvidence:
    capture = _Capture()
    try:
        session_id, agent_id = _validate_root_session(session)
        origin, home_path, home_digest = _configured_home(home)
        home_fd, home_identity = _open_absolute_directory(
            capture,
            home_path,
            reject_symlink_leaf=True,
        )

        index_fd, index_identity, index_generation, index_tail = (
            _open_regular_tail_at(
                capture,
                home_fd,
                home_identity,
                "session_index.jsonl",
                _INDEX_BYTES,
            )
        )
        index_records, index_valid = _parse_index_tail(index_tail)
        if not index_valid:
            _raise()
        index_row: Optional[Mapping[str, Any]] = None
        for value in index_records:
            row = _mapping(value)
            row_id = row.get("sessionId")
            if not isinstance(row_id, str) or not row_id:
                _raise()
            if row_id == session_id:
                index_row = row
        if index_row is None:
            _raise()
        index_id = _native_id(index_row.get("sessionId"))

        session_components = _contained_components(
            home_path,
            index_row.get("sessionDir"),
        )
        session_fd, session_identity = _open_contained_directory(
            capture,
            home_fd,
            home_identity,
            session_components,
        )
        directory_id = _native_id(Path(session_identity.canonical_path).name)

        state_fd, state_identity, state_generation, state_payload = _open_regular_at(
            capture,
            session_fd,
            session_identity,
            "state.json",
            _DOCUMENT_BYTES,
        )
        state = _mapping(_strict_json(state_payload))
        state_id = _native_id(state.get("id"))
        state_agents = _mapping(state.get("agents"))
        main_agent = _mapping(state_agents.get("main"))

        optional_workspaces = _optional_regular_at(
            capture,
            home_fd,
            home_identity,
            "workspaces.json",
            _DOCUMENT_BYTES,
        )
        workspaces_identity: Optional[FileIdentity] = None
        workspaces_generation: Optional[FileGeneration] = None
        workspace_row: Optional[Mapping[str, Any]] = None
        workspace_digest: Optional[str] = None
        if optional_workspaces is not None:
            (
                unused,
                workspaces_identity,
                workspaces_generation,
                workspace_payload,
            ) = optional_workspaces
            del unused
            workspaces_document = _mapping(_strict_json(workspace_payload))
            workspaces = _mapping(workspaces_document.get("workspaces", {}))
            workspace_id = Path(session_identity.canonical_path).parent.name
            candidate = workspaces.get(workspace_id)
            if candidate is not None:
                workspace_row = _mapping(candidate)
                workspace_digest = _digest(workspace_row)

        project_sources: List[ProjectSource] = [
            _project_source(capture, "adapter", session.project)
        ]
        index_project = _present_project_value(index_row, "workDir")
        if index_project is not None:
            project_sources.append(
                _project_source(capture, "index", index_project)
            )
        state_project = _present_project_value(state, "cwd")
        if state_project is not None:
            project_sources.append(
                _project_source(capture, "state", state_project)
            )
        if workspace_row is not None:
            workspace_project = _present_project_value(workspace_row, "root")
            if workspace_project is not None:
                project_sources.append(
                    _project_source(capture, "workspace", workspace_project)
                )
        project_identity = project_sources[0].identity
        project_key = (project_identity.dev, project_identity.ino)
        if any(
            (source.identity.dev, source.identity.ino) != project_key
            for source in project_sources
        ):
            _raise()

        agents_fd, agents_identity = _open_directory_at(
            capture,
            session_fd,
            session_identity,
            "agents",
        )
        main_fd, main_identity = _open_directory_at(
            capture,
            agents_fd,
            agents_identity,
            "main",
        )
        homedir = _text(main_agent.get("homedir")).strip()
        if not homedir:
            _raise()
        if os.path.isabs(homedir):
            homedir_components = _contained_components(home_path, homedir)
        else:
            homedir_components = _contained_components(
                home_path,
                os.path.join(*(tuple(session_components) + (homedir,))),
            )
        unused_homedir_fd, homedir_identity = _open_contained_directory(
            capture,
            home_fd,
            home_identity,
            homedir_components,
        )
        del unused_homedir_fd
        if (
            homedir_identity.dev,
            homedir_identity.ino,
        ) != (
            main_identity.dev,
            main_identity.ino,
        ):
            _raise()
        wire_fd, wire_identity, wire_generation, wire_payload = _open_regular_at(
            capture,
            main_fd,
            main_identity,
            "wire.jsonl",
            _WIRE_BYTES,
        )
        for record in _strict_jsonl(wire_payload, allow_empty=True):
            _mapping(record)
        _bind_session_transcript(capture, session.transcript, wire_identity)

        if not (
            session_id == index_id == state_id == directory_id
            and agent_id == "main"
        ):
            _raise()

        evidence = KimiIdentityEvidence(
            schema_version=KIMI_IDENTITY_SCHEMA_VERSION,
            native_root_id=session_id,
            index_session_id=index_id,
            state_session_id=state_id,
            directory_session_id=directory_id,
            agent_id=agent_id,
            native_root=True,
            home_origin=origin,
            home_value_digest=home_digest,
            home=home_identity,
            session_dir=session_identity,
            project=project_identity,
            project_sources=tuple(project_sources),
            index_file=index_identity,
            index_generation=index_generation,
            index_content_offset=index_tail.offset,
            index_content_size=len(index_tail.payload),
            index_content_digest=hashlib.sha256(index_tail.payload).hexdigest(),
            index_row_digest=_digest(
                {
                    "sessionId": index_row.get("sessionId"),
                    "sessionDir": index_row.get("sessionDir"),
                    "workDir": index_row.get("workDir"),
                }
            ),
            workspaces_file=workspaces_identity,
            workspaces_generation=workspaces_generation,
            workspace_row_digest=workspace_digest,
            state_file=state_identity,
            state_generation=state_generation,
            state_identity_digest=_digest(
                {
                    "id": state.get("id"),
                    "cwd": state.get("cwd"),
                    "main": main_agent,
                }
            ),
            root_wire=wire_identity,
            root_wire_generation=wire_generation,
            root_wire_content_digest=hashlib.sha256(wire_payload).hexdigest(),
            _anchors=capture.release(
                {
                    "index": index_fd,
                    "state": state_fd,
                    "wire": wire_fd,
                }
            ),
        )
        if not evidence.ids_agree or not evidence.projects_agree:
            evidence.close()
            _raise()
        return evidence
    except KimiIdentityError:
        capture.close()
        raise
    except Exception:
        capture.close()
        _raise()


def capture_kimi_identity(
    session: Session,
    *,
    home: Optional[Path] = None,
) -> KimiIdentityEvidence:
    """Capture owner-safe, descriptor-anchored identity for a Kimi root."""

    return _capture(session, home=home)


def read_kimi_index_metadata(
    path: Path,
    *,
    max_bytes: int = _INDEX_BYTES,
) -> Tuple[Tuple[Mapping[str, Any], ...], bool]:
    """Read the observation index once, preserving valid rows and validity."""

    try:
        with Path(path).open("rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size < 0:
                return (), False
            tail = _read_fd_tail(
                stream.fileno(),
                int(before.st_size),
                max_bytes,
            )
            after = os.fstat(stream.fileno())
            if (
                not _same_object(before, after)
                or _generation(before) != _generation(after)
            ):
                return (), False
    except (KimiIdentityError, OSError, TypeError, ValueError):
        return (), False
    try:
        return _parse_index_tail(tail)
    except KimiIdentityError:
        return (), False


def revalidate_kimi_identity(
    evidence: KimiIdentityEvidence,
    session: Session,
    *,
    home: Optional[Path] = None,
) -> KimiIdentityEvidence:
    """Recapture and require exact source identities and generations."""

    if not isinstance(evidence, KimiIdentityEvidence) or evidence.closed:
        _raise("session_changed")
    try:
        fresh = _capture(session, home=home)
    except KimiIdentityError:
        _raise("session_changed")
    if fresh != evidence:
        fresh.close()
        _raise("session_changed")
    return fresh


def revalidate_kimi_identity_after_kimi_start(
    evidence: KimiIdentityEvidence,
    session: Session,
    *,
    home: Optional[Path] = None,
) -> KimiIdentityEvidence:
    """Rebind controlled Kimi-owned index/state/wire generation updates.

    Kimi may append its index and root wire and may atomically replace state
    after startup. Stable IDs, projects, home, index/wire inodes, and the
    identity-bearing state fields must remain unchanged.
    """

    if not isinstance(evidence, KimiIdentityEvidence) or evidence.closed:
        _raise("session_changed")
    try:
        fresh = _capture(session, home=home)
    except KimiIdentityError:
        _raise("session_changed")
    try:
        stable = (
            fresh.schema_version == evidence.schema_version
            and fresh.native_root_id == evidence.native_root_id
            and fresh.index_session_id == evidence.index_session_id
            and fresh.state_session_id == evidence.state_session_id
            and fresh.directory_session_id == evidence.directory_session_id
            and fresh.agent_id == evidence.agent_id
            and fresh.native_root == evidence.native_root
            and fresh.home_origin == evidence.home_origin
            and fresh.home_value_digest == evidence.home_value_digest
            and fresh.home == evidence.home
            and fresh.session_dir == evidence.session_dir
            and fresh.project == evidence.project
            and fresh.project_sources == evidence.project_sources
            and fresh.index_file == evidence.index_file
            and fresh.index_row_digest == evidence.index_row_digest
            and fresh.index_generation.size >= evidence.index_generation.size
            and (
                fresh.index_generation.size > evidence.index_generation.size
                or fresh.index_generation == evidence.index_generation
            )
            and _descriptor_range_digest(
                fresh._anchors.descriptor("index"),
                evidence.index_content_offset,
                evidence.index_content_size,
            )
            == evidence.index_content_digest
            and fresh.workspaces_file == evidence.workspaces_file
            and fresh.workspaces_generation == evidence.workspaces_generation
            and fresh.workspace_row_digest == evidence.workspace_row_digest
            and fresh.state_identity_digest == evidence.state_identity_digest
            and fresh.root_wire == evidence.root_wire
            and fresh.root_wire_generation.size
            >= evidence.root_wire_generation.size
            and (
                fresh.root_wire_generation.size
                > evidence.root_wire_generation.size
                or fresh.root_wire_generation == evidence.root_wire_generation
            )
            and _descriptor_range_digest(
                fresh._anchors.descriptor("wire"),
                0,
                evidence.root_wire_generation.size,
            )
            == evidence.root_wire_content_digest
        )
    except BaseException as error:
        fresh.close()
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        _raise("session_changed")
    if not stable:
        fresh.close()
        _raise("session_changed")
    return fresh


__all__ = [
    "DirectoryIdentity",
    "FileGeneration",
    "FileIdentity",
    "KIMI_IDENTITY_SCHEMA_VERSION",
    "KimiIdentityError",
    "KimiIdentityEvidence",
    "ProjectSource",
    "capture_kimi_identity",
    "read_kimi_index_metadata",
    "revalidate_kimi_identity",
    "revalidate_kimi_identity_after_kimi_start",
]
