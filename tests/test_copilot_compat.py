import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from scripts.copilot_compat import REQUIRED_FLAGS, run


class CopilotCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.executable = Path(self.temporary.name) / "copilot"

    def tearDown(self):
        self.temporary.cleanup()

    def write_cli(self, body):
        self.executable.write_text(body, encoding="utf-8")
        self.executable.chmod(
            stat.S_IRUSR
            | stat.S_IWUSR
            | stat.S_IXUSR
        )

    def test_compatible_probe_only_checks_public_help_contract(self):
        self.write_cli(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "--version) echo 'GitHub Copilot 1.2.3';;\n"
            "--help) printf '%s\\n' '{}';;\n"
            "esac\n".format(" ".join(REQUIRED_FLAGS))
        )
        result = run(str(self.executable), 2.0)
        self.assertTrue(result["compatible"])
        self.assertEqual("1.2.3", result["version"])
        self.assertEqual(list(REQUIRED_FLAGS), result["flags_present"])
        self.assertNotIn("token", json.dumps(result).casefold())

    def test_missing_cli_is_honest_and_bounded(self):
        result = run(os.path.join(self.temporary.name, "missing-copilot"), 2.0)
        self.assertFalse(result["available"])
        self.assertFalse(result["compatible"])
        self.assertEqual("cli_not_found", result["reason"])


if __name__ == "__main__":
    unittest.main()
