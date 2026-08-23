import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from sidecar.adapters.base import sanitize_terminal_text
from sidecar.process_runner import run_bounded


COMMAND_TIMEOUT_SECONDS = 15
OUTPUT_LIMIT_BYTES = 128 * 1024
DIAGNOSTIC_LIMIT = 2000
SAFE_PARSER_ARGUMENTS = {
    "claude": {
        ("--version",),
        ("--help",),
    },
    "codex": {
        ("--version",),
        ("--help",),
        ("exec", "--help"),
        ("exec", "resume", "--help"),
    },
    "cursor-agent": {
        ("--version",),
        ("--help",),
    },
}
STORE_PATHS = (
    Path(".claude/projects"),
    Path(".codex/sessions"),
    Path(".codex/thread_history_1.sqlite"),
    Path(".cursor/chats"),
    Path(".cursor/projects"),
)
_VALUE_TOKEN = r"[A-Za-z0-9][A-Za-z0-9._-]*"


def _file_signature(path):
    try:
        details = os.stat(str(path))
    except OSError:
        return None
    return (
        stat.S_IFMT(details.st_mode),
        int(details.st_size),
        int(getattr(details, "st_mtime_ns", details.st_mtime * 1e9)),
    )


def _store_signatures(home):
    return tuple(
        (str(relative), _file_signature(home / relative))
        for relative in STORE_PATHS
    )


