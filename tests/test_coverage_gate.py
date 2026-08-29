import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts import coverage_gate


def summary(covered_lines, num_statements, covered_branches=0, num_branches=0):
    return {
        "covered_lines": covered_lines,
        "num_statements": num_statements,
        "covered_branches": covered_branches,
        "num_branches": num_branches,
    }


def document(files):
    totals = summary(0, 0, 0, 0)
    entries = {}
    for path, counts in files.items():
        entries[path] = {"summary": counts}
        for field in totals:
            totals[field] += counts[field]
    return {"files": entries, "totals": totals}


class CoverageGateTests(unittest.TestCase):
    def test_core_redline_is_97_for_lines_and_branches(self):
        self.assertEqual(97.0, coverage_gate.CORE_THRESHOLD)
        self.assertEqual(97.0, coverage_gate.CORE_BRANCH_THRESHOLD)

    def test_thresholds_pass_at_or_above_every_minimum(self):
        result = coverage_gate.evaluate_coverage(
            document(
                {
                    "sidecar/model.py": summary(100, 100),
                    "sidecar/cli.py": summary(75, 100),
                }
            )
        )

        self.assertTrue(result.passed)
        self.assertEqual(100.0, result.metrics["core"].percent)
        self.assertEqual(75.0, result.metrics["relaxed"].percent)
        self.assertEqual(87.5, result.metrics["overall"].percent)

    def test_each_threshold_can_fail_independently_or_in_aggregate(self):
        cases = (
            (
                {
                    "sidecar/model.py": summary(89, 100),
                    "sidecar/cli.py": summary(100, 100),
                },
                ("core coverage",),
            ),
            (
                {
                    "sidecar/model.py": summary(100, 100),
                    "sidecar/cli.py": summary(6, 10),
                },
                ("relaxed coverage",),
            ),
            (
                {
                    "sidecar/model.py": summary(95, 100),
                    "sidecar/cli.py": summary(70, 100),
                },
                ("core coverage", "overall coverage"),
            ),
        )
        for files, expected_failures in cases:
            with self.subTest(expected_failures=expected_failures):
                result = coverage_gate.evaluate_coverage(document(files))
                self.assertFalse(result.passed)
                self.assertEqual(
                    expected_failures,
                    tuple(failure.split()[0] + " coverage" for failure in result.failures),
                )

    def test_core_branch_coverage_is_a_hard_gate(self):
        result = coverage_gate.evaluate_coverage(
            document(
                {
                    "sidecar/model.py": summary(100, 100, 0, 100),
                    "sidecar/cli.py": summary(100, 100, 100, 100),
                }
            )
        )

        self.assertEqual(100.0, result.metrics["core"].percent)
        self.assertEqual(0.0, result.metrics["core"].branch_percent)
        self.assertFalse(result.passed)
        self.assertTrue(
            any(
                failure.startswith("core branch coverage")
                for failure in result.failures
            )
        )

    def test_tier_grouping_is_explicit_and_exhaustive(self):
        for path in coverage_gate.RELAXED_MODULES:
            with self.subTest(path=path):
                self.assertEqual("relaxed", coverage_gate.coverage_tier(path))
        for path in (
            "sidecar/model.py",
            "sidecar/state.py",
            "/checkout/sidecar/remote.py",
        ):
            with self.subTest(path=path):
                self.assertEqual("core", coverage_gate.coverage_tier(path))

    def test_malformed_or_incomplete_coverage_data_is_rejected(self):
        invalid_documents = (
            None,
            {},
            {"files": {}, "totals": {}},
            {
                "files": {"sidecar/model.py": {"summary": summary(1, 1)}},
                "totals": summary(1, 1),
            },
            document(
                {
                    "tests/test_example.py": summary(1, 1),
                    "sidecar/cli.py": summary(1, 1),
                }
            ),
            {
                "files": {
                    "sidecar/model.py": {"summary": summary(2, 1)},
                    "sidecar/cli.py": {"summary": summary(1, 1)},
                },
                "totals": summary(3, 2),
            },
            {
                "files": {
                    "sidecar/model.py": {"summary": summary(1, 1)},
                    "sidecar/cli.py": {"summary": summary(1, 1)},
                },
                "totals": summary(1, 2),
            },
        )
        for invalid in invalid_documents:
            with self.subTest(invalid=invalid):
                with self.assertRaises(coverage_gate.CoverageDataError):
                    coverage_gate.coverage_metrics(invalid)

    def test_baseline_regression_is_reported_independently_of_absolute_gate(self):
        baseline = {
            "core": {"lines": 98.0, "branches": 98.0},
            "relaxed": {"lines": 80.0, "branches": 70.0},
            "overall": {"lines": 90.0, "branches": 80.0},
        }
        result = coverage_gate.evaluate_coverage(
            document(
                {
                    "sidecar/model.py": summary(97, 100, 97, 100),
                    "sidecar/cli.py": summary(80, 100, 70, 100),
                }
            ),
            baseline=baseline,
        )
        self.assertFalse(result.passed)
        self.assertTrue(
            any("core lines coverage" in failure for failure in result.failures)
        )
        self.assertTrue(
            any("core branches coverage" in failure for failure in result.failures)
        )

    def test_baseline_loader_rejects_wrong_version_and_invalid_metric(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "baseline.json"
            path.write_text(json.dumps({"version": "0.8.0"}), encoding="utf-8")
            with self.assertRaises(coverage_gate.CoverageDataError):
                coverage_gate.load_baseline(path)
            path.write_text(
                json.dumps(
                    {
                        "version": "0.9.0",
                        "core": {"lines": 101, "branches": 97},
                        "relaxed": {"lines": 70, "branches": 70},
                        "overall": {"lines": 80, "branches": 80},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(coverage_gate.CoverageDataError):
                coverage_gate.load_baseline(path)

    def test_missing_and_invalid_json_return_data_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid = root / "invalid.json"
            invalid.write_text("{", encoding="utf-8")
            for path in (root / "missing.json", invalid):
                with self.subTest(path=path):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        return_code = coverage_gate.main(
                            [str(path), "--root", str(root)]
                        )
                    self.assertEqual(2, return_code)
                    self.assertIn("coverage gate data error", stderr.getvalue())

    def test_pragma_allowlist_accepts_only_exact_historical_occurrence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed_path = root / "sidecar" / "inject.py"
            allowed_path.parent.mkdir(parents=True)
            allowed_line = next(
                iter(coverage_gate.ALLOWED_PRAGMAS["sidecar/inject.py"])
            )
            allowed_path.write_text(allowed_line + "\n", encoding="utf-8")

            report = coverage_gate.scan_pragmas(root)
            self.assertTrue(report.passed)
            self.assertEqual(1, report.count)

            new_path = root / "sidecar" / "new_module.py"
            new_path.write_text(
                "value = 1  " + coverage_gate.PRAGMA_MARKER + "\n",
                encoding="utf-8",
            )
            report = coverage_gate.scan_pragmas(root)
            self.assertFalse(report.passed)
            self.assertEqual(2, report.count)
            self.assertEqual(1, len(report.violations))
            self.assertIn("sidecar/new_module.py:1", report.violations[0])

    def test_pragma_scan_ignores_private_local_workspace_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_path = root / ".local" / "reference" / "private.py"
            private_path.parent.mkdir(parents=True)
            private_path.write_text(
                "value = 1  " + coverage_gate.PRAGMA_MARKER + "\n",
                encoding="utf-8",
            )

            report = coverage_gate.scan_pragmas(root)

            self.assertTrue(report.passed)
            self.assertEqual(0, report.count)

    def test_gate_main_prints_metrics_thresholds_and_suppression_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "coverage.json"
            report_path.write_text(
                json.dumps(
                    document(
                        {
                            "sidecar/model.py": summary(100, 100),
                            "sidecar/cli.py": summary(75, 100),
                        }
                    )
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                return_code = coverage_gate.main(
                    [str(report_path), "--root", str(root)]
                )

        self.assertEqual(0, return_code)
        output = stdout.getvalue()
        self.assertIn("coverage core:", output)
        self.assertIn("coverage thresholds:", output)
        self.assertIn("coverage suppressions: 0 existing", output)


if __name__ == "__main__":
    unittest.main()
