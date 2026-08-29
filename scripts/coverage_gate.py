#!/usr/bin/env python3
"""Enforce tiered product coverage and the suppression-comment policy."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
CORE_THRESHOLD = 97.0
CORE_BRANCH_THRESHOLD = 97.0
RELAXED_THRESHOLD = 70.0
OVERALL_THRESHOLD = 85.0
RELAXED_MODULES = frozenset(
    {
        "sidecar/__main__.py",
        "sidecar/adapters/__init__.py",
        "sidecar/adapters/base.py",
        "sidecar/adapters/claude.py",
        "sidecar/adapters/codex.py",
        "sidecar/adapters/copilot.py",
        "sidecar/adapters/cursor.py",
        "sidecar/adapters/dsh.py",
        "sidecar/adapters/kimi.py",
        "sidecar/cli.py",
        "sidecar/client.py",
        "sidecar/cursor_chat.py",
        "sidecar/http_server.py",
        "sidecar/launchd.py",
        "sidecar/presentation.py",
        "sidecar/process_runner.py",
        "sidecar/release.py",
        "sidecar/remote_inventory.py",
        "sidecar/remote_transport.py",
        "sidecar/remote_watch_transport.py",
        "sidecar/runtime_cmd.py",
        "sidecar/tui.py",
    }
)

PRAGMA_MARKER = "# pragma:" + " no cover"
ALLOWED_PRAGMAS = {
    "sidecar/daemon_log.py": Counter(
        {
            "except ImportError:  "
            + PRAGMA_MARKER
            + " - exercised in an isolated import test": 1,
        }
    ),
    "sidecar/inject.py": Counter(
        {
            "except ImportError:  "
            + PRAGMA_MARKER
            + " - exercised only on non-POSIX Python": 1,
        }
    ),
    "sidecar/launchd.py": Counter(
        {
            "except ImportError:  "
            + PRAGMA_MARKER
            + " - unavailable on non-POSIX platforms": 1,
            "except ImportError:  "
            + PRAGMA_MARKER
            + " - unavailable on Windows": 1,
        }
    ),
    "sidecar/send_audit.py": Counter(
        {
            "except ImportError:  "
            + PRAGMA_MARKER
            + " - exercised only on non-POSIX Python": 1,
            "except ImportError:  "
            + PRAGMA_MARKER
            + " - unavailable on non-POSIX Python": 1,
        }
    ),
}
_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".local",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "build",
        "dist",
    }
)


class CoverageDataError(ValueError):
    """Raised when a coverage JSON document cannot support the gate."""


def load_baseline(path: Path) -> Mapping[str, Mapping[str, float]]:
    """Load the committed coverage floor used to detect regressions."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise CoverageDataError(
            "cannot read coverage baseline {}: {}".format(path, error)
        ) from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CoverageDataError(
            "invalid coverage baseline {}: {}".format(path, error)
        ) from error
    if not isinstance(document, Mapping) or document.get("version") != "0.9.0":
        raise CoverageDataError("coverage baseline must declare version 0.9.0")
    result: Dict[str, Mapping[str, float]] = {}
    for tier in ("core", "relaxed", "overall"):
        values = document.get(tier)
        if not isinstance(values, Mapping):
            raise CoverageDataError("coverage baseline missing {} metrics".format(tier))
        checked: Dict[str, float] = {}
        for name in ("lines", "branches"):
            value = values.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 100.0
            ):
                raise CoverageDataError(
                    "coverage baseline has invalid {} {} metric".format(tier, name)
                )
            checked[name] = float(value)
        result[tier] = checked
    return result


@dataclass(frozen=True)
class CoverageMetric:
    """Aggregated line and branch opportunities for one coverage tier."""

    files: Tuple[str, ...]
    covered_lines: int
    num_statements: int
    covered_branches: int
    num_branches: int

    @property
    def covered(self) -> int:
        return self.covered_lines + self.covered_branches

    @property
    def opportunities(self) -> int:
        return self.num_statements + self.num_branches

    @property
    def percent(self) -> float:
        """Return line coverage for compatibility with existing callers."""
        return self.line_percent

    @property
    def line_percent(self) -> float:
        if not self.num_statements:
            return 100.0
        return 100.0 * self.covered_lines / self.num_statements

    @property
    def branch_percent(self) -> float:
        if not self.num_branches:
            return 100.0
        return 100.0 * self.covered_branches / self.num_branches


