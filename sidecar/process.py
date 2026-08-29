"""Discover running local agent processes without third-party dependencies."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
import time
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple, Union

from sidecar.process_runner import run_bounded


_AGENT_EXECUTABLES = frozenset(
    ("claude", "codex", "cursor-agent", "cursor", "copilot", "dsh", "kimi")
)
_NODE_EXECUTABLES = frozenset(("node", "nodejs"))
_STRICT_PS_STDOUT_LIMIT = 1024 * 1024
_STRICT_INSPECTION_STDERR_LIMIT = 4096
_STRICT_PS_TIMEOUT_SECONDS = 2.0
_STRICT_CWD_TIMEOUT_SECONDS = 1.0
_STRICT_INSPECTION_SECONDS = 2.0
_STRICT_CANDIDATE_LIMIT = 64
_STRICT_CANDIDATE_TOKEN_LIMIT = 4096
_STRICT_TOTAL_CANDIDATE_TOKEN_LIMIT = 16384
_NODE_ATTACHED_PATH_OPTIONS = frozenset(
    (
        "--experimental-loader",
        "--import",
        "--loader",
        "--require",
    )
)
_NODE_CODE_OPTIONS = frozenset(("-e", "-p", "--eval", "--print"))


def _safe_text(value: object) -> str:
    """Return printable Unicode, replacing invalid bytes and surrogates."""

    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value).encode("utf-8", "replace").decode("utf-8")


def _snip(value: object, limit: int = 100) -> str:
    text = " ".join(_safe_text(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\N{HORIZONTAL ELLIPSIS}"


def _executable_basename(command: str) -> str:
    """Extract argv[0]'s basename without interpreting wrapper arguments."""

    token = command.lstrip().split(None, 1)[0] if command.strip() else ""
    token = token.strip("\"'")
    return token.rsplit("/", 1)[-1]


def parse_ps_output(
    ps_text: Union[str, bytes],
    cwd_lookup: Optional[Callable[[int], str]] = None,
) -> List[Dict[str, object]]:
    """Parse ``ps`` output into JSON-compatible agent process records.

    The expected columns are ``pid``, ``etime``, and ``command``. Detection is
    deliberately limited to argv[0]'s basename: shell, ssh, ``env``, and this
    sidecar can mention an agent in their arguments without becoming matches.
    """

    text = _safe_text(ps_text)
    processes: List[Dict[str, object]] = []
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid_text, elapsed, command = parts
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid <= 0:
            continue

        executable = _executable_basename(command)
        if executable not in _AGENT_EXECUTABLES:
            continue

        cwd = ""
        if cwd_lookup is not None:
            try:
                cwd = _safe_text(cwd_lookup(pid) or "")
            except (OSError, subprocess.SubprocessError, ValueError):
                cwd = ""

        record: Dict[str, object] = {
            "pid": pid,
            "etime": _safe_text(elapsed),
            "exe": executable,
            "cmd": _snip(command),
            "cwd": cwd,
        }
        # Guard the public contract if this function is changed later.
        json.dumps(record)
        processes.append(record)
    return processes


def _linux_pid_cwd(pid: int) -> str:
    try:
        return _safe_text(os.readlink("/proc/{}/cwd".format(pid)))
    except (OSError, ValueError):
        return ""


def _macos_pid_cwd(pid: int) -> str:
    try:
        completed = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    for line in _safe_text(completed.stdout or b"").splitlines():
        if line.startswith("n"):
            return _safe_text(line[1:])
    return ""


def _pid_cwd(pid: int) -> str:
    """Return a process cwd where the host exposes one, otherwise ``""``."""

    if sys.platform.startswith("linux"):
        return _linux_pid_cwd(pid)
    if sys.platform == "darwin":
        return _macos_pid_cwd(pid)
    return ""


def running_agent_processes() -> List[Dict[str, object]]:
    """Return live agent process records suitable for the CLI ``ps`` command."""

    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,etime=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return parse_ps_output(completed.stdout or b"", cwd_lookup=_pid_cwd)


class ProcessInspectionError(RuntimeError):
    """A live owner or inspection uncertainty blocks native mutation."""

    code = "session_busy"

    def __init__(self) -> None:
        super().__init__("session_busy")


def _inspection_failed() -> ProcessInspectionError:
    return ProcessInspectionError()


def _inspection_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _inspection_failed()
    return remaining


def _strict_identity(value: object) -> Tuple[int, int]:
    try:
        dev = getattr(value, "dev")
        ino = getattr(value, "ino")
    except (AttributeError, TypeError):
        raise _inspection_failed() from None
    if (
        type(dev) is not int
        or type(ino) is not int
        or dev < 0
        or ino <= 0
    ):
        raise _inspection_failed()
    return dev, ino


