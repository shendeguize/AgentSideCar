import ast
import hashlib
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
    ROOT / "README.zh.md",
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
    "main": {
        "check (ubuntu-latest, Python 3.9)",
        "Remote payload (Python 3.8)",
    },
    "release": {"check (macos-latest, Python 3.9)"},
    "version-tags": set(),
}
README_HEADING_PAIRS = (
    (1, "Agent Sidecar", "Agent Sidecar"),
    (2, "Version 0.9.0", "版本 0.9.0"),
    (2, "Support matrix", "支持矩阵"),
    (2, "Supported local sources", "支持的本地数据源"),
    (2, "Installation", "安装"),
    (3, "Install with pipx", "使用 pipx 安装"),
    (3, "Install a GitHub Release zipapp", "安装 GitHub Release zipapp"),
    (3, "Build the deterministic zipapp", "构建确定性 zipapp"),
    (3, "Use a repository checkout", "使用仓库检出版本"),
    (3, "Deploy the pod-local E2E topology", "部署 pod 本地 E2E 拓扑"),
    (2, "Uninstall", "卸载"),
    (2, "Commands", "命令"),
    (3, "Remote list and status", "远程列表与状态"),
    (3, "Remote watch", "远程监视"),
    (3, "Experimental local send", "实验性本地发送"),
    (3, "Persistent user service", "持久化用户服务"),
    (2, "Status semantics", "状态语义"),
    (2, "Daemon, fallback, and local protocol", "守护进程、回退与本地协议"),
    (3, "Opt-in loopback HTTP", "可选启用的回环 HTTP"),
    (2, "Agent skill integration", "Agent Skill 集成"),
    (2, "DSH plugin", "DSH 插件"),
    (2, "Development", "开发与质量门禁"),
    (3, "Release and version checklist", "发布与版本清单"),
    (2, "Security and reporting", "安全与问题报告"),
    (2, "Current scope and deferred work", "当前范围与后续工作"),
    (2, "FAQ", "常见问题"),
    (3, "Is Agent Sidecar published on PyPI?", "Agent Sidecar 发布到 PyPI 了吗？"),
    (
        3,
        "Can I use Agent Sidecar on Linux or Windows?",
        "可以在 Linux 或 Windows 上使用 Agent Sidecar 吗？",
    ),
    (
        3,
        "Why can a completed session still appear working?",
        "为什么已完成的会话仍显示为 working？",
    ),
    (
        3,
        "Can Agent Sidecar control remote sessions?",
        "Agent Sidecar 可以控制远程会话吗？",
    ),
    (
        3,
        "Where does Agent Sidecar store its own state?",
        "Agent Sidecar 在哪里存储自己的状态？",
    ),
    (2, "License", "许可证"),
)
README_REQUIRED_OPTIONS = {
    "--allow-write",
    "--force",
    "--from-start",
    "--host",
    "--http",
    "--http-port",
    "--json",
    "--remote",
    "--request-id",
    "--uninstall",
}
README_REQUIRED_LINK_TARGETS = {
    "README.md",
    "README.zh.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "https://github.com/shendeguize/AgentSideCar/releases/latest",
    "https://shendeguize.github.io/AgentSideCar/",
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


def _workflow_job_block(document, job_id):
    match = re.search(
        r"(?ms)^  {}:\s*$\n(?P<body>.*?)(?=^  \S[^:\n]*:\s*$|\Z)".format(
            re.escape(job_id)
        ),
        document,
    )
    if match is None:
        raise AssertionError("CI has no {!r} job".format(job_id))
    return match.group("body")


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
    matrix_contexts = {
        template.replace("${{ matrix.os }}", operating_system).replace(
            "${{ matrix.python-version }}", version
        )
        for operating_system in operating_systems
        for version in versions
    }
    static_contexts = {name for name in job_names if "${{" not in name}
    return matrix_contexts | static_contexts


def _required_contexts(ruleset):
    contexts = set()
    for rule in ruleset.get("rules", []):
        if rule.get("type") != "required_status_checks":
            continue
        checks = rule.get("parameters", {}).get("required_status_checks", [])
        contexts.update(check["context"] for check in checks)
    return contexts


def _markdown_headings(document):
    headings = []
    inside_fence = False
    for line in document.splitlines():
        if re.match(r"^\s*```", line):
            inside_fence = not inside_fence
            continue
        if inside_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match is not None:
            headings.append((len(match.group(1)), match.group(2)))
    if inside_fence:
        raise AssertionError("README has an unterminated fenced code block")
    return tuple(headings)


def _shell_command_blocks(document):
    return tuple(
        match.group("body").strip()
        for match in re.finditer(
            r"(?ms)^```(?:sh|bash)\s*$\n(?P<body>.*?)^```\s*$",
            document,
        )
    )


def _documented_long_options(document):
    return frozenset(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", document))


def _markdown_link_targets(document):
    return frozenset(re.findall(r"\]\(([^)\s]+)", document))


def _html_anchor_ids(document):
    return frozenset(re.findall(r'<a\s+id="([^"]+)"></a>', document))


class GovernanceContractTests(unittest.TestCase):
    def test_compiled_agent_governance_matches_canonical_soul(self):
        rules_dir = ROOT / ".rules"
        soul_path = rules_dir / "soul.mdc"
        config = (rules_dir / "compile-config.yaml").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        stored_hashes = json.loads(
            (rules_dir / ".compile-hashes.json").read_text(encoding="utf-8")
        )

        self.assertTrue(soul_path.is_file())
        soul = soul_path.read_text(encoding="utf-8").strip()
        self.assertEqual(
            "<!-- Auto-generated by devolaflow rule compiler. "
            "Do not edit manually. -->",
            agents.splitlines()[0],
        )
        self.assertGreater(len(agents.splitlines()), 20)
        self.assertIn(soul, agents)
        for pointer in (
            "CONTRIBUTING.md",
            "README.md",
            "README.zh.md",
            "skills/agent-sidecar/SKILL.md",
            "skills/agent-sidecar/reference.md",
            "CHANGELOG.md",
        ):
            with self.subTest(pointer=pointer):
                self.assertIn(pointer, agents)

        expected_hash = hashlib.sha256(agents.encode("utf-8")).hexdigest()[:16]
        self.assertEqual({"agents_md": expected_hash}, stored_hashes)
        for declaration in (
            'source_dir: ".rules"',
            "- name: soul",
            'output: "AGENTS.md"',
            "include_layers: [soul]",
            'hash_file: ".rules/.compile-hashes.json"',
        ):
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, config)

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

    def test_remote_python_3_8_job_contract(self):
        document = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        job = _workflow_job_block(document, "remote-python-3-8")

        self.assertRegex(job, r"(?m)^    name: Remote payload \(Python 3\.8\)$")
        self.assertRegex(job, r"(?m)^    runs-on: ubuntu-latest$")
        self.assertRegex(job, r"(?m)^    container: python:3\.8-slim$")
        self.assertRegex(
            job,
            r"(?m)^\s+uses: actions/checkout@[0-9a-fA-F]{40}(?:\s+#.*)?$",
        )
        self.assertRegex(
            job,
            r"(?m)^\s+run: python -m unittest "
            r"tests/test_remote\.py tests/test_remote_watch\.py$",
        )
        self.assertNotIn("actions/setup-python", job)
        self.assertNotIn("pip install", job)

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

    def test_release_recovery_uses_only_tag_qualified_checkouts(self):
        document = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")

        self.assertEqual(2, document.count("ref: refs/tags/${{ inputs.tag }}"))
        self.assertEqual(2, document.count("ref: ${{ github.ref }}"))
        self.assertEqual(2, document.count("- name: Validate release source ref"))
        self.assertEqual(2, document.count("if [[ ! \"${candidate}\" =~ ${semver_tag} ]]"))
        self.assertEqual(
            2,
            document.count('if [[ "${GITHUB_REF}" != refs/tags/* ]]'),
        )
        self.assertIn("runs-on: macos-15", document)
        self.assertIn('python-version: "3.11"', document)
        self.assertIn("sys.version_info[:2] != (3, 11)", document)

    def test_plugin_release_uses_oidc_and_builds_once(self):
        document = (WORKFLOWS / "plugin-release.yml").read_text(encoding="utf-8")

        self.assertIn('tags:\n      - "plugin-v*"', document)
        self.assertIn("id-token: write", document)
        self.assertIn('node-version: "24"', document)
        self.assertIn("package-manager-cache: false", document)
        self.assertIn("pnpm test", document)
        self.assertIn("pnpm typecheck && pnpm build", document)
        self.assertEqual(1, document.count("pnpm build"))
        self.assertIn(
            "npm publish --access public --provenance --ignore-scripts",
            document,
        )
        self.assertNotIn("NPM_TOKEN", document)

    def test_plugin_package_repository_matches_github_identity(self):
        package = json.loads(
            (ROOT / "plugin" / "package.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            "git+https://github.com/shendeguize/AgentSideCar.git",
            package["repository"]["url"],
        )

    def test_release_guard_requires_head_to_equal_the_peeled_tag_commit(self):
        document = (ROOT / "scripts" / "release_guard.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('git.try_resolve_commit("HEAD")', document)
        self.assertIn("if head_commit != tag_commit:", document)
        self.assertIn("does not match peeled tag", document)

    def test_release_publication_excludes_dispatch_and_asserts_exact_tag_ref(self):
        document = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        release_job = document[document.index("\n  release:\n") :]

        self.assertIn(
            "if: github.event_name == 'push' "
            "&& startsWith(github.ref, 'refs/tags/')",
            release_job,
        )
        self.assertNotIn("workflow_dispatch", release_job)
        ref_assertion = 'if [[ "${GITHUB_REF}" != "refs/tags/${RELEASE_TAG}" ]]'
        self.assertIn(ref_assertion, release_job)
        self.assertLess(
            release_job.index(ref_assertion),
            release_job.index("- name: Attest build provenance"),
        )

    def test_release_ubuntu_gate_uses_spawn_importable_script(self):
        document = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")

        self.assertIn(
            'quality_gate="${RUNNER_TEMP}/portable-quality-gate.py"',
            document,
        )
        self.assertIn('trap \'rm -f "${quality_gate}"\' EXIT', document)
        self.assertIn(
            'repository_root = Path(os.environ["GITHUB_WORKSPACE"]).resolve()',
            document,
        )
        self.assertIn('sys.path.insert(0, str(repository_root))', document)
        self.assertIn(
            'discover(\n                  str(repository_root / "tests")\n',
            document,
        )
        self.assertIn(
            'PYTHONPATH="${GITHUB_WORKSPACE}${PYTHONPATH:+:${PYTHONPATH}}"',
            document,
        )
        self.assertIn('if __name__ == "__main__":', document)
        self.assertIn('python "${quality_gate}"', document)
        self.assertNotIn(
            "python - <<'PY'\n          import unittest",
            document,
        )

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

    def test_dual_language_readmes_keep_structure_commands_and_links_in_sync(self):
        english_path = ROOT / "README.md"
        translated_path = ROOT / "README.zh.md"
        english = english_path.read_text(encoding="utf-8")
        translated = translated_path.read_text(encoding="utf-8")

        expected_english = tuple(
            (level, english_heading)
            for level, english_heading, _ in README_HEADING_PAIRS
        )
        expected_translated = tuple(
            (level, translated_heading)
            for level, _, translated_heading in README_HEADING_PAIRS
        )
        self.assertEqual(expected_english, _markdown_headings(english))
        self.assertEqual(expected_translated, _markdown_headings(translated))
        self.assertEqual(
            _shell_command_blocks(english),
            _shell_command_blocks(translated),
        )
        english_options = _documented_long_options(english)
        self.assertEqual(english_options, _documented_long_options(translated))
        self.assertLessEqual(README_REQUIRED_OPTIONS, english_options)
        self.assertEqual(
            _markdown_link_targets(english),
            _markdown_link_targets(translated),
        )

        link_targets = _markdown_link_targets(english)
        self.assertLessEqual(README_REQUIRED_LINK_TARGETS, link_targets)
        for target in link_targets:
            if target.startswith("#"):
                anchor = target[1:]
                with self.subTest(anchor=anchor):
                    self.assertIn(anchor, _html_anchor_ids(english))
                    self.assertIn(anchor, _html_anchor_ids(translated))
                continue
            if "://" in target:
                continue
            relative_path = target.partition("#")[0]
            with self.subTest(target=target):
                self.assertTrue((ROOT / relative_path).is_file())

        for command in CANONICAL_COMMAND_INVENTORY:
            documented = "agent-sidecar " + " ".join(command)
            with self.subTest(command=documented):
                self.assertIn(documented, english)
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
