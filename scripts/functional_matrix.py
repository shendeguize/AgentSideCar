#!/usr/bin/env python3
"""Validate and run the v0.9.0 functional test matrix."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "docs" / "traceability" / "functional-design-matrix.md"
DEFAULT_MANIFEST = ROOT / "docs" / "traceability" / "functional-tests.json"
ID_PATTERN = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
VALID_STATUSES = frozenset({"done", "partial", "blocked", "manual", "n/a"})


@dataclass(frozen=True)
class MatrixEntry:
    identifier: str
    status: str
    row_number: int


@dataclass(frozen=True)
class Suite:
    name: str
    runtime: str
    command: Tuple[str, ...]
    matrix_ids: Tuple[str, ...]


@dataclass(frozen=True)
class ValidationReport:
    entries: Tuple[MatrixEntry, ...]
    suites: Tuple[Suite, ...]
    duplicate_ids: Tuple[str, ...]
    unknown_test_ids: Tuple[str, ...]
    missing_test_ids: Tuple[str, ...]
    invalid_statuses: Tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not (
            self.duplicate_ids
            or self.unknown_test_ids
            or self.invalid_statuses
            or self.missing_test_ids
        )


def _table_rows(text: str) -> Iterable[Tuple[int, List[str]]]:
    for row_number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [cell.strip() for cell in stripped[1:-1].split("|")]
        if len(cells) >= 4:
            yield row_number, cells


def parse_matrix(text: str) -> Tuple[MatrixEntry, ...]:
    """Extract ID/status rows from the versioned matrix."""

    entries: List[MatrixEntry] = []
    for row_number, cells in _table_rows(text):
        identifier = cells[0]
        if identifier in {"ID", "Source", "Gap"}:
            continue
        if not ID_PATTERN.fullmatch(identifier):
            continue
        status = cells[-1].split(":", 1)[0].strip().lower()
        entries.append(MatrixEntry(identifier, status, row_number))
    return tuple(entries)


def load_matrix(path: Path = DEFAULT_MATRIX) -> Tuple[MatrixEntry, ...]:
    try:
        return parse_matrix(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError("cannot read matrix {}: {}".format(path, error)) from error


def _suite_from_object(value: object) -> Suite:
    if not isinstance(value, Mapping):
        raise ValueError("functional suite must be an object")
    name = value.get("name")
    runtime = value.get("runtime")
    command = value.get("command")
    matrix_ids = value.get("matrix_ids")
    if (
        not isinstance(name, str)
        or not name
        or runtime not in {"python", "plugin"}
        or not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
        or not isinstance(matrix_ids, list)
        or not matrix_ids
        or not all(isinstance(item, str) and ID_PATTERN.fullmatch(item) for item in matrix_ids)
    ):
        raise ValueError("invalid functional suite definition")
    return Suite(name, runtime, tuple(command), tuple(matrix_ids))


def load_suites(path: Path = DEFAULT_MANIFEST) -> Tuple[Suite, ...]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError("cannot read functional manifest {}: {}".format(path, error)) from error
    except json.JSONDecodeError as error:
        raise ValueError("invalid functional manifest {}: {}".format(path, error)) from error
    if not isinstance(document, Mapping) or document.get("version") != "0.9.0":
        raise ValueError("functional manifest must declare version 0.9.0")
    raw_suites = document.get("suites")
    if not isinstance(raw_suites, list) or not raw_suites:
        raise ValueError("functional manifest must contain suites")
    return tuple(_suite_from_object(value) for value in raw_suites)


def validate(
    entries: Sequence[MatrixEntry],
    suites: Sequence[Suite],
    *,
    allow_incomplete: bool = False,
) -> ValidationReport:
    del allow_incomplete
    entry_ids = [entry.identifier for entry in entries]
    suite_ids = [identifier for suite in suites for identifier in suite.matrix_ids]
    duplicates = tuple(
        sorted(
            {
                identifier
                for identifiers in (entry_ids, suite_ids)
                for identifier in identifiers
                if identifiers.count(identifier) > 1
            }
        )
    )
    unknown = tuple(sorted(set(suite_ids).difference(entry_ids)))
    missing = tuple(
        sorted(
            set(entry_ids).difference(suite_ids)
            - {
                entry.identifier
                for entry in entries
                if entry.status in {"n/a", "manual"}
            }
        )
    )
    invalid = tuple(
        "{}:{}".format(entry.identifier, entry.status)
        for entry in entries
        if entry.status not in VALID_STATUSES
    )
    return ValidationReport(
        tuple(entries),
        tuple(suites),
        duplicates,
        unknown,
        missing,
        invalid,
    )


def _print_report(report: ValidationReport, *, allow_incomplete: bool) -> None:
    print(
        "functional matrix: {} entries, {} suites".format(
            len(report.entries), len(report.suites)
        )
    )
    if report.duplicate_ids:
        print("duplicate matrix IDs: {}".format(", ".join(report.duplicate_ids)))
    if report.unknown_test_ids:
        print("unknown suite IDs: {}".format(", ".join(report.unknown_test_ids)))
    if report.invalid_statuses:
        print("invalid statuses: {}".format(", ".join(report.invalid_statuses)))
    if report.missing_test_ids:
        label = "unmapped release IDs"
        if allow_incomplete:
            label = "unmapped release IDs (allowed during migration)"
        print("{}: {}".format(label, ", ".join(report.missing_test_ids)))
    if report.passed or (
        allow_incomplete
        and not report.duplicate_ids
        and not report.unknown_test_ids
        and not report.invalid_statuses
    ):
        print("functional matrix: valid")
    else:
        print("functional matrix: incomplete")


def _run_suite(suite: Suite) -> int:
    command = list(suite.command)
    cwd = ROOT if suite.runtime == "python" else ROOT / "plugin"
    if suite.runtime == "python":
        command = [sys.executable] + command
    print("==> functional {} ({})".format(suite.name, suite.runtime), flush=True)
    return subprocess.run(command, cwd=str(cwd), check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "run"))
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        entries = load_matrix(args.matrix)
        suites = load_suites(args.manifest)
        report = validate(entries, suites, allow_incomplete=args.allow_incomplete)
    except ValueError as error:
        print("functional matrix error: {}".format(error), file=sys.stderr)
        return 2
    _print_report(report, allow_incomplete=args.allow_incomplete)
    if args.report is not None:
        document = {
            "version": "0.9.0",
            "entries": len(report.entries),
            "suites": [
                {
                    "name": suite.name,
                    "runtime": suite.runtime,
                    "matrix_ids": list(suite.matrix_ids),
                }
                for suite in report.suites
            ],
            "duplicate_ids": list(report.duplicate_ids),
            "unknown_test_ids": list(report.unknown_test_ids),
            "missing_test_ids": list(report.missing_test_ids),
            "invalid_statuses": list(report.invalid_statuses),
            "passed": report.passed,
        }
        try:
            args.report.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError as error:
            print(
                "functional matrix error: cannot write report: {}".format(error),
                file=sys.stderr,
            )
            return 2
    if not report.passed and not args.allow_incomplete:
        return 1
    if args.action == "check":
        return 0
    for suite in report.suites:
        result = _run_suite(suite)
        if result:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