def _strict_canonical_path(value: object) -> str:
    try:
        path = getattr(value, "canonical_path")
    except (AttributeError, TypeError):
        raise _inspection_failed() from None
    if not isinstance(path, str) or not path or "\x00" in path:
        raise _inspection_failed()
    return path


def _strict_run(
    argv: List[str],
    *,
    stdout_limit: int,
    timeout: float,
):
    try:
        result = run_bounded(
            argv,
            input_limit=1,
            stdout_limit=stdout_limit,
            stderr_limit=_STRICT_INSPECTION_STDERR_LIMIT,
            timeout=timeout,
            env={
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            },
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        raise _inspection_failed() from None
    if (
        result.returncode != 0
        or result.overflow is not None
        or result.cleanup_incomplete
    ):
        raise _inspection_failed()
    return result


def _strict_process_rows(
    deadline: Optional[float] = None,
) -> List[Tuple[int, int, str, str]]:
    if deadline is None:
        deadline = time.monotonic() + _STRICT_INSPECTION_SECONDS
    result = _strict_run(
        ["ps", "-axo", "pid=,pgid=,comm=,command="],
        stdout_limit=_STRICT_PS_STDOUT_LIMIT,
        timeout=min(_STRICT_PS_TIMEOUT_SECONDS, _inspection_remaining(deadline)),
    )
    _inspection_remaining(deadline)
    text = result.stdout.decode("utf-8", "surrogateescape")

    rows: List[Tuple[int, int, str, str]] = []
    for line in text.splitlines():
        _inspection_remaining(deadline)
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 3)
        if len(parts) < 3 and _has_kimi_indicator(
            _lexical_command_probes(stripped)
        ):
            raise _inspection_failed()
        if len(parts) < 3:
            continue
        pid_text, pgid_text, argv0 = parts[:3]
        command = parts[3] if len(parts) == 4 else argv0
        try:
            pid = int(pid_text)
            pgid = int(pgid_text)
        except ValueError:
            if _has_kimi_indicator(_lexical_command_probes(stripped)):
                raise _inspection_failed() from None
            continue
        invalid_row = pid <= 0 or pgid <= 0 or not argv0 or not command
        if invalid_row and _has_kimi_indicator(
            _lexical_command_probes(stripped)
        ):
            raise _inspection_failed()
        if invalid_row:
            continue
        rows.append((pid, pgid, argv0, command))
    return rows


def _strict_linux_pid_cwd(pid: int) -> str:
    try:
        value = os.readlink("/proc/{}/cwd".format(pid))
    except (OSError, ValueError):
        raise _inspection_failed() from None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise _inspection_failed()
    return value


def _strict_macos_pid_cwd(pid: int) -> str:
    result = _strict_run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        stdout_limit=64 * 1024,
        timeout=_STRICT_CWD_TIMEOUT_SECONDS,
    )
    try:
        lines = result.stdout.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError:
        raise _inspection_failed() from None
    names = [line[1:] for line in lines if line.startswith("n") and len(line) > 1]
    if len(names) != 1 or "\x00" in names[0]:
        raise _inspection_failed()
    return names[0]


def _strict_macos_cwd_identity(
    pid: int,
    deadline: float,
) -> Tuple[int, int, int]:
    result = _strict_run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-F0Din"],
        stdout_limit=64 * 1024,
        timeout=min(_STRICT_CWD_TIMEOUT_SECONDS, _inspection_remaining(deadline)),
    )
    _inspection_remaining(deadline)
    fields = []
    for raw_field in result.stdout.split(b"\0"):
        field = raw_field.lstrip(b"\n")
        if field:
            fields.append(field)
    expected_pid = "p{}".format(pid).encode("ascii")
    if expected_pid not in fields or b"fcwd" not in fields:
        raise _inspection_failed()
    devices = [field[1:] for field in fields if field.startswith(b"D")]
    inodes = [field[1:] for field in fields if field.startswith(b"i")]
    names = [field[1:] for field in fields if field.startswith(b"n")]
    if len(devices) != 1 or len(inodes) != 1 or len(names) != 1:
        raise _inspection_failed()
    try:
        device = int(devices[0], 0)
        inode = int(inodes[0], 10)
        path = names[0].decode("utf-8", "strict")
    except (UnicodeDecodeError, ValueError):
        raise _inspection_failed() from None
    if device < 0 or inode <= 0 or not path or "\x00" in path or "\n" in path:
        raise _inspection_failed()
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
    except (OSError, TypeError, ValueError):
        if descriptor >= 0:
            os.close(descriptor)
        raise _inspection_failed() from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != (device, inode)
    ):
        os.close(descriptor)
        raise _inspection_failed()
    return device, inode, descriptor


