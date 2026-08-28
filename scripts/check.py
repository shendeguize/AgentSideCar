#!/usr/bin/env python3
"""Run the repository's local quality stages in a stable order."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, TextIO, Tuple

ROOT = Path(__file__).resolve().parents[1]
STAGE_ORDER = ("lint", "tests", "coverage", "pack", "cli", "skill", "site")
FAST_STAGE_ORDER = ("lint", "coverage", "pack", "cli", "skill", "site")
StageRunner = Callable[[str, bool], int]


def select_stages(only: Optional[Sequence[str]]) -> Tuple[str, ...]:
    """Return requested stages once, in canonical execution order."""
    if not only:
        return STAGE_ORDER
    unknown = set(only).difference(STAGE_ORDER)
    if unknown:
        raise ValueError("unknown stage: {}".format(sorted(unknown)[0]))
    requested = set(only)
    return tuple(stage for stage in STAGE_ORDER if stage in requested)


def _run_command(
    arguments: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    result = subprocess.run(
        list(arguments),
        cwd=str(ROOT),
        check=False,
        env=None if env is None else dict(env),
    )
    return result.returncode


def _run_stage(stage: str, full_tests_ran: bool) -> int:
    python = sys.executable
    if stage == "lint":
        return _run_command(("ruff", "check", "."))
    if stage == "tests":
        return _run_command(
            (python, "-m", "unittest", "discover", "-s", "tests", "-v")
        )
    if stage == "coverage":
        with tempfile.TemporaryDirectory(
            prefix="agent-sidecar-coverage-"
        ) as temporary:
            data_file = Path(temporary) / ".coverage"
            report_file = Path(temporary) / "coverage.json"
            environment = dict(os.environ)
            environment["COVERAGE_FILE"] = str(data_file)
            test_code = _run_command(
                (
                    python,
                    "-m",
                    "coverage",
                    "run",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-v",
                ),
                env=environment,
            )
            if test_code:
                return test_code
            combine_code = _run_command(
                (python, "-m", "coverage", "combine"),
                env=environment,
            )
            if combine_code:
                return combine_code
            json_code = _run_command(
                (
                    python,
                    "-m",
                    "coverage",
                    "json",
                    "-o",
                    str(report_file),
                ),
                env=environment,
            )
            if json_code:
                return json_code
            return _run_command(
                (python, str(ROOT / "scripts" / "coverage_gate.py"), str(report_file))
            )
    if stage == "pack":
        with tempfile.TemporaryDirectory(prefix="agent-sidecar-check-") as temporary:
            artifact = Path(temporary) / "agent-sidecar.pyz"
            build_code = _run_command(
                (
                    python,
                    "-m",
                    "sidecar",
                    "package",
                    "build",
                    "--output",
                    str(artifact),
                )
            )
            if build_code:
                return build_code
            return _run_command((python, str(artifact), "--version"))
    if stage == "cli":
        help_code = _run_command((python, "-m", "sidecar", "--help"))
        if help_code:
            return help_code
        return _run_command((python, "-m", "sidecar", "--version"))
    if stage == "skill":
        if full_tests_ran:
            print("skill tests already covered by full discovery", flush=True)
            return 0
        return _run_command((python, "-m", "unittest", "-v", "tests.test_skill"))
    if stage == "site":
        return _run_command((python, str(ROOT / "scripts" / "site_check.py")))
    raise ValueError("unknown stage: {}".format(stage))


def run_stages(
    stages: Sequence[str],
    *,
    stage_runner: StageRunner = _run_stage,
    stream: Optional[TextIO] = None,
) -> int:
    """Run stages until the first failure."""
    output = sys.stdout if stream is None else stream
    completed = []
    for stage in stages:
        started = time.monotonic()
        print("==> {}".format(stage), file=output, flush=True)
        return_code = stage_runner(
            stage,
            "tests" in completed or "coverage" in completed,
        )
        elapsed = time.monotonic() - started
        if return_code:
            print(
                "<== {} failed ({:.2f}s)".format(stage, elapsed),
                file=output,
                flush=True,
            )
            return return_code
        completed.append(stage)
        print(
            "<== {} passed ({:.2f}s)".format(stage, elapsed),
            file=output,
            flush=True,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run local quality checks in canonical order."
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=STAGE_ORDER,
        metavar="STAGE",
        help="run only this stage (repeatable)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="run coverage once as the full test pass, avoiding duplicate tests",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stage_runner: StageRunner = _run_stage,
    stream: Optional[TextIO] = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.fast and args.only:
        raise SystemExit("--fast cannot be combined with --only")
    return run_stages(
        FAST_STAGE_ORDER if args.fast else select_stages(args.only),
        stage_runner=stage_runner,
        stream=stream,
    )


if __name__ == "__main__":
    raise SystemExit(main())
