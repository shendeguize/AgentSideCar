import ast
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RULESETS = ROOT / ".github" / "rulesets"
DOCUMENTATION = (
    ROOT / "README.md",
    ROOT / "skills" / "agent-sidecar" / "SKILL.md",
    ROOT / "skills" / "agent-sidecar" / "reference.md",
)
CANONICAL_COMMAND_INVENTORY = (
    ("status",),
    ("list",),
    ("watch",),
    ("send",),
    ("audit", "reset"),
    ("package", "build"),
    ("daemon", "start"),
    ("service", "install"),
    ("tui",),
)
EXPECTED_RULESETS = {
    "main.json": ("main", "branch", "refs/heads/main"),
    "release.json": ("release", "branch", "refs/heads/release"),
    "tags.json": ("version-tags", "tag", "refs/tags/v*"),
}
EXPECTED_REQUIRED_CONTEXTS = {
    "main": {"check (ubuntu-latest, Python 3.9)"},
    "release": {"check (macos-latest, Python 3.9)"},
    "version-tags": set(),
}


def _project_dependencies(document):
    section = re.search(
        r"(?ms)^\[project\]\s*$\n(?P<body>.*?)(?=^\[|\Z)",
        document,
    )
    if section is None:
        raise AssertionError("pyproject.toml has no [project] section")
    assignment = re.search(
        r"(?ms)^dependencies\s*=\s*(?P<value>\[.*?\])\s*$",
        section.group("body"),
    )
    if assignment is None:
        raise AssertionError("[project].dependencies is missing or not an array")
    try:
        value = ast.literal_eval(assignment.group("value"))
    except (SyntaxError, ValueError) as error:
        raise AssertionError("[project].dependencies is not a literal array") from error
    if not isinstance(value, list):
        raise AssertionError("[project].dependencies is not an array")
    return value


def _declared_version(source):
    tree = ast.parse(source)
    for statement in tree.body:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == "__version__"
            and isinstance(statement.value, ast.Str)
        ):
            return statement.value.s
    raise AssertionError("sidecar.__version__ is not a string literal")


def _latest_released_changelog_version(document):
    versions = re.findall(r"(?m)^## \[([^\]]+)\](?:\s+-\s+.*)?$", document)
    for version in versions:
        if version.lower() != "unreleased":
            return version
    raise AssertionError("CHANGELOG.md has no released version section")


def _workflow_job_display_names(document):
    return tuple(
        value.strip().strip("\"'")
        for value in re.findall(r"(?m)^ {4}name:\s*(.+?)\s*$", document)
    )


def _stable_ci_contexts(document):
    job_names = _workflow_job_display_names(document)
    template = next(
        (name for name in job_names if "matrix.os" in name),
        None,
    )
    if template is None:
        raise AssertionError("CI has no stable matrix job display-name template")
    version_match = re.search(r"python-version:\s*(\[[^\n]+\])", document)
    if version_match is None:
        raise AssertionError("CI has no literal Python version matrix")
    versions = json.loads(version_match.group(1))
    operating_systems = sorted(
        set(re.findall(r'"(ubuntu-latest|macos-latest)"', document))
    )
    if not operating_systems:
        raise AssertionError("CI has no stable runner labels")
    return {
        template.replace("${{ matrix.os }}", operating_system).replace(
            "${{ matrix.python-version }}", version
        )
        for operating_system in operating_systems
        for version in versions
    }


def _required_contexts(ruleset):
    contexts = set()
    for rule in ruleset.get("rules", []):
        if rule.get("type") != "required_status_checks":
            continue
        checks = rule.get("parameters", {}).get("required_status_checks", [])
        contexts.update(check["context"] for check in checks)
    return contexts


def _heading_levels(document):
    return tuple(
        len(match.group(1))
        for match in re.finditer(r"(?m)^(#{1,6})\s+\S", document)
    )