def _strict_linux_cwd_identity(pid: int) -> Tuple[int, int, int]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = -1
    try:
        descriptor = os.open("/proc/{}/cwd".format(pid), flags)
        metadata = os.fstat(descriptor)
    except (OSError, TypeError, ValueError):
        if descriptor >= 0:
            os.close(descriptor)
        raise _inspection_failed() from None
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise _inspection_failed()
    return metadata.st_dev, metadata.st_ino, descriptor


def _strict_candidate_cwd(
    pid: int,
    deadline: float,
) -> Tuple[int, int, int]:
    _inspection_remaining(deadline)
    result: Optional[Tuple[int, int, int]] = None
    ownership_transferred = False
    if sys.platform.startswith("linux"):
        result = _strict_linux_cwd_identity(pid)
    elif sys.platform == "darwin":
        result = _strict_macos_cwd_identity(pid, deadline)
    else:
        raise _inspection_failed()
    try:
        _inspection_remaining(deadline)
        ownership_transferred = True
        return result
    finally:
        if result is not None and not ownership_transferred:
            try:
                os.close(result[2])
            except (IndexError, OSError, TypeError):
                pass


def _strict_pid_cwd(pid: int) -> str:
    if sys.platform.startswith("linux"):
        return _strict_linux_pid_cwd(pid)
    if sys.platform == "darwin":
        return _strict_macos_pid_cwd(pid)
    raise _inspection_failed()


def _strict_command_tokens(command: str) -> Tuple[str, ...]:
    try:
        command.encode("utf-8", "strict")
        tokens = tuple(shlex.split(command, posix=True))
    except (UnicodeEncodeError, ValueError):
        raise _inspection_failed() from None
    if not tokens or any("\x00" in token for token in tokens):
        raise _inspection_failed()
    return tokens


def _path_identity(path: str) -> Tuple[int, int]:
    try:
        canonical = os.path.realpath(path)
        metadata = os.stat(canonical)
    except (OSError, TypeError, ValueError):
        raise _inspection_failed() from None
    if not stat.S_ISREG(metadata.st_mode):
        raise _inspection_failed()
    return metadata.st_dev, metadata.st_ino


def _candidate_token_identity(
    token: str,
    cwd_descriptor: int,
) -> Optional[Tuple[int, int]]:
    if not token or token == "--":
        return None
    try:
        if os.path.isabs(token):
            metadata = os.stat(token)
        else:
            metadata = os.stat(token, dir_fd=cwd_descriptor)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, TypeError, ValueError):
        raise _inspection_failed() from None
    if not stat.S_ISREG(metadata.st_mode):
        return None
    return metadata.st_dev, metadata.st_ino


def _token_probes(token: str) -> Tuple[str, ...]:
    probes = (token,)
    if token.startswith("-") and "=" in token:
        probes += (token.split("=", 1)[1],)
    return probes


def _is_exact_kimi_token(token: str) -> bool:
    return os.path.basename(token.rstrip("/")).lower() == "kimi"


def _is_suspicious_kimi_main(token: str) -> bool:
    normalized = token.replace("\\", "/").rstrip("/")
    parts = tuple(part.lower() for part in normalized.split("/") if part)
    return (
        len(parts) >= 2
        and parts[-1] == "main.mjs"
        and "kimi-code" in parts[:-1]
    )


def _has_kimi_indicator(probes: Tuple[str, ...]) -> bool:
    return any(
        _is_exact_kimi_token(probe) or _is_suspicious_kimi_main(probe)
        for probe in probes
    )


def _lexical_command_probes(command: str) -> Tuple[str, ...]:
    probes = []
    token_count = 0
    for raw_token in command.split():
        token_count += 1
        if token_count > _STRICT_CANDIDATE_TOKEN_LIMIT:
            raise _inspection_failed()
        token = raw_token.strip("\"'")
        probes.extend(_token_probes(token))
    return tuple(probes)


class _NodeKimiClassification(Enum):
    ORDINARY = "ordinary"
    DEFINITE = "definite"
    NEEDS_CWD_IDENTITY = "needs_cwd_identity"


