"""DeepSeek DSH projection-cache and compressed transcript adapter."""

from __future__ import annotations

import json
import os
import selectors
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from sidecar.adapters.base import (
    Adapter,
    ContentBlockEvent,
    as_mapping,
    compact_json,
    content_block_events,
    local_timestamp,
    read_json_object,
    snip,
    text_content,
)
from sidecar.model import Event, Session, Status

_ACTIVE_WINDOW_S = 120.0
_INDEX_BYTES = 8 * 1024 * 1024
_REPLAY_BYTES = 256 * 1024 * 1024
_REPLAY_RETAINED_BYTES = 8 * 1024 * 1024
_REPLAY_RECORDS = 1024
_REPLAY_LINE_BYTES = 1024 * 1024
_REPLAY_TIMEOUT_S = 10.0
_DISCOVERY_MAX_ENTRIES = 4096
_DISCOVERY_MAX_CANDIDATES = 256
_DISCOVERY_COMPRESSED_BYTES = 64 * 1024 * 1024
_DISCOVERY_EXECUTABLE_BYTES = 64 * 1024 * 1024
_DISCOVERY_HEADER_BYTES = 64 * 1024
# Allow a loaded-machine cold decoder launch to finish instead of briefly
# hiding an active session. The shared five-second discovery deadline still
# prevents multiple bad candidates from extending the scan linearly.
_DISCOVERY_CANDIDATE_TIMEOUT_S = 3.5
_DISCOVERY_TIMEOUT_S = 5.0
_DISCOVERY_HEADER_CACHE_ENTRIES = 512
_SESSION_FORMAT_VERSION = 0


def _utf16_units(value: str) -> Iterable[int]:
    encoded = value.encode("utf-16-be", "surrogatepass")
    for (unit,) in struct.iter_unpack(">H", encoded):
        yield unit


def _encode_segment(value: str) -> str:
    """Match DSH's UTF-16 path-segment encoding."""

    if not value:
        raise ValueError("cannot encode an empty path segment")
    if value == ".":
        return "~002E"
    if value == "..":
        return "~002E~002E"
    output = []
    for unit in _utf16_units(value):
        char = chr(unit)
        if char != "~" and (
            48 <= unit <= 57
            or 65 <= unit <= 90
            or 97 <= unit <= 122
            or char in "._-"
        ):
            output.append(char)
        else:
            output.append("~{:04X}".format(unit))
    return "".join(output)


def _project_key(cwd: str) -> str:
    """Match DSH's readable, lossy cwd directory key."""

    if not cwd:
        raise ValueError("cannot encode an empty project path")
    output: List[str] = []
    separator_run = False
    for unit in _utf16_units(cwd):
        char = chr(unit)
        if char in "/\\:":
            if not separator_run:
                output.append("-")
            separator_run = True
        elif (
            char != "~"
            and (
                48 <= unit <= 57
                or 65 <= unit <= 90
                or 97 <= unit <= 122
                or char in "._-"
            )
        ):
            output.append(char)
            separator_run = False
        else:
            output.append("~{:04X}".format(unit))
            separator_run = False
    readable = "".join(output).lstrip("-") or "root"
    return "--{}--".format(readable[:251])


def _transcript_path(dsh_home: Path, cwd: str, session_id: str) -> Path:
    project = _project_key(cwd) if cwd else "_no-cwd"
    return dsh_home / "sessions" / project / _encode_segment(session_id) / "session.jsonl.zstd"


def _configured_dsh_home(home: Path) -> Optional[Path]:
    """Resolve DSH's independent home without treating blanks as cwd."""

    configured = os.environ.get("DSH_HOME")
    if configured is None or not configured.strip():
        return home / ".dsh"
    if "\x00" in configured:
        return None
    candidate = Path(configured)
    if not candidate.is_absolute():
        return None
    return Path(os.path.normpath(str(candidate)))


_FileIdentity = Tuple[int, int, int, int, int]
_ExecutableIdentity = Tuple[int, int, int, int, int, int, int]


class _DecodeOutcome(Enum):
    SUCCESS = "success"
    DETERMINISTIC_INVALID = "deterministic_invalid"
    TRANSIENT_FAILURE = "transient_failure"


@dataclass(frozen=True)
class _HeaderDecode:
    outcome: _DecodeOutcome
    header: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class _ResolvedExecutable:
    path: Path
    identity: _ExecutableIdentity


@dataclass
class _BoundExecutableState:
    descriptor: int
    snapshot: Optional[tempfile.TemporaryDirectory]
    lock: threading.Lock


def _close_bound_executable_state(state: _BoundExecutableState) -> None:
    """Idempotently release the platform-specific bound executable resource."""

    with state.lock:
        descriptor = state.descriptor
        snapshot = state.snapshot
        state.descriptor = -1
        state.snapshot = None
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if snapshot is not None:
        try:
            snapshot.cleanup()
        except OSError:
            pass