@dataclass(frozen=True)
class CoverageGateResult:
    """Coverage metrics and any threshold failures."""

    metrics: Mapping[str, CoverageMetric]
    failures: Tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class PragmaReport:
    """Observed suppression count and policy violations."""

    count: int
    violations: Tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.violations


def _canonical_source_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized.startswith("sidecar/"):
        return normalized
    marker = "/sidecar/"
    if marker in normalized:
        return "sidecar/" + normalized.split(marker, 1)[1]
    raise CoverageDataError("coverage JSON contains non-sidecar file: {!r}".format(path))


def coverage_tier(path: str) -> str:
    """Return the governed tier for a sidecar source path."""
    canonical = _canonical_source_path(path)
    if canonical in RELAXED_MODULES:
        return "relaxed"
    return "core"


def _summary_counts(path: str, summary: object) -> Tuple[int, int, int, int]:
    if not isinstance(summary, Mapping):
        raise CoverageDataError("{} has no valid summary object".format(path))
    values = []
    for field in (
        "covered_lines",
        "num_statements",
        "covered_branches",
        "num_branches",
    ):
        value = summary.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CoverageDataError(
                "{} has invalid nonnegative integer {!r}".format(path, field)
            )
        values.append(value)
    covered_lines, num_statements, covered_branches, num_branches = values
    if covered_lines > num_statements or covered_branches > num_branches:
        raise CoverageDataError("{} reports covered work above its total".format(path))
    return covered_lines, num_statements, covered_branches, num_branches


def _aggregate(
    summaries: Mapping[str, Tuple[int, int, int, int]],
    paths: Sequence[str],
) -> CoverageMetric:
    covered_lines = num_statements = covered_branches = num_branches = 0
    for path in paths:
        line_covered, line_total, branch_covered, branch_total = summaries[path]
        covered_lines += line_covered
        num_statements += line_total
        covered_branches += branch_covered
        num_branches += branch_total
    metric = CoverageMetric(
        files=tuple(sorted(paths)),
        covered_lines=covered_lines,
        num_statements=num_statements,
        covered_branches=covered_branches,
        num_branches=num_branches,
    )
    if not metric.opportunities:
        raise CoverageDataError("coverage tier has no executable opportunities")
    return metric


def coverage_metrics(document: object) -> Dict[str, CoverageMetric]:
    """Build core, relaxed, and overall metrics from Coverage.py JSON."""
    if not isinstance(document, Mapping):
        raise CoverageDataError("coverage JSON root must be an object")
    files = document.get("files")
    totals = document.get("totals")
    if not isinstance(files, Mapping) or not files:
        raise CoverageDataError("coverage JSON must contain a nonempty files object")
    if not isinstance(totals, Mapping):
        raise CoverageDataError("coverage JSON must contain a totals object")

    summaries: Dict[str, Tuple[int, int, int, int]] = {}
    grouped: Dict[str, List[str]] = {"core": [], "relaxed": []}
    for raw_path, details in files.items():
        if not isinstance(raw_path, str) or not isinstance(details, Mapping):
            raise CoverageDataError("coverage file entries must be path/object pairs")
        path = _canonical_source_path(raw_path)
        if path in summaries:
            raise CoverageDataError("duplicate normalized coverage path: {}".format(path))
        summaries[path] = _summary_counts(path, details.get("summary"))
        grouped[coverage_tier(path)].append(path)

    if not grouped["core"] or not grouped["relaxed"]:
        raise CoverageDataError("coverage JSON must contain both core and relaxed files")

    metrics = {
        "core": _aggregate(summaries, grouped["core"]),
        "relaxed": _aggregate(summaries, grouped["relaxed"]),
        "overall": _aggregate(summaries, list(summaries)),
    }
    total_counts = _summary_counts("<totals>", totals)
    actual_counts = (
        metrics["overall"].covered_lines,
        metrics["overall"].num_statements,
        metrics["overall"].covered_branches,
        metrics["overall"].num_branches,
    )
    if total_counts != actual_counts:
        raise CoverageDataError(
            "coverage totals do not match the sum of sidecar file summaries"
        )
    return metrics