def _consume_node_token_budget(
    count: int,
    token_budget: Optional[List[int]],
) -> None:
    if token_budget is None:
        return
    total = token_budget[0] + count
    if total > _STRICT_TOTAL_CANDIDATE_TOKEN_LIMIT:
        raise _inspection_failed()
    token_budget[0] = total


def _node_candidate_tokens(tokens: Tuple[str, ...]) -> Tuple[str, ...]:
    values = []
    skip_code_value = False
    options_ended = False
    for token in tokens[1:]:
        if skip_code_value:
            skip_code_value = False
            continue
        if not options_ended and token == "--":
            options_ended = True
            continue
        if options_ended:
            values.append(token)
            continue
        if token in _NODE_CODE_OPTIONS:
            skip_code_value = True
            continue
        if (
            (token.startswith("-e") or token.startswith("-p"))
            and len(token) > 2
            and not token.startswith("--")
        ):
            continue
        if token.startswith("-"):
            if token.startswith("-r") and len(token) > 2:
                values.append(token[2:])
                continue
            option, separator, value = token.partition("=")
            if not separator:
                continue
            if option not in _NODE_ATTACHED_PATH_OPTIONS:
                continue
            values.extend((value,) if value else ())
            continue
        values.append(token)
    return tuple(values)


def _node_tokens_have_kimi_hint(
    tokens: Tuple[str, ...],
    executable_identity: Tuple[int, int],
    deadline: float,
) -> bool:
    candidates = _node_candidate_tokens(tokens)
    if _has_kimi_indicator(candidates):
        return True
    for candidate in candidates:
        if not os.path.isabs(candidate):
            continue
        _inspection_remaining(deadline)
        try:
            token_identity = _candidate_token_identity(candidate, -1)
        except ProcessInspectionError:
            return True
        _inspection_remaining(deadline)
        if token_identity == executable_identity:
            return True
    return False


def _classify_node_kimi_candidate(
    command: str,
    executable_identity: Tuple[int, int],
    deadline: float,
    token_budget: Optional[List[int]] = None,
) -> Tuple[_NodeKimiClassification, Optional[Tuple[str, ...]]]:
    malformed = False
    try:
        tokens = _strict_command_tokens(command)
    except ProcessInspectionError:
        malformed = True
        tokens = tuple(
            raw_token.strip("\"'") for raw_token in command.split()
        )
    _consume_node_token_budget(len(tokens), token_budget)
    if len(tokens) > _STRICT_CANDIDATE_TOKEN_LIMIT:
        if _node_tokens_have_kimi_hint(
            tokens,
            executable_identity,
            deadline,
        ):
            raise _inspection_failed()
        return _NodeKimiClassification.ORDINARY, None
    candidates = _node_candidate_tokens(tokens)
    if malformed:
        if _has_kimi_indicator(candidates):
            return _NodeKimiClassification.DEFINITE, None
        for candidate in candidates:
            if not os.path.isabs(candidate):
                continue
            _inspection_remaining(deadline)
            try:
                token_identity = _candidate_token_identity(candidate, -1)
            except ProcessInspectionError:
                return _NodeKimiClassification.NEEDS_CWD_IDENTITY, None
            _inspection_remaining(deadline)
            if token_identity == executable_identity:
                return _NodeKimiClassification.DEFINITE, None
        return _NodeKimiClassification.ORDINARY, None
    if _has_kimi_indicator(candidates):
        return _NodeKimiClassification.DEFINITE, None
    relative_tokens = []
    for token in candidates:
        _inspection_remaining(deadline)
        if not os.path.isabs(token):
            relative_tokens.append(token)
            continue
        try:
            token_identity = _candidate_token_identity(token, -1)
        except ProcessInspectionError:
            return _NodeKimiClassification.NEEDS_CWD_IDENTITY, None
        _inspection_remaining(deadline)
        if token_identity == executable_identity:
            return _NodeKimiClassification.DEFINITE, None
    if relative_tokens:
        return (
            _NodeKimiClassification.NEEDS_CWD_IDENTITY,
            tuple(relative_tokens),
        )
    return _NodeKimiClassification.ORDINARY, None


def _relative_candidate_token_identity(
    token: str,
    cwd_descriptor: int,
) -> Optional[Tuple[int, int]]:
    try:
        path_metadata = os.stat(
            token,
            dir_fd=cwd_descriptor,
            follow_symlinks=False,
        )
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        raise _inspection_failed() from None
    is_symlink = stat.S_ISLNK(path_metadata.st_mode)
    if not is_symlink and not stat.S_ISREG(path_metadata.st_mode):
        return None
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    if not is_symlink:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(token, flags, dir_fd=cwd_descriptor)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        raise _inspection_failed() from None
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        raise _inspection_failed() from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    return (
        None
        if not stat.S_ISREG(metadata.st_mode)
        else (metadata.st_dev, metadata.st_ino)
    )


