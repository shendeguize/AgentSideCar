import json
import tempfile
import unittest
from pathlib import Path

from scripts import functional_matrix


class FunctionalMatrixTests(unittest.TestCase):
    def test_current_matrix_and_manifest_have_stable_release_shape(self):
        entries = functional_matrix.load_matrix()
        suites = functional_matrix.load_suites()
        identifiers = {entry.identifier for entry in entries}
        self.assertGreaterEqual(len(entries), 150)
        self.assertIn("FU-AGENT-001", identifiers)
        self.assertIn("UX-17", identifiers)
        self.assertIn("ENV-N6", identifiers)
        report = functional_matrix.validate(entries, suites)
        self.assertTrue(report.passed)
        self.assertEqual((), report.missing_test_ids)

    def test_validation_rejects_duplicate_unknown_and_invalid_entries(self):
        entries = (
            functional_matrix.MatrixEntry("FU-CLI-001", "done", 1),
            functional_matrix.MatrixEntry("FU-CLI-001", "broken", 2),
        )
        suites = (
            functional_matrix.Suite(
                "suite",
                "python",
                ("-m", "unittest", "tests.test_cli"),
                ("FU-CLI-001", "FU-UNKNOWN-001"),
            ),
        )
        report = functional_matrix.validate(entries, suites)
        self.assertEqual(("FU-CLI-001",), report.duplicate_ids)
        self.assertEqual(("FU-UNKNOWN-001",), report.unknown_test_ids)
        self.assertEqual(("FU-CLI-001:broken",), report.invalid_statuses)
        self.assertFalse(report.passed)

        duplicate_suite = functional_matrix.Suite(
            "duplicate",
            "python",
            ("-m", "unittest", "tests.test_cli"),
            ("FU-CLI-001", "FU-CLI-001"),
        )
        self.assertIn(
            "FU-CLI-001",
            functional_matrix.validate(
                (functional_matrix.MatrixEntry("FU-CLI-001", "done", 1),),
                (duplicate_suite,),
            ).duplicate_ids,
        )

    def test_na_entries_do_not_require_functional_suite_mapping(self):
        entries = (
            functional_matrix.MatrixEntry("META-7", "n/a", 1),
            functional_matrix.MatrixEntry("UX-17", "manual", 2),
            functional_matrix.MatrixEntry("FU-CLI-001", "done", 3),
        )
        suites = (
            functional_matrix.Suite(
                "suite",
                "python",
                ("-m", "unittest", "tests.test_cli"),
                ("FU-CLI-001",),
            ),
        )
        self.assertTrue(functional_matrix.validate(entries, suites).passed)

    def test_manifest_and_matrix_load_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            bad_matrix = directory / "matrix.md"
            bad_manifest = directory / "manifest.json"
            bad_matrix.write_text("| ID | Requirement | Evidence | Status |\n", encoding="utf-8")
            bad_manifest.write_text("[]", encoding="utf-8")
            self.assertEqual((), functional_matrix.load_matrix(bad_matrix))
            with self.assertRaises(ValueError):
                functional_matrix.load_suites(bad_manifest)
            bad_manifest.write_text(
                json.dumps({"version": "0.8.0", "suites": []}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                functional_matrix.load_suites(bad_manifest)

    def test_suite_definition_validation(self):
        with self.assertRaises(ValueError):
            functional_matrix._suite_from_object({})
        with self.assertRaises(ValueError):
            functional_matrix._suite_from_object(
                {
                    "name": "bad",
                    "runtime": "python",
                    "command": ["python"],
                    "matrix_ids": ["not an id"],
                }
            )


if __name__ == "__main__":
    unittest.main()