class GovernanceContractTests(unittest.TestCase):
    def test_runtime_dependencies_remain_an_explicit_empty_array(self):
        document = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertEqual([], _project_dependencies(document))

    def test_package_version_matches_latest_released_changelog_section(self):
        source = (ROOT / "sidecar" / "__init__.py").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

        self.assertEqual(
            _latest_released_changelog_version(changelog),
            _declared_version(source),
        )

    def test_ruleset_required_contexts_are_stable_ci_display_names(self):
        ci_document = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        stable_contexts = _stable_ci_contexts(ci_document)
        contexts_by_ruleset = {}
        for path in sorted(RULESETS.glob("*.json")):
            ruleset = json.loads(path.read_text(encoding="utf-8"))
            contexts_by_ruleset[ruleset["name"]] = _required_contexts(ruleset)

        self.assertEqual(EXPECTED_REQUIRED_CONTEXTS, contexts_by_ruleset)
        for name, contexts in contexts_by_ruleset.items():
            with self.subTest(ruleset=name):
                self.assertLessEqual(contexts, stable_contexts)

    def test_all_workflow_action_references_are_full_commit_shas(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            document = path.read_text(encoding="utf-8")
            references = re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", document)
            self.assertTrue(references, "{} has no action references".format(path.name))
            for reference in references:
                if reference.startswith("./") or reference.startswith("docker://"):
                    continue
                with self.subTest(workflow=path.name, reference=reference):
                    self.assertRegex(reference, r"^[^@\s]+@[0-9a-fA-F]{40}$")

    def test_documented_canonical_commands_are_backed_by_cli_help(self):
        for path in DOCUMENTATION:
            document = path.read_text(encoding="utf-8")
            for command in CANONICAL_COMMAND_INVENTORY:
                documented = "agent-sidecar " + " ".join(command)
                with self.subTest(document=path.name, command=documented):
                    self.assertIn(documented, document)

        for command in CANONICAL_COMMAND_INVENTORY:
            completed = subprocess.run(
                (sys.executable, "-m", "sidecar", *command, "--help"),
                cwd=str(ROOT),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            with self.subTest(command=command):
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertIn("usage:", completed.stdout.lower())
                self.assertIn(" ".join(command), completed.stdout)

    def test_dual_language_readme_structure_when_translation_exists(self):
        english_path = ROOT / "README.md"
        translated_path = ROOT / "README.zh.md"
        if not translated_path.exists():
            self.skipTest(
                "README.zh.md is introduced in the next documentation batch; "
                "this contract activates automatically once it exists"
            )

        english = english_path.read_text(encoding="utf-8")
        translated = translated_path.read_text(encoding="utf-8")
        self.assertEqual(_heading_levels(english), _heading_levels(translated))
        for command in CANONICAL_COMMAND_INVENTORY:
            documented = "agent-sidecar " + " ".join(command)
            with self.subTest(command=documented):
                self.assertIn(documented, translated)

    def test_three_ruleset_files_are_valid_and_target_expected_refs(self):
        paths = sorted(RULESETS.glob("*.json"))
        self.assertEqual(sorted(EXPECTED_RULESETS), [path.name for path in paths])
        for path in paths:
            ruleset = json.loads(path.read_text(encoding="utf-8"))
            expected_name, expected_target, expected_ref = EXPECTED_RULESETS[path.name]
            with self.subTest(path=path.name):
                self.assertEqual(expected_name, ruleset.get("name"))
                self.assertEqual(expected_target, ruleset.get("target"))
                self.assertEqual("active", ruleset.get("enforcement"))
                self.assertEqual(
                    [expected_ref],
                    ruleset.get("conditions", {})
                    .get("ref_name", {})
                    .get("include"),
                )
                self.assertEqual(
                    [],
                    ruleset.get("conditions", {})
                    .get("ref_name", {})
                    .get("exclude"),
                )


if __name__ == "__main__":
    unittest.main()