class _BoundExecutable:
    """One fixed-identity decoder binding shared for an adapter's lifetime."""

    def __init__(
        self,
        executable: _ResolvedExecutable,
        descriptor: int,
        launch_path: str,
        inherited_fds: Tuple[int, ...],
        snapshot: Optional[tempfile.TemporaryDirectory] = None,
        environment: Optional[Dict[str, str]] = None,
    ) -> None:
        self.path = executable.path
        self.identity = executable.identity
        self.launch_path = launch_path
        self._inherited_fds = inherited_fds
        self.environment = environment
        self._state = _BoundExecutableState(
            descriptor,
            snapshot,
            threading.Lock(),
        )
        self._finalizer = weakref.finalize(
            self,
            _close_bound_executable_state,
            self._state,
        )

    @property
    def descriptor(self) -> int:
        with self._state.lock:
            return self._state.descriptor

    @property
    def inherited_fds(self) -> Tuple[int, ...]:
        with self._state.lock:
            if self._state.descriptor < 0 and self._inherited_fds:
                return ()
            return self._inherited_fds

    @property
    def closed(self) -> bool:
        with self._state.lock:
            return self._state.descriptor < 0 and self._state.snapshot is None

    def spawn_decoder(self, source_fd: int) -> subprocess.Popen:
        """Spawn while holding the resource lock against concurrent close."""

        with self._state.lock:
            if self._state.descriptor < 0 and self._state.snapshot is None:
                raise OSError("bound decoder is closed")
            return subprocess.Popen(
                [str(self.path), "-dc"],
                executable=self.launch_path,
                pass_fds=self._inherited_fds,
                stdin=source_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                env=self.environment,
            )

    def close(self) -> None:
        """Release the descriptor/snapshot once; safe to call repeatedly."""

        self._finalizer()


@dataclass(frozen=True)
class _DurableCandidate:
    project_name: str
    directory_name: str
    session_id: str
    transcript: Path
    identity: _FileIdentity


def _stat_ns(details: os.stat_result, name: str, fallback: float) -> int:
    return int(getattr(details, name, int(fallback * 1_000_000_000)))


def _file_identity(details: os.stat_result) -> _FileIdentity:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_size),
        _stat_ns(details, "st_mtime_ns", details.st_mtime),
        _stat_ns(details, "st_ctime_ns", details.st_ctime),
    )


def _executable_identity(details: os.stat_result) -> _ExecutableIdentity:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_uid),
        int(details.st_mode),
        int(details.st_size),
        _stat_ns(details, "st_mtime_ns", details.st_mtime),
        _stat_ns(details, "st_ctime_ns", details.st_ctime),
    )


def _acceptable_executable(path: Path) -> Optional[_ResolvedExecutable]:
    try:
        details = path.lstat()
    except OSError:
        return None
    geteuid = getattr(os, "geteuid", None)
    effective_uid = geteuid() if callable(geteuid) else details.st_uid
    mode = stat.S_IMODE(details.st_mode)
    if (
        not path.is_absolute()
        or stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_size <= 0
        or details.st_size > _DISCOVERY_EXECUTABLE_BYTES
        or details.st_uid not in (0, effective_uid)
        or bool(mode & 0o022)
        or not bool(mode & 0o111)
        or not os.access(str(path), os.X_OK)
    ):
        return None
    return _ResolvedExecutable(path, _executable_identity(details))


def _resolve_discovery_zstd(value: str) -> Optional[_ResolvedExecutable]:
    """Resolve one stable decoder path without retaining future PATH behavior."""

    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    separators = (os.path.sep,) + ((os.path.altsep,) if os.path.altsep else ())
    if any(separator in value for separator in separators):
        candidate = Path(value)
        if not candidate.is_absolute():
            return None
        return _acceptable_executable(Path(os.path.normpath(str(candidate))))
    resolved = shutil.which(value)
    if not resolved:
        return None
    try:
        candidate = Path(resolved)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return _acceptable_executable(candidate)


def _open_executable(executable: _ResolvedExecutable) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        return -1
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(str(executable.path), flags)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or _executable_identity(details) != executable.identity
        ):
            os.close(descriptor)
            return -1
        return descriptor
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        return -1


def _write_all(descriptor: int, chunk: bytes) -> None:
    offset = 0
    while offset < len(chunk):
        written = os.write(descriptor, chunk[offset:])
        if written <= 0:
            raise OSError("short executable snapshot write")
        offset += written


def _snapshot_executable(
    executable: _ResolvedExecutable,
    source_fd: int,
) -> Tuple[Optional[tempfile.TemporaryDirectory], Optional[str]]:
    snapshot: Optional[tempfile.TemporaryDirectory] = None
    destination_fd = -1
    try:
        snapshot = tempfile.TemporaryDirectory(prefix="agent-sidecar-dsh-")
        os.chmod(snapshot.name, 0o700)
        directory_details = Path(snapshot.name).lstat()
        geteuid = getattr(os, "geteuid", None)
        effective_uid = (
            geteuid() if callable(geteuid) else directory_details.st_uid
        )
        if (
            not stat.S_ISDIR(directory_details.st_mode)
            or directory_details.st_uid != effective_uid
            or bool(stat.S_IMODE(directory_details.st_mode) & 0o077)
        ):
            raise OSError("unsafe executable snapshot directory")
        snapshot_path = Path(snapshot.name) / "decoder"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        destination_fd = os.open(str(snapshot_path), flags, 0o700)
        os.lseek(source_fd, 0, os.SEEK_SET)
        copied = 0
        while copied < executable.identity[4]:
            chunk = os.read(
                source_fd,
                min(1024 * 1024, executable.identity[4] - copied),
            )
            if not chunk:
                raise OSError("short executable snapshot read")
            _write_all(destination_fd, chunk)
            copied += len(chunk)
        if os.read(source_fd, 1):
            raise OSError("executable grew while snapshotting")
        if _executable_identity(os.fstat(source_fd)) != executable.identity:
            raise OSError("executable changed while snapshotting")
        os.fsync(destination_fd)
        os.close(destination_fd)
        destination_fd = -1
        os.chmod(snapshot_path, 0o500)
        details = snapshot_path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size != executable.identity[4]
            or bool(stat.S_IMODE(details.st_mode) & 0o077)
        ):
            raise OSError("unsafe executable snapshot")
        return snapshot, str(snapshot_path)
    except OSError:
        if destination_fd >= 0:
            os.close(destination_fd)
        if snapshot is not None:
            snapshot.cleanup()
        return None, None


