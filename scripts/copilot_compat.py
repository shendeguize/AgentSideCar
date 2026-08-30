#!/usr/bin/env python3
"""Run a no-credential structural compatibility smoke for Copilot CLI.

This intentionally invokes only ``--version`` and ``--help``.  It verifies
that the authenticated resume shape used by the plugin is still advertised
without reading, printing, or modifying credentials.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Sequence

MAX_OUTPUT_BYTES = 64 * 1024
MAX_TIMEOUT_SECONDS = 30.0
REQUIRED_FLAGS = ("--resume", "--interactive", "--silent", "--no-color", "--no-auto-update")
VERSION_PATTERN = re.compile(r"\b\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?\b")


def _bounded_command(command: Sequence[str], timeout: float) -> tuple[int, bytes, bytes, str]:
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        return 124, stdout[:MAX_OUTPUT_BYTES], stderr[:MAX_OUTPUT_BYTES], "timeout"
    overflow = len(stdout) > MAX_OUTPUT_BYTES or len(stderr) > MAX_OUTPUT_BYTES
    return (
        int(process.returncode),
        stdout[:MAX_OUTPUT_BYTES],
        stderr[:MAX_OUTPUT_BYTES],
        "output_limit" if overflow else "",
    )


def _version(text: bytes) -> str:
    match = VERSION_PATTERN.search(text.decode("utf-8", "replace"))
    return match.group(0) if match else "unknown"


def run(executable: str, timeout: float) -> Dict[str, object]:
    resolved = shutil.which(executable)
    base = Path(executable).name or "copilot"
    result: Dict[str, object] = {
        "executable": base,
        "available": resolved is not None,
        "version": "unknown",
        "required_flags": list(REQUIRED_FLAGS),
        "flags_present": [],
        "compatible": False,
        "reason": None,
    }
    if resolved is None:
        result["reason"] = "cli_not_found"
        return result
    version_code, version_out, version_err, version_issue = _bounded_command(
        (resolved, "--version"),
        timeout,
    )
    help_code, help_out, help_err, help_issue = _bounded_command(
        (resolved, "--help"),
        timeout,
    )
    result["version"] = _version(version_out + b"\n" + version_err)
    help_text = (help_out + b"\n" + help_err).decode("utf-8", "replace")
    present = [flag for flag in REQUIRED_FLAGS if flag in help_text]
    result["flags_present"] = present
    if version_issue or help_issue:
        result["reason"] = version_issue or help_issue
    elif version_code != 0 or help_code != 0:
        result["reason"] = "cli_probe_failed"
    elif len(present) != len(REQUIRED_FLAGS):
        result["reason"] = "resume_flags_missing"
    else:
        result["compatible"] = True
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default="copilot")
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    if (
        not args.executable
        or "\x00" in args.executable
        or not 0 < args.timeout <= MAX_TIMEOUT_SECONDS
    ):
        parser.error("invalid executable or timeout")
    result = run(args.executable, args.timeout)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["compatible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
