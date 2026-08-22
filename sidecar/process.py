"""Discover running local agent processes without third-party dependencies."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Callable, Dict, List, Optional, Union


_AGENT_EXECUTABLES = frozenset(
    ("claude", "codex", "cursor-agent", "cursor", "copilot", "dsh", "kimi")
)


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


__all__ = ["parse_ps_output", "running_agent_processes"]