def _darwin_snapshot_environment(executable: _ResolvedExecutable) -> Dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DYLD_")
    }
    library_path = executable.path.parent.parent / "lib"
    try:
        details = library_path.lstat()
    except OSError:
        return environment
    geteuid = getattr(os, "geteuid", None)
    effective_uid = geteuid() if callable(geteuid) else details.st_uid
    if (
        not library_path.is_absolute()
        or stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid not in (0, effective_uid)
        or bool(stat.S_IMODE(details.st_mode) & 0o022)
    ):
        return environment
    environment["DYLD_LIBRARY_PATH"] = str(library_path)
    return environment


def _bind_executable(executable: _ResolvedExecutable) -> Optional[_BoundExecutable]:
    descriptor = _open_executable(executable)
    if descriptor < 0:
        return None
    snapshot: Optional[tempfile.TemporaryDirectory] = None
    try:
        if sys.platform.startswith("linux"):
            bound = _BoundExecutable(
                executable,
                descriptor,
                "/dev/fd/{}".format(descriptor),
                (descriptor,),
            )
            descriptor = -1
            return bound
        if sys.platform == "darwin":
            snapshot, launch_path = _snapshot_executable(executable, descriptor)
            if snapshot is not None and launch_path is not None:
                environment = _darwin_snapshot_environment(executable)
                os.close(descriptor)
                descriptor = -1
                bound = _BoundExecutable(
                    executable,
                    -1,
                    launch_path,
                    (),
                    snapshot,
                    environment,
                )
                snapshot = None
                return bound
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if snapshot is not None:
            try:
                snapshot.cleanup()
            except OSError:
                pass


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        return 0
    return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)


def _open_directory(path: Path) -> int:
    flags = _directory_flags()
    if not flags:
        return -1
    try:
        descriptor = os.open(str(path), flags)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return -1
        return descriptor
    except OSError:
        return -1


def _open_directory_at(parent_fd: int, name: str) -> int:
    flags = _directory_flags()
    if not flags:
        return -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return -1
        return descriptor
    except OSError:
        return -1


def _open_regular_at(parent_fd: int, name: str) -> Tuple[int, Optional[os.stat_result]]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        return -1, None
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size <= 0
            or details.st_size > _DISCOVERY_COMPRESSED_BYTES
        ):
            os.close(descriptor)
            return -1, None
        return descriptor, details
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        return -1, None


def _decode_segment(value: str) -> Optional[str]:
    """Decode one canonical DSH UTF-16 path segment."""

    if not value or value in (".", ".."):
        return None
    units: List[int] = []
    index = 0
    hexdigits = "0123456789ABCDEF"
    while index < len(value):
        char = value[index]
        if char == "~":
            encoded = value[index + 1 : index + 5]
            if len(encoded) != 4 or any(item not in hexdigits for item in encoded):
                return None
            units.append(int(encoded, 16))
            index += 5
            continue
        if not (
            char.isascii()
            and (char.isalnum() or char in "._-")
        ):
            return None
        units.append(ord(char))
        index += 1
    try:
        raw = b"".join(struct.pack(">H", unit) for unit in units)
        decoded = raw.decode("utf-16-be", "surrogatepass")
        if not decoded or _encode_segment(decoded) != value:
            return None
        return decoded
    except (UnicodeError, ValueError):
        return None


def _scan_directory_names(
    descriptor: int,
    budget: int,
    deadline: float,
) -> Tuple[List[str], int, bool]:
    """Collect real child-directory names and prove the entry scan completed."""

    names: List[str] = []
    consumed = 0
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if time.monotonic() >= deadline or consumed >= budget:
                    return names, consumed, False
                consumed += 1
                try:
                    if entry.is_dir(follow_symlinks=False):
                        names.append(entry.name)
                except OSError:
                    continue
    except OSError:
        return names, consumed, False
    return sorted(names), consumed, time.monotonic() < deadline