def _decoded_output(result):
    return (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace")


def _help_block(help_text, declaration):
    lines = help_text.splitlines()
    for index, line in enumerate(lines):
        if declaration.match(line) is None:
            continue
        indentation = len(line) - len(line.lstrip())
        block = [line.strip()]
        for continuation in lines[index + 1 :]:
            if not continuation.strip():
                break
            continuation_indent = len(continuation) - len(continuation.lstrip())
            if continuation_indent <= indentation:
                break
            block.append(continuation.strip())
        return "\n".join(block)
    return None


def _option_block(help_text, option):
    declaration = re.compile(
        r"^\s*(?:-[A-Za-z0-9?],\s*)?"
        + re.escape(option)
        + r"(?=$|\s|<|\[|=(?=[<\[]))",
        re.IGNORECASE,
    )
    return _help_block(help_text, declaration)


def _argument_block(help_text, argument):
    declaration = re.compile(
        r"^\s*(?:\[|<)?"
        + re.escape(argument)
        + r"(?:\.\.\.)?(?:\]|>)?(?=$|\s)",
        re.IGNORECASE,
    )
    return _help_block(help_text, declaration)


def _documented_values(option_block):
    if option_block is None:
        return set()
    values = set(
        match.group(1).casefold()
        for match in re.finditer(
            r"[\"'](" + _VALUE_TOKEN + r")[\"']",
            option_block,
        )
    )
    for match in re.finditer(
        r"\b" + _VALUE_TOKEN + r"(?:\s*\|\s*" + _VALUE_TOKEN + r")+\b",
        option_block,
    ):
        values.update(
            item.strip().casefold()
            for item in match.group(0).split("|")
        )
    for match in re.finditer(
        r"(?:choices|possible values)\s*:\s*([^) \]]+(?:\s*,\s*[^) \]]+)*)",
        option_block,
        re.IGNORECASE,
    ):
        values.update(
            token.casefold()
            for token in re.findall(_VALUE_TOKEN, match.group(1))
        )
    return values


class NativeHelpContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.sandbox_root = Path(self.temporary.name).resolve()
        self.isolated_home = self.sandbox_root / "home"
        self.isolated_tmp = self.sandbox_root / "tmp"
        self.xdg_cache = self.sandbox_root / "xdg-cache"
        self.xdg_config = self.sandbox_root / "xdg-config"
        self.xdg_data = self.sandbox_root / "xdg-data"
        for directory in (
            self.isolated_home,
            self.isolated_tmp,
            self.xdg_cache,
            self.xdg_config,
            self.xdg_data,
        ):
            directory.mkdir()
        self.environment = None

        def clean_temporary_directory():
            self.temporary.cleanup()
            self.assertFalse(
                self.sandbox_root.exists(),
                "native contract sandbox was not removed",
            )

        self.addCleanup(clean_temporary_directory)

    def _executable(self, name):
        located = shutil.which(name)
        if located is None:
            self.skipTest("{} is not installed".format(name))
        located_path = Path(os.path.abspath(located))
        try:
            resolved = located_path.resolve(strict=True)
        except OSError as error:
            self.skipTest("{} cannot be resolved: {}".format(name, error))
        if not resolved.is_file() or not os.access(str(resolved), os.X_OK):
            self.skipTest("{} is not an executable file".format(name))

        path_entries = []
        for candidate in (
            located_path.parent,
            resolved.parent,
        ):
            value = str(candidate)
            if value not in path_entries:
                path_entries.append(value)
        for candidate in os.defpath.split(os.pathsep):
            if (
                candidate
                and candidate != "."
                and os.path.isabs(candidate)
                and candidate not in path_entries
            ):
                path_entries.append(candidate)

        self.environment = {
            "HOME": str(self.isolated_home),
            "USERPROFILE": str(self.isolated_home),
            "XDG_CACHE_HOME": str(self.xdg_cache),
            "XDG_CONFIG_HOME": str(self.xdg_config),
            "XDG_DATA_HOME": str(self.xdg_data),
            "TMPDIR": str(self.isolated_tmp),
            "TMP": str(self.isolated_tmp),
            "TEMP": str(self.isolated_tmp),
            "PATH": os.pathsep.join(path_entries),
            "LANG": "C",
            "LC_ALL": "C",
            "TERM": "dumb",
            "NO_COLOR": "1",
        }
        return str(resolved)

    def _sanitized(self, value, executable=None):
        if isinstance(value, bytes):
            text = value.decode("utf-8", "replace")
        else:
            text = str(value or "")
        for sensitive in (
            str(self.isolated_home),
            str(Path.home()),
            executable,
        ):
            if sensitive:
                text = text.replace(str(sensitive), "[path]")
        text = re.sub(
            r"(?i)\b(api[-_ ]?key|authorization|token)"
            r"(\s*[:=]\s*)\S+",
            r"\1\2[redacted]",
            text,
        )
        sanitized = sanitize_terminal_text(text)
        return sanitized[:DIAGNOSTIC_LIMIT] or "<no output>"

    def _run_parser(self, name, executable, arguments):
        self.assertIn(arguments, SAFE_PARSER_ARGUMENTS[name])
        self.assertIsNotNone(self.environment)
        before = _store_signatures(self.isolated_home)
        try:
            try:
                result = run_bounded(
                    (executable,) + arguments,
                    input_limit=1,
                    stdout_limit=OUTPUT_LIMIT_BYTES,
                    stderr_limit=OUTPUT_LIMIT_BYTES,
                    timeout=COMMAND_TIMEOUT_SECONDS,
                    env=self.environment,
                    cwd=self.isolated_home,
                )
            except subprocess.TimeoutExpired as error:
                details = "{}\n{}".format(error.output or "", error.stderr or "")
                self.fail(
                    "{} parser command timed out: {}".format(
                        name,
                        self._sanitized(details, executable),
                    )
                )
            except OSError as error:
                self.fail(
                    "{} parser command could not run: {}".format(
                        name,
                        self._sanitized(error, executable),
                    )
                )
        finally:
            self.assertEqual(
                before,
                _store_signatures(self.isolated_home),
                "{} parser command changed an isolated agent store".format(name),
            )

        diagnostic = self._sanitized(_decoded_output(result), executable)
        self.assertIsNone(
            result.overflow,
            "{} parser output exceeded its cap: {}".format(name, diagnostic),
        )
        self.assertEqual(
            0,
            result.returncode,
            "{} parser command exited {}: {}".format(
                name,
                result.returncode,
                diagnostic,
            ),
        )
        output = _decoded_output(result)
        self.assertTrue(
            output.strip(),
            "{} parser command returned no output".format(name),
        )
        return output

    def _version_and_help(self, name):
        executable = self._executable(name)
        version = self._run_parser(name, executable, ("--version",))
        self.assertTrue(
            version.strip(),
            "{} --version returned no version text".format(name),
        )
        return (
            executable,
            self._run_parser(name, executable, ("--help",)),
        )

    def _assert_options(self, name, output, executable, *options):
        diagnostic = self._sanitized(output, executable)
        for option in options:
            with self.subTest(command=name, option=option):
                self.assertIsNotNone(
                    _option_block(output, option),
                    "{} help omitted option {}: {}".format(
                        name,
                        option,
                        diagnostic,
                    ),
                )

    def _assert_option_values(
        self,
        name,
        output,
        executable,
        option,
        required_values,
    ):
        block = _option_block(output, option)
        self.assertIsNotNone(
            block,
            "{} help omitted option {}: {}".format(
                name,
                option,
                self._sanitized(output, executable),
            ),
        )
        documented = _documented_values(block)
        missing = set(required_values) - documented
        self.assertFalse(
            missing,
            "{} {} block omitted documented values {}: {}".format(
                name,
                option,
                sorted(missing),
                self._sanitized(block, executable),
            ),
        )

    def test_help_block_parser_rejects_unrelated_false_positives(self):
        help_text = """
Options:
  --input-format <format>  Input choices: "text", "stream-json"
  --other-format <format>  Output choices: "json"
  --fallback <value>       Refer to --input-format and json elsewhere
"""
        block = _option_block(help_text, "--input-format")

        self.assertEqual({"text", "stream-json"}, _documented_values(block))
        self.assertNotIn("json", _documented_values(block))
        self.assertIsNone(
            _option_block(
                "  --fallback <value> Refer to --input-format in prose",
                "--input-format",
            )
        )
        self.assertIsNone(
            _option_block(
                "  --other <value>  Only works with\n"
                "                     --output-format=stream-json)",
                "--output-format",
            )
        )

    def test_claude_help_supports_send_plan_contract(self):
        executable, help_text = self._version_and_help("claude")

        self._assert_options(
            "claude",
            help_text,
            executable,
            "--print",
            "--resume",
            "--input-format",
            "--output-format",
        )
        self._assert_option_values(
            "claude",
            help_text,
            executable,
            "--input-format",
            {"text", "stream-json"},
        )
        self._assert_option_values(
            "claude",
            help_text,
            executable,
            "--output-format",
            {"text", "json", "stream-json"},
        )

    def test_codex_exec_resume_help_supports_send_plan_contract(self):
        executable, top_help = self._version_and_help("codex")
        exec_help = self._run_parser("codex", executable, ("exec", "--help"))
        resume_help = self._run_parser(
            "codex",
            executable,
            ("exec", "resume", "--help"),
        )

        self.assertRegex(
            top_help,
            re.compile(r"(?im)^\s*exec(?:\s|$)"),
            "codex help omitted exec: {}".format(
                self._sanitized(top_help, executable)
            ),
        )
        self.assertRegex(
            exec_help,
            re.compile(r"(?im)^\s*resume(?:\s|$)"),
            "codex exec help omitted resume: {}".format(
                self._sanitized(exec_help, executable)
            ),
        )
        self._assert_options(
            "codex exec resume",
            resume_help,
            executable,
            "--json",
        )
        self.assertRegex(
            resume_help,
            re.compile(r"(?i)\bexec\s+resume\b"),
            "codex resume help omitted its command path: {}".format(
                self._sanitized(resume_help, executable)
            ),
        )
        prompt_block = _argument_block(resume_help, "PROMPT")
        self.assertIsNotNone(
            prompt_block,
            "codex resume help omitted its prompt argument: {}".format(
                self._sanitized(resume_help, executable),
            ),
        )
        self.assertRegex(
            prompt_block,
            re.compile(r"(?:^|\s)[`'\"]?-[`'\"]?(?:\s|$)", re.IGNORECASE),
            "codex prompt argument does not document '-' input: {}".format(
                self._sanitized(prompt_block, executable)
            ),
        )
        self.assertRegex(
            prompt_block,
            re.compile(r"\bstdin\b", re.IGNORECASE),
            "codex prompt argument does not bind '-' to stdin: {}".format(
                self._sanitized(prompt_block, executable)
            ),
        )

    def test_cursor_agent_help_supports_send_plan_contract(self):
        executable, help_text = self._version_and_help("cursor-agent")

        self._assert_options(
            "cursor-agent",
            help_text,
            executable,
            "--print",
            "--resume",
            "--output-format",
        )
        self._assert_option_values(
            "cursor-agent",
            help_text,
            executable,
            "--output-format",
            {"text", "json", "stream-json"},
        )
        prompt_block = _argument_block(help_text, "prompt")
        self.assertIsNotNone(
            prompt_block,
            "cursor-agent help does not expose a positional prompt: {}".format(
                self._sanitized(help_text, executable)
            ),
        )


if __name__ == "__main__":
    unittest.main()