def _strict_executable_binding(
    path: str,
    identity: Tuple[int, int],
) -> Tuple[int, int, int, int, int, int, int]:
    if not os.path.isabs(path):
        raise _inspection_failed()
    try:
        metadata = os.lstat(path)
    except (OSError, TypeError, ValueError):
        raise _inspection_failed() from None
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
        or metadata.st_uid != os.getuid()
        or not mode & stat.S_IXUSR
        or mode & (stat.S_IWGRP | stat.S_IWOTH)
        or metadata.st_nlink != 1
    ):
        raise _inspection_failed()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_ctime_ns,
    )


def _assert_no_live_kimi_in_project(
    project: object,
    executable: object,
    *,
    excluded_process_group: Optional[int] = None,
) -> None:
    """Fail closed if a native Kimi owner may exist in the bound project."""

    project_identity = _strict_identity(project)
    _strict_canonical_path(project)
    executable_identity = _strict_identity(executable)
    _strict_canonical_path(executable)
    if excluded_process_group is not None and (
        type(excluded_process_group) is not int
        or excluded_process_group <= 0
    ):
        raise _inspection_failed()

    deadline = time.monotonic() + _STRICT_INSPECTION_SECONDS
    candidate_count = 0
    token_budget = [0]
    rows = _strict_process_rows(deadline)
    _inspection_remaining(deadline)
    for pid, pgid, ps_argv0, command in rows:
        _inspection_remaining(deadline)
        argv0_name = os.path.basename(ps_argv0).lower()
        command_name = _executable_basename(command).lower()
        is_kimi = argv0_name == "kimi" or command_name == "kimi"
        is_node = (
            argv0_name in _NODE_EXECUTABLES
            or command_name in _NODE_EXECUTABLES
        )
        if not is_kimi and not is_node:
            continue
        if excluded_process_group is not None and pgid == excluded_process_group:
            continue
        classification = _NodeKimiClassification.DEFINITE
        relative_tokens: Optional[Tuple[str, ...]] = None
        if not is_kimi:
            classification, relative_tokens = _classify_node_kimi_candidate(
                command,
                executable_identity,
                deadline,
                token_budget,
            )
            if classification is _NodeKimiClassification.ORDINARY:
                continue

        candidate_count += 1
        if candidate_count > _STRICT_CANDIDATE_LIMIT:
            raise _inspection_failed()
        try:
            cwd_dev, cwd_ino, cwd_descriptor = _strict_candidate_cwd(
                pid,
                deadline,
            )
        except ProcessInspectionError:
            if (
                classification
                is _NodeKimiClassification.NEEDS_CWD_IDENTITY
                and relative_tokens is not None
            ):
                # With a single-link bound executable, an inaccessible cwd
                # cannot conceal a hardlink. A same-UID symlink disguise
                # remains indistinguishable, so ordinary relative Node is
                # deliberately skipped instead of blocking every mutation.
                continue
            raise
        try:
            if (cwd_dev, cwd_ino) != project_identity:
                continue
            if classification is _NodeKimiClassification.DEFINITE:
                raise _inspection_failed()
            if relative_tokens is None:
                raise _inspection_failed()
            for token in relative_tokens:
                _inspection_remaining(deadline)
                token_identity = _relative_candidate_token_identity(
                    token,
                    cwd_descriptor,
                )
                _inspection_remaining(deadline)
                if token_identity == executable_identity:
                    raise _inspection_failed()
        finally:
            try:
                os.close(cwd_descriptor)
            except OSError:
                pass


def assert_no_live_kimi_in_project(
    project: object,
    executable: object,
    *,
    excluded_process_group: Optional[int] = None,
) -> None:
    """Fail closed if a native Kimi owner may exist in the bound project."""

    executable_identity = _strict_identity(executable)
    executable_path = _strict_canonical_path(executable)
    binding = _strict_executable_binding(
        executable_path,
        executable_identity,
    )
    try:
        _assert_no_live_kimi_in_project(
            project,
            executable,
            excluded_process_group=excluded_process_group,
        )
    finally:
        ending_binding = _strict_executable_binding(
            executable_path,
            executable_identity,
        )
        if ending_binding != binding:
            raise _inspection_failed()


__all__ = [
    "ProcessInspectionError",
    "assert_no_live_kimi_in_project",
    "parse_ps_output",
    "running_agent_processes",
]