def _enumerate_durable_candidates(
    sessions_path: Path,
    sessions_fd: int,
    deadline: float,
) -> Tuple[List[_DurableCandidate], Set[str], bool]:
    remaining = _DISCOVERY_MAX_ENTRIES
    projects, consumed, complete = _scan_directory_names(
        sessions_fd,
        remaining,
        deadline,
    )
    remaining -= consumed
    if not complete:
        return [], set(), False

    candidates: List[_DurableCandidate] = []
    seen_ids: Set[str] = set()
    ambiguous_ids: Set[str] = set()
    for project_name in projects:
        if time.monotonic() >= deadline:
            return [], set(), False
        project_fd = _open_directory_at(sessions_fd, project_name)
        if project_fd < 0:
            continue
        try:
            directories, consumed, complete = _scan_directory_names(
                project_fd,
                remaining,
                deadline,
            )
            remaining -= consumed
            if not complete:
                return [], set(), False
            for directory_name in directories:
                if time.monotonic() >= deadline:
                    return [], set(), False
                session_id = _decode_segment(directory_name)
                if session_id is None:
                    continue
                directory_fd = _open_directory_at(project_fd, directory_name)
                if directory_fd < 0:
                    continue
                try:
                    transcript_fd, details = _open_regular_at(
                        directory_fd,
                        "session.jsonl.zstd",
                    )
                    if transcript_fd < 0 or details is None:
                        continue
                    os.close(transcript_fd)
                finally:
                    os.close(directory_fd)
                if session_id in seen_ids:
                    ambiguous_ids.add(session_id)
                else:
                    seen_ids.add(session_id)
                candidates.append(
                    _DurableCandidate(
                        project_name,
                        directory_name,
                        session_id,
                        sessions_path
                        / project_name
                        / directory_name
                        / "session.jsonl.zstd",
                        _file_identity(details),
                    )
                )
        finally:
            os.close(project_fd)
    return candidates, ambiguous_ids, True


def _open_candidate(
    sessions_fd: int,
    candidate: _DurableCandidate,
) -> Tuple[int, Optional[os.stat_result]]:
    project_fd = _open_directory_at(sessions_fd, candidate.project_name)
    if project_fd < 0:
        return -1, None
    try:
        directory_fd = _open_directory_at(project_fd, candidate.directory_name)
        if directory_fd < 0:
            return -1, None
        try:
            descriptor, details = _open_regular_at(
                directory_fd,
                "session.jsonl.zstd",
            )
        finally:
            os.close(directory_fd)
    finally:
        os.close(project_fd)
    if (
        descriptor < 0
        or details is None
        or _file_identity(details) != candidate.identity
    ):
        if descriptor >= 0:
            os.close(descriptor)
        return -1, None
    return descriptor, details