def evaluate_coverage(
    document: object,
    baseline: Optional[Mapping[str, Mapping[str, float]]] = None,
) -> CoverageGateResult:
    """Evaluate tiered line and core branch coverage thresholds."""
    metrics = coverage_metrics(document)
    thresholds = {
        "core": CORE_THRESHOLD,
        "relaxed": RELAXED_THRESHOLD,
        "overall": OVERALL_THRESHOLD,
    }
    failures = [
        "{} coverage {:.2f}% is below {:.2f}%".format(
            name, metrics[name].percent, threshold
        )
        for name, threshold in thresholds.items()
        if metrics[name].percent + 1e-12 < threshold
    ]
    if metrics["core"].branch_percent + 1e-12 < CORE_BRANCH_THRESHOLD:
        failures.append(
            "core branch coverage {:.2f}% is below {:.2f}%".format(
                metrics["core"].branch_percent,
                CORE_BRANCH_THRESHOLD,
            )
        )
    if baseline is not None:
        for name in ("core", "relaxed", "overall"):
            for metric_name, actual in (
                ("lines", metrics[name].line_percent),
                ("branches", metrics[name].branch_percent),
            ):
                floor = baseline[name][metric_name]
                if actual + 1e-12 < floor:
                    failures.append(
                        "{} {} coverage {:.2f}% is below baseline {:.2f}%".format(
                            name,
                            metric_name,
                            actual,
                            floor,
                        )
                    )
    return CoverageGateResult(metrics=metrics, failures=tuple(failures))


def load_coverage_json(path: Path) -> object:
    """Load one Coverage.py JSON report with stable diagnostics."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CoverageDataError(
            "cannot read coverage JSON {}: {}".format(path, error)
        ) from error
    try:
        return json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CoverageDataError(
            "invalid coverage JSON {}: {}".format(path, error)
        ) from error


def scan_pragmas(root: Path) -> PragmaReport:
    """Reject coverage suppressions outside the exact historical allowlist."""
    observed: Dict[str, Counter] = {}
    violations: List[str] = []
    count = 0
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if any(part in _IGNORED_DIRECTORY_NAMES for part in relative.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            violations.append("{}: cannot scan: {}".format(relative, error))
            continue
        source_path = relative.as_posix()
        for line_number, line in enumerate(lines, 1):
            if PRAGMA_MARKER not in line:
                continue
            count += 1
            stripped = line.strip()
            observed.setdefault(source_path, Counter())[stripped] += 1
            if (
                observed[source_path][stripped]
                > ALLOWED_PRAGMAS.get(source_path, Counter())[stripped]
            ):
                violations.append(
                    "{}:{}: unapproved coverage suppression".format(
                        source_path, line_number
                    )
                )
    return PragmaReport(count=count, violations=tuple(violations))


def _print_metric(name: str, metric: CoverageMetric) -> None:
    print(
        "coverage {}: lines {:.2f}% ({}/{}), "
        "branches {:.2f}% ({}/{})".format(
            name,
            metric.line_percent,
            metric.covered_lines,
            metric.num_statements,
            metric.branch_percent,
            metric.covered_branches,
            metric.num_branches,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enforce tiered Coverage.py JSON and suppression policy."
    )
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root used for suppression scanning",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="committed coverage floor used for regression detection",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        baseline = None if args.baseline is None else load_baseline(args.baseline)
        result = evaluate_coverage(
            load_coverage_json(args.coverage_json),
            baseline=baseline,
        )
    except CoverageDataError as error:
        print("coverage gate data error: {}".format(error), file=sys.stderr)
        return 2

    for name in ("core", "relaxed", "overall"):
        _print_metric(name, result.metrics[name])
        print(
            "coverage {} files: {}".format(
                name,
                ", ".join(result.metrics[name].files),
            )
        )
    print(
        "coverage thresholds: core >= {:.2f}% lines / {:.2f}% branches, "
        "relaxed >= {:.2f}%, overall >= {:.2f}%".format(
            CORE_THRESHOLD,
            CORE_BRANCH_THRESHOLD,
            RELAXED_THRESHOLD,
            OVERALL_THRESHOLD,
        )
    )

    pragma_report = scan_pragmas(args.root)
    print(
        "coverage suppressions: {} existing, {} allowlisted signatures".format(
            pragma_report.count,
            sum(sum(lines.values()) for lines in ALLOWED_PRAGMAS.values()),
        )
    )
    for failure in result.failures:
        print("coverage gate failed: {}".format(failure), file=sys.stderr)
    for violation in pragma_report.violations:
        print("coverage gate failed: {}".format(violation), file=sys.stderr)
    return 0 if result.passed and pragma_report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
