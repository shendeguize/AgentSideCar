import io
import runpy
import unittest
from pathlib import Path
from unittest import mock

from scripts import check


class CheckScriptTests(unittest.TestCase):
    def test_module_entrypoint_propagates_cli_exit_code(self):
        entrypoint = Path(__file__).parents[1] / "sidecar" / "__main__.py"
        with mock.patch("sidecar.cli.main", return_value=7):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(entrypoint), run_name="__main__")
        self.assertEqual(7, raised.exception.code)

    def test_default_selection_uses_canonical_stage_order(self):
        self.assertEqual(check.STAGE_ORDER, check.select_stages(None))

    def test_repeated_only_is_deduplicated_and_canonically_ordered(self):
        calls = []

        def runner(stage, full_tests_ran):
            calls.append((stage, full_tests_ran))
            return 0

        result = check.main(
            ["--only", "skill", "--only", "lint", "--only", "tests"],
            stage_runner=runner,
            stream=io.StringIO(),
        )

        self.assertEqual(0, result)
        self.assertEqual(
            [
                ("lint", False),
                ("tests", False),
                ("skill", True),
            ],
            calls,
        )

    def test_first_failure_stops_later_stages_and_propagates_code(self):
        calls = []

        def runner(stage, full_tests_ran):
            calls.append((stage, full_tests_ran))
            return 7 if stage == "tests" else 0

        result = check.run_stages(
            ("lint", "tests", "pack"),
            stage_runner=runner,
            stream=io.StringIO(),
        )

        self.assertEqual(7, result)
        self.assertEqual([("lint", False), ("tests", False)], calls)

    def test_fast_mode_runs_coverage_once_and_skips_duplicate_tests(self):
        calls = []

        def runner(stage, full_tests_ran):
            calls.append((stage, full_tests_ran))
            return 0

        result = check.main(
            ["--fast"],
            stage_runner=runner,
            stream=io.StringIO(),
        )

        self.assertEqual(0, result)
        self.assertEqual(
            [
                ("matrix", False),
                ("lint", False),
                ("coverage", False),
                ("pack", True),
                ("cli", True),
                ("skill", True),
                ("site", True),
            ],
            calls,
        )

    def test_fast_mode_cannot_be_combined_with_selected_stages(self):
        with self.assertRaises(SystemExit):
            check.main(["--fast", "--only", "lint"], stream=io.StringIO())


if __name__ == "__main__":
    unittest.main()