def _read_durable_header(
    source_fd: int,
    executable: _BoundExecutable,
    timeout: float,
) -> _HeaderDecode:
    """Decode only the bounded first logical line from one bound file."""

    if timeout <= 0:
        return _HeaderDecode(_DecodeOutcome.TRANSIENT_FAILURE)
    deadline = time.monotonic() + timeout
    # Preserve a bounded reaping window even when decoding consumes its budget.
    # This avoids leaking a timed-out child while retaining at least half of
    # very small synthetic budgets and nearly all real cold-launch budgets.
    cleanup_reserve = min(0.1, max(0.05, timeout * 0.03), timeout * 0.5)
    drain_deadline = deadline - cleanup_reserve
    if time.monotonic() >= drain_deadline:
        return _HeaderDecode(_DecodeOutcome.TRANSIENT_FAILURE)
    try:
        process = executable.spawn_decoder(source_fd)
    except OSError:
        return _HeaderDecode(_DecodeOutcome.TRANSIENT_FAILURE)
    if process.stdout is None:
        _stop_process(process, deadline)
        return _HeaderDecode(_DecodeOutcome.TRANSIENT_FAILURE)

    raw = bytearray()
    complete = False
    eof = False
    failed = False
    decoded_header: Optional[Dict[str, Any]] = None
    invalid_complete = False
    selector: Optional[selectors.BaseSelector] = None
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        while len(raw) < _DISCOVERY_HEADER_BYTES:
            remaining = drain_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready = selector.select(min(0.05, remaining))
            except (OSError, ValueError):
                failed = True
                break
            if not ready:
                # A child can exit before its pipe buffer is observed. Keep
                # draining until EOF, a complete line, or the deadline.
                continue
            try:
                chunk = os.read(
                    process.stdout.fileno(),
                    min(8192, _DISCOVERY_HEADER_BYTES - len(raw)),
                )
            except OSError:
                failed = True
                break
            if not chunk:
                eof = True
                break
            newline = chunk.find(b"\n")
            if newline >= 0:
                raw.extend(chunk[:newline])
                complete = True
                break
            raw.extend(chunk)
        if complete:
            try:
                loaded = json.loads(bytes(raw).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeError):
                invalid_complete = True
            else:
                if isinstance(loaded, dict):
                    decoded_header = loaded
                else:
                    invalid_complete = True
        discarded = 0
        while (
            invalid_complete
            and process.poll() is None
            and discarded < _DISCOVERY_HEADER_BYTES
        ):
            remaining = drain_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready = selector.select(min(0.05, remaining))
            except (OSError, ValueError):
                failed = True
                break
            if not ready:
                continue
            try:
                chunk = os.read(
                    process.stdout.fileno(),
                    min(8192, _DISCOVERY_HEADER_BYTES - discarded),
                )
            except OSError:
                failed = True
                break
            if not chunk:
                eof = True
                break
            discarded += len(chunk)
    except (OSError, ValueError):
        failed = True
    finally:
        if selector is not None:
            try:
                selector.close()
            except OSError:
                failed = True
        try:
            process.stdout.close()
        except OSError:
            failed = True
        returncode = process.poll()
        if eof and returncode is None:
            try:
                returncode = process.wait(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            except subprocess.TimeoutExpired:
                failed = True
        stopped_returncode, stopped = _stop_process(process, deadline)
        if returncode is None:
            returncode = stopped_returncode
    if failed:
        return _HeaderDecode(_DecodeOutcome.TRANSIENT_FAILURE)
    if complete:
        if returncode not in (None, 0) and not stopped:
            return _HeaderDecode(_DecodeOutcome.TRANSIENT_FAILURE)
        if invalid_complete:
            if stopped or returncode != 0:
                return _HeaderDecode(_DecodeOutcome.TRANSIENT_FAILURE)
            return _HeaderDecode(_DecodeOutcome.DETERMINISTIC_INVALID)
        return _HeaderDecode(_DecodeOutcome.SUCCESS, decoded_header)
    if returncode not in (None, 0) and not stopped:
        return _HeaderDecode(_DecodeOutcome.TRANSIENT_FAILURE)
    if not complete:
        if not eof and len(raw) < _DISCOVERY_HEADER_BYTES:
            return _HeaderDecode(_DecodeOutcome.TRANSIENT_FAILURE)
        return _HeaderDecode(_DecodeOutcome.DETERMINISTIC_INVALID)


def _durable_session(
    candidate: _DurableCandidate,
    header: Mapping[str, Any],
    updated_at: float,
) -> Optional[Session]:
    """Validate one durable header and conservatively normalize a top-level session."""

    session_id = header.get("id")
    created_at = header.get("createdAt")
    delegation_depth = header.get("delegationDepth")
    version = header.get("version")
    if (
        header.get("type") != "session"
        or not isinstance(session_id, str)
        or not session_id
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version != _SESSION_FORMAT_VERSION
        or not isinstance(created_at, int)
        or isinstance(created_at, bool)
        or created_at < 0
        or created_at > (1 << 53) - 1
        or not isinstance(delegation_depth, int)
        or isinstance(delegation_depth, bool)
        or delegation_depth != 0
        or "parentSession" in header
        or "origin" in header
        or "seedLength" in header
    ):
        return None

    raw_cwd = header.get("cwd")
    if (
        not isinstance(raw_cwd, str)
        or not raw_cwd
        or "\x00" in raw_cwd
        or not os.path.isabs(raw_cwd)
    ):
        return None
    agent_preset = header.get("agentPreset")
    if "agentPreset" in header and not isinstance(agent_preset, str):
        return None
    cwd = raw_cwd
    try:
        project_name = _project_key(cwd)
        directory_name = _encode_segment(session_id)
    except ValueError:
        return None
    if (
        session_id != candidate.session_id
        or project_name != candidate.project_name
        or directory_name != candidate.directory_name
    ):
        return None

    created_epoch = float(created_at) / 1000.0
    session = Session(
        agent="dsh",
        session_id=session_id,
        project=cwd,
        transcript=str(candidate.transcript),
        updated_at=updated_at or created_epoch,
        title=snip(Path(cwd).name or cwd or "DSH session", 160),
        extra={
            "source": "session_store",
            "created_at": created_at,
            "transcript_exists": True,
            "replay_available": True,
            "durable_only": True,
            "open_evidence": False,
        },
    )
    session.status = Status.IDLE
    return session


def _discover_durable_sessions(
    dsh_home: Path,
    executable: Optional[_BoundExecutable],
    cached_ids: Set[str],
    header_cache: "OrderedDict[_FileIdentity, _HeaderDecode]",
) -> List[Session]:
    """Boundedly find cache-missing sessions in DSH's durable session store."""

    if executable is None or executable.closed:
        return []
    deadline = time.monotonic() + _DISCOVERY_TIMEOUT_S
    sessions_path = dsh_home / "sessions"
    sessions_fd = _open_directory(sessions_path)
    if sessions_fd < 0:
        return []
    try:
        candidates, ambiguous, complete = _enumerate_durable_candidates(
            sessions_path,
            sessions_fd,
            deadline,
        )
        if not complete:
            return []
        selected = [
            candidate
            for candidate in candidates
            if candidate.session_id not in cached_ids
            and candidate.session_id not in ambiguous
        ][:_DISCOVERY_MAX_CANDIDATES]
        found: Dict[str, Session] = {}
        for candidate in selected:
            if time.monotonic() >= deadline:
                break
            source_fd, details = _open_candidate(sessions_fd, candidate)
            if source_fd < 0 or details is None:
                continue
            try:
                try:
                    decoded = header_cache.pop(candidate.identity)
                except KeyError:
                    remaining = min(
                        _DISCOVERY_CANDIDATE_TIMEOUT_S,
                        deadline - time.monotonic(),
                    )
                    decoded = _read_durable_header(
                        source_fd,
                        executable,
                        remaining,
                    )
                    for identity in list(header_cache):
                        if (
                            identity[:2] == candidate.identity[:2]
                            and identity != candidate.identity
                        ):
                            del header_cache[identity]
                    if decoded.outcome is not _DecodeOutcome.TRANSIENT_FAILURE:
                        header_cache[candidate.identity] = decoded
                        while len(header_cache) > _DISCOVERY_HEADER_CACHE_ENTRIES:
                            header_cache.popitem(last=False)
                else:
                    header_cache[candidate.identity] = decoded
            finally:
                os.close(source_fd)
            header = decoded.header or {}
            session = _durable_session(
                candidate,
                header,
                details.st_mtime,
            )
            if session is not None:
                found[session.session_id] = session
    finally:
        os.close(sessions_fd)
    return sorted(
        found.values(),
        key=lambda session: (-session.updated_at, session.session_id),
    )


def _stop_process(
    process: subprocess.Popen,
    deadline: Optional[float] = None,
) -> Tuple[Optional[int], bool]:
    returncode = process.poll()
    if returncode is not None:
        return returncode, False
    stopped = True
    try:
        process.terminate()
    except OSError:
        return process.poll(), stopped
    wait_timeout = 0.2
    if deadline is not None:
        wait_timeout = max(
            0.0,
            min(wait_timeout, (deadline - time.monotonic()) / 2.0),
        )
    try:
        returncode = process.wait(timeout=wait_timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            return process.poll(), stopped
        wait_timeout = 0.2
        if deadline is not None:
            wait_timeout = max(0.0, min(wait_timeout, deadline - time.monotonic()))
        try:
            returncode = process.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired:
            returncode = process.poll()
    return returncode, stopped


class ReplayPage(List[Dict[str, Any]]):
    """One bounded replay page plus an explicit end-of-transcript signal.

    ``exhausted`` is ``True`` only when the scan truly consumed the
    retrievable transcript: it reached the end of the decoded stream, or a
    degraded source (missing file, missing ``zstd``, failed decoder start)
    had nothing retrievable at all. It is ``False`` when the page stopped
    early on the record, retained-byte, scan-byte, or time budget, so
    callers know more retained records may remain past this page.
    """

    __slots__ = ("exhausted",)

    def __init__(
        self,
        records: Iterable[Dict[str, Any]] = (),
        exhausted: bool = True,
    ) -> None:
        super().__init__(records)
        self.exhausted = bool(exhausted)


def replay_dsh_events(
    path: Path,
    after_seq: Optional[int] = None,
    max_output_bytes: int = _REPLAY_BYTES,
    max_records: int = _REPLAY_RECORDS,
    timeout: float = _REPLAY_TIMEOUT_S,
    zstd_binary: str = "zstd",
    max_retained_bytes: int = _REPLAY_RETAINED_BYTES,
) -> ReplayPage:
    """Stream a bounded zstd replay for scanners and seq-based watchers.

    ``max_output_bytes`` bounds the total decompressed stream scan, while
    ``max_retained_bytes`` and ``max_records`` independently bound memory used
    by matching records. Missing ``zstd``, incomplete live frames, malformed
    rows, and timeouts all degrade to the complete records decoded so far.

    The returned :class:`ReplayPage` reports ``exhausted=False`` whenever one
    of those budgets ended the page before the true end of the stream, so
    paging callers can tell "this is everything retained" apart from "there
    may be more after this page's last ``seq``".
    """

    if (
        max_output_bytes <= 0
        or max_retained_bytes <= 0
        or max_records <= 0
        or timeout <= 0
    ):
        return ReplayPage()
    try:
        if not path.is_file():
            return ReplayPage()
    except OSError:
        return ReplayPage()
    if os.path.sep not in zstd_binary and shutil.which(zstd_binary) is None:
        return ReplayPage()

    try:
        process = subprocess.Popen(
            [zstd_binary, "-dc", "--", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError:
        return ReplayPage()
    if process.stdout is None:
        _stop_process(process)
        return ReplayPage()

    records: List[Dict[str, Any]] = []
    pending = bytearray()
    dropping_line = False
    retained = 0
    scanned = 0
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()

    def consume(raw: bytes) -> bool:
        nonlocal retained
        if not raw.strip() or len(raw) > _REPLAY_LINE_BYTES:
            return False
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeError):
            return False
        if not isinstance(record, dict):
            return False
        seq = record.get("seq")
        if after_seq is not None:
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= after_seq:
                return False
        if len(raw) > max_retained_bytes - retained:
            # Return an already useful page; if this one record is too large,
            # skip it and continue looking for a smaller later record.
            return bool(records)
        records.append(record)
        retained += len(raw)
        return len(records) >= max_records or retained >= max_retained_bytes

    def consume_chunk(chunk: bytes) -> bool:
        nonlocal dropping_line
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            if newline < 0:
                segment = chunk[start:]
                if not dropping_line:
                    if len(pending) + len(segment) <= _REPLAY_LINE_BYTES:
                        pending.extend(segment)
                    else:
                        pending.clear()
                        dropping_line = True
                return False

            segment = chunk[start:newline]
            start = newline + 1
            if dropping_line:
                dropping_line = False
                pending.clear()
                continue
            if len(pending) + len(segment) > _REPLAY_LINE_BYTES:
                pending.clear()
                continue
            raw = bytes(pending) + segment
            pending.clear()
            if consume(raw):
                return True
        return False

    stop = False
    eof = False
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while not stop and scanned < max_output_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready = selector.select(min(0.1, remaining))
            if not ready:
                if process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(
                    process.stdout.fileno(),
                    min(64 * 1024, max_output_bytes - scanned),
                )
            except OSError:
                break
            if not chunk:
                eof = True
                break
            scanned += len(chunk)
            if consume_chunk(chunk):
                stop = True
        if eof and pending and not dropping_line and not stop:
            stop = consume(bytes(pending))
    finally:
        selector.close()
        process.stdout.close()
        _stop_process(process)
    # Reaching end-of-stream without a budget stop is the only proof that no
    # retained record was left behind; every other exit (record, retained-byte,
    # scan-byte, or time budget) may have stopped before later records.
    return ReplayPage(records, exhausted=eof and not stop)


def _sequence(record: Mapping[str, Any]) -> Optional[int]:
    value = record.get("seq")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _event_extra(record: Mapping[str, Any], **values: Any) -> Dict[str, Any]:
    extra: Dict[str, Any] = {"native_type": str(record.get("type") or "")}
    seq = _sequence(record)
    if seq is not None:
        extra["seq"] = seq
    extra.update({key: value for key, value in values.items() if value is not None})
    return extra


def _render_content_block(
    item: Mapping[str, Any],
    default_kind: str,
) -> Optional[ContentBlockEvent]:
    item_type = str(item.get("type") or "")
    if item_type == "text":
        return default_kind, item.get("text"), 120
    if item_type == "reasoning":
        return "thinking", item.get("text"), 80
    if item_type == "tool-call":
        return (
            "tool_call",
            "{} {}".format(item.get("name") or "tool", item.get("arguments") or ""),
            120,
        )
    if item_type == "tool-result":
        return "tool_result", text_content(item.get("content")), 80
    return None


class DSHAdapter(Adapter):
    name = "dsh"
    agent_names = ("dsh",)

    def __init__(self, discovery_zstd_binary: str = "zstd") -> None:
        self._seen_seq: Dict[str, int] = {}
        self._discovery_lock = threading.RLock()
        try:
            self._discovery_zstd = _resolve_discovery_zstd(
                discovery_zstd_binary
            )
        except (OSError, RuntimeError, ValueError):
            self._discovery_zstd = None
        self._discovery_decoder: Optional[_BoundExecutable] = None
        if self._discovery_zstd is not None:
            try:
                self._discovery_decoder = _bind_executable(self._discovery_zstd)
            except (OSError, RuntimeError):
                # Projection-cache discovery remains available if a trusted
                # decoder cannot be bound during adapter initialization.
                self._discovery_decoder = None
        self._durable_header_cache: "OrderedDict[_FileIdentity, _HeaderDecode]" = (
            OrderedDict()
        )

    def discover(self, home: Path) -> Iterable[Session]:
        with self._discovery_lock:
            return self._discover(home)

    def _discover(self, home: Path) -> List[Session]:
        dsh_home = _configured_dsh_home(home)
        if dsh_home is None:
            return []
        index_path = dsh_home / "storages" / "session_projcache.json"
        index = read_json_object(index_path, _INDEX_BYTES)
        tables = as_mapping(index.get("tables"))
        entries = as_mapping(as_mapping(tables.get("sessions")))
        try:
            index_mtime = index_path.stat().st_mtime
        except OSError:
            index_mtime = 0.0

        sessions: List[Session] = []
        for raw_session_id, raw_entry in entries.items():
            if not isinstance(raw_session_id, str) or not raw_session_id:
                continue
            entry = as_mapping(raw_entry)
            identity = as_mapping(entry.get("identity"))
            cwd = str(identity.get("cwd") or "")
            created_at = identity.get("createdAt")
            created_epoch = (
                float(created_at) / 1000.0
                if isinstance(created_at, (int, float)) and not isinstance(created_at, bool)
                else 0.0
            )
            rows = as_mapping(entry.get("rows"))
            stats_row = as_mapping(rows.get("sessionStats"))
            stats = dict(as_mapping(stats_row.get("val")))
            seq_value = stats_row.get("seq")
            seq = (
                seq_value
                if isinstance(seq_value, int) and not isinstance(seq_value, bool)
                else 0
            )
            previous_seq = self._seen_seq.get(raw_session_id)
            seq_grew = previous_seq is not None and seq > previous_seq
            self._seen_seq[raw_session_id] = seq

            try:
                transcript = _transcript_path(dsh_home, cwd, raw_session_id)
            except ValueError:
                continue
            try:
                transcript_mtime = transcript.stat().st_mtime
            except OSError:
                transcript_mtime = 0.0
            updated_at = transcript_mtime or created_epoch or index_mtime
            if seq_grew:
                updated_at = max(updated_at, index_mtime)

            title_row = as_mapping(rows.get("title"))
            title_value = title_row.get("val")
            title = title_value if isinstance(title_value, str) else ""
            plan = dict(as_mapping(as_mapping(rows.get("plan")).get("val")))
            pending = as_mapping(stats.get("pendingCalls"))
            open_evidence = (
                stats.get("openStep") is not None
                or bool(pending)
                or plan.get("running") is not None
            )
            extra: Dict[str, Any] = {
                "source": "session_projcache",
                "index": str(index_path),
                "created_at": created_at,
                "seq": seq,
                "seq_grew": seq_grew,
                "stats": stats,
                "plan": plan,
                "open_evidence": open_evidence,
                "transcript_exists": transcript_mtime > 0,
                "replay_available": transcript_mtime > 0 and shutil.which("zstd") is not None,
            }
            session = Session(
                agent="dsh",
                session_id=raw_session_id,
                project=cwd,
                transcript=str(transcript),
                updated_at=updated_at,
                title=snip(title or Path(cwd).name or cwd, 160),
                extra=extra,
            )
            session.status = self.infer_status(session) or Status.IDLE
            sessions.append(session)
        cached_ids = {session.session_id for session in sessions}
        sessions.extend(
            session
            for session in _discover_durable_sessions(
                dsh_home,
                self._discovery_decoder,
                cached_ids,
                self._durable_header_cache,
            )
        )
        return sessions

    def close(self) -> None:
        """Explicitly release the adapter-lifetime discovery decoder."""

        with self._discovery_lock:
            if self._discovery_decoder is not None:
                self._discovery_decoder.close()

    def normalize(self, record: Mapping[str, Any], session: Session) -> Iterable[Event]:
        native_type = str(record.get("type") or "")
        data = as_mapping(record.get("data"))
        timestamp = local_timestamp(
            record.get("createdAt") if native_type == "session" else record.get("time"),
            fallback=session.updated_at,
        )

        if native_type == "session":
            text = snip(record.get("cwd") or record.get("id") or "session")
            return [
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    "session",
                    text,
                    _event_extra(
                        record,
                        version=record.get("version"),
                        parent_id=record.get("parentSession"),
                    ),
                )
            ]
        if native_type in ("turn/start", "turn/end", "step/start", "step/end"):
            kind = native_type.replace("/", "_")
            number = data.get("turn") if native_type.startswith("turn/") else data.get("step")
            reason = as_mapping(data.get("reason")).get("kind")
            text = "{} {}".format(kind, number if number is not None else "").strip()
            if reason:
                text = "{}: {}".format(text, reason)
            return [
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    kind,
                    text,
                    _event_extra(record, turn=data.get("turn"), step=data.get("step")),
                )
            ]
        if native_type == "agent/inbox/spliced":
            events: List[Event] = []
            inserted = data.get("inserted")
            if isinstance(inserted, Sequence) and not isinstance(
                inserted, (str, bytes, bytearray)
            ):
                for message in inserted:
                    if isinstance(message, Mapping):
                        events.extend(
                            content_block_events(
                                message.get("content"),
                                session,
                                timestamp,
                                "user",
                                _event_extra(record),
                                _render_content_block,
                            )
                        )
            return events
        if native_type in ("user/message", "assistant/message"):
            message = (
                data
                if native_type == "user/message"
                else as_mapping(data.get("message"))
            )
            default_kind = "user" if native_type == "user/message" else "assistant"
            return content_block_events(
                message.get("content"),
                session,
                timestamp,
                default_kind,
                _event_extra(record),
                _render_content_block,
            )
        if native_type == "tool/call":
            name = str(data.get("name") or "tool")
            arguments = data.get("arguments")
            if not isinstance(arguments, str):
                arguments = compact_json(arguments or {})
            return [
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    "tool_call",
                    snip("{} {}".format(name, arguments), 120),
                    _event_extra(
                        record,
                        call_id=data.get("callId"),
                        turn=data.get("turn"),
                        step=data.get("step"),
                    ),
                )
            ]
        if native_type == "tool/result":
            message = as_mapping(data.get("message"))
            return [
                Event(
                    timestamp,
                    session.agent,
                    session.session_id,
                    "tool_result",
                    snip(text_content(message.get("content")), 80),
                    _event_extra(
                        record,
                        turn=data.get("turn"),
                        step=data.get("step"),
                        error=data.get("error"),
                    ),
                )
            ]
        return []

    def infer_status(self, session: Session, now: Optional[float] = None) -> Optional[Status]:
        current = time.time() if now is None else now
        if current - session.updated_at >= _ACTIVE_WINDOW_S:
            return Status.IDLE
        if session.extra.get("open_evidence") or session.extra.get("seq_grew"):
            return Status.WORKING
        return Status.WAITING

    def replay(
        self,
        session: Session,
        after_seq: Optional[int] = None,
        max_output_bytes: int = _REPLAY_BYTES,
        max_records: int = _REPLAY_RECORDS,
        timeout: float = _REPLAY_TIMEOUT_S,
        max_retained_bytes: int = _REPLAY_RETAINED_BYTES,
    ) -> ReplayPage:
        if not session.transcript:
            return ReplayPage()
        return replay_dsh_events(
            Path(session.transcript),
            after_seq=after_seq,
            max_output_bytes=max_output_bytes,
            max_records=max_records,
            timeout=timeout,
            max_retained_bytes=max_retained_bytes,
        )


__all__ = ["DSHAdapter", "ReplayPage", "replay_dsh_events"]
