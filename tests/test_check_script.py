import io
import unittest

from scripts import check


class CheckScriptTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
