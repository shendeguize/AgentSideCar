import dataclasses
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sidecar.process import (
    _NodeKimiClassification,
    ProcessInspectionError,
    _candidate_token_identity,
    _classify_node_kimi_candidate,
    _linux_pid_cwd,
    _macos_pid_cwd,
    _path_identity,
    _strict_candidate_cwd,
    _strict_command_tokens,
    _strict_linux_cwd_identity,
    _strict_linux_pid_cwd,
    _strict_macos_cwd_identity,
    _strict_macos_pid_cwd,
    _strict_process_rows,
    assert_no_live_kimi_in_project,
    parse_ps_output,
    running_agent_processes,
)
from sidecar.process_runner import BoundedProcessResult


class ParsePsOutputTests(unittest.TestCase):
    def test_detects_supported_executable_basenames(self):
        ps_text = """\
101 00:01 /usr/local/bin/claude --resume one
102 02:03 codex exec
103 1-02:03:04 /opt/tools/cursor-agent --mode cli
104 00:04 /usr/bin/cursor agent
105 00:05 copilot --headless
106 00:06 /home/me/bin/dsh run
107 00:07 kimi --print hello
"""
        cwds = {pid: "/work/{}".format(pid) for pid in range(101, 108)}

        records = parse_ps_output(ps_text, cwd_lookup=cwds.__getitem__)

        self.assertEqual(
            [record["exe"] for record in records],
            ["claude", "codex", "cursor-agent", "cursor", "copilot", "dsh", "kimi"],
        )
        self.assertEqual(records[0]["pid"], 101)
        self.assertEqual(records[0]["etime"], "00:01")
        self.assertEqual(records[-1]["cwd"], "/work/107")
        json.dumps(records)

    def test_ignores_wrappers_and_sidecar_command_text(self):
        ps_text = """\
201 00:01 /usr/bin/ssh build-host claude --resume one
202 00:02 /bin/zsh -lc 'codex exec'
203 00:03 /bin/bash /tmp/cursor-agent-wrapper
204 00:04 /usr/bin/env copilot --headless
205 00:05 /usr/bin/python3 /repo/agent_sidecar.py ps kimi dsh
206 00:06 agent-sidecar ps --fixture claude
207 00:07 /Applications/Cursor.app/Contents/MacOS/Cursor
not-a-pid 00:08 claude
208 only-two-columns
"""

        self.assertEqual(parse_ps_output(ps_text), [])

    def test_replaces_invalid_utf8_without_losing_valid_record(self):
        ps_bytes = b"301 00:01 /usr/bin/kimi --prompt bad-\xff-value\n"

        records = parse_ps_output(ps_bytes, cwd_lookup=lambda _pid: b"/tmp/\xfe")

        self.assertEqual(records[0]["exe"], "kimi")
        self.assertIn("\ufffd", records[0]["cmd"])
        self.assertEqual(records[0]["cwd"], "/tmp/\ufffd")
        json.dumps(records, ensure_ascii=False).encode("utf-8")

    def test_cwd_lookup_failure_becomes_empty_string(self):
        def unavailable(_pid):
            raise OSError("process exited")

        records = parse_ps_output("401 00:01 claude", cwd_lookup=unavailable)

        self.assertEqual(records[0]["cwd"], "")


class CwdLookupTests(unittest.TestCase):
    @mock.patch("sidecar.process.os.readlink")
    def test_linux_reads_proc_cwd(self, readlink):
        readlink.return_value = "/tmp/project"

        self.assertEqual(_linux_pid_cwd(77), "/tmp/project")
        readlink.assert_called_once_with("/proc/77/cwd")

    @mock.patch("sidecar.process.os.readlink", side_effect=FileNotFoundError)
    def test_linux_failure_returns_empty_cwd(self, _readlink):
        self.assertEqual(_linux_pid_cwd(78), "")

    @mock.patch("sidecar.process.subprocess.run")
    def test_macos_reads_lsof_name_field(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"p79\nfcwd\nn/tmp/mac-project\n"
        )

        self.assertEqual(_macos_pid_cwd(79), "/tmp/mac-project")
        self.assertEqual(
            run.call_args.args[0],
            ["lsof", "-a", "-p", "79", "-d", "cwd", "-Fn"],
        )

    @mock.patch("sidecar.process.subprocess.run", side_effect=FileNotFoundError)
    def test_macos_without_lsof_returns_empty_cwd(self, _run):
        self.assertEqual(_macos_pid_cwd(80), "")


class RunningAgentProcessesTests(unittest.TestCase):
    @mock.patch("sidecar.process._pid_cwd", return_value="/tmp/project")
    @mock.patch("sidecar.process.subprocess.run")
    def test_ps_execution_uses_byte_safe_parser(self, run, _cwd):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"501 00:09 /opt/bin/claude --x \xff\n"
        )

        records = running_agent_processes()

        self.assertEqual(records[0]["pid"], 501)
        self.assertIn("\ufffd", records[0]["cmd"])

    @mock.patch("sidecar.process.subprocess.run", side_effect=FileNotFoundError)
    def test_missing_ps_returns_empty_list(self, _run):
        self.assertEqual(running_agent_processes(), [])


class _Identity:
    def __init__(self, path):
        canonical = os.path.realpath(str(path))
        metadata = os.stat(canonical)
        self.canonical_path = canonical
        self.dev = metadata.st_dev
        self.ino = metadata.st_ino


class StrictKimiOwnerGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.project = root / "project"
        self.other_project = root / "other"
        self.project.mkdir()
        self.other_project.mkdir()
        self.executable = root / "kimi"
        self.executable.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        self.executable.chmod(0o700)
        self.project_identity = _Identity(self.project)
        self.executable_identity = _Identity(self.executable)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _cwd_record(path):
        descriptor = os.open(str(path), os.O_RDONLY)
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino, descriptor

    def test_same_project_kimi_is_busy_but_other_project_is_not(self):
        rows = [(101, 101, "kimi", "/usr/local/bin/kimi acp")]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.project),
        ):
            with self.assertRaises(ProcessInspectionError) as raised:
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )
        self.assertEqual("session_busy", raised.exception.code)
        self.assertEqual("session_busy", str(raised.exception))

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.other_project),
        ):
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )

    def test_node_shebang_process_resolves_bound_kimi_script(self):
        rows = [
            (
                102,
                102,
                "/usr/local/bin/node",
                "/usr/local/bin/node {} acp".format(self.executable),
            )
        ]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.project),
        ):
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )

        option_commands = (
            "node --require preload {} acp".format(self.executable),
            "node --require={} other.mjs".format(self.executable),
        )
        for command in option_commands:
            with self.subTest(command=command), mock.patch(
                "sidecar.process._strict_process_rows",
                return_value=[(102, 102, "node", command)],
            ), mock.patch(
                "sidecar.process._strict_candidate_cwd",
                side_effect=lambda *_args: self._cwd_record(self.project),
            ):
                with self.assertRaises(ProcessInspectionError):
                    assert_no_live_kimi_in_project(
                        self.project_identity,
                        self.executable_identity,
                    )

        relative_rows = [
            (102, 102, "node", "node ../kimi acp"),
        ]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=relative_rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.project),
        ):
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )

        command_argv0_rows = [
            (102, 102, "MainThread", "/usr/local/bin/kimi acp"),
        ]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=command_argv0_rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.project),
        ):
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )

    def test_executable_nlink_is_revalidated_after_scan(self):
        alias = self.project / "late-hardlink"

        def rows(_deadline):
            os.link(self.executable, alias)
            return []

        try:
            with mock.patch(
                "sidecar.process._strict_process_rows",
                side_effect=rows,
            ):
                with self.assertRaises(ProcessInspectionError):
                    assert_no_live_kimi_in_project(
                        self.project_identity,
                        self.executable_identity,
                    )
        finally:
            if alias.exists():
                alias.unlink()

    def test_executable_binding_rejects_unsafe_owner_mode(self):
        self.executable.chmod(0o722)
        with mock.patch(
            "sidecar.process._strict_process_rows",
        ) as process_rows:
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )
        process_rows.assert_not_called()

    def test_unrelated_node_script_is_not_a_kimi_candidate(self):
        unrelated = Path(self.temporary.name) / "other.mjs"
        unrelated.write_text("", encoding="utf-8")
        rows = [
            (103, 103, "node", "node {}".format(unrelated)),
            (105, 105, "node", "node /tmp/kimiko"),
            (106, 106, "node", "node /tmp/kimiko-code/main.mjs"),
        ]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=AssertionError("ordinary Node cwd was inspected"),
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_not_called()

    def test_relative_node_script_uses_anchored_cwd_identity(self):
        ordinary = self.project / "ordinary.mjs"
        ordinary.write_text("", encoding="utf-8")
        rows = [(104, 104, "ce", "node ordinary.mjs")]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.project),
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_called_once()

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=ProcessInspectionError(),
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_called_once()

    def test_node_classification_is_explicit_and_option_aware(self):
        unrelated = Path(self.temporary.name) / "absolute-other.mjs"
        unrelated.write_text("", encoding="utf-8")
        deadline = time.monotonic() + 1
        cases = (
            (
                "node {}".format(unrelated),
                _NodeKimiClassification.ORDINARY,
                None,
            ),
            (
                "node {}".format(self.executable),
                _NodeKimiClassification.DEFINITE,
                None,
            ),
            (
                "node relative-main.mjs",
                _NodeKimiClassification.NEEDS_CWD_IDENTITY,
                ("relative-main.mjs",),
            ),
            (
                "node --require preload.mjs relative-main.mjs",
                _NodeKimiClassification.NEEDS_CWD_IDENTITY,
                ("preload.mjs", "relative-main.mjs"),
            ),
        )
        identity = (
            self.executable_identity.dev,
            self.executable_identity.ino,
        )
        for command, expected, script in cases:
            with self.subTest(command=command):
                self.assertEqual(
                    (expected, script),
                    _classify_node_kimi_candidate(
                        command,
                        identity,
                        deadline,
                    ),
                )

    def test_relative_hardlink_and_symlink_alias_match_bound_kimi(self):
        hardlink = self.project / "hardlink-alias.mjs"
        symlink = self.project / "symlink-alias.mjs"
        os.link(self.executable, hardlink)
        with mock.patch(
            "sidecar.process._strict_process_rows",
        ) as process_rows:
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )
        process_rows.assert_not_called()
        hardlink.unlink()

        symlink.symlink_to(self.executable)
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[
                (110, 110, "node", "node {}".format(symlink.name))
            ],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.project),
        ):
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[
                (110, 110, "node", "node {}".format(symlink.name))
            ],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=ProcessInspectionError(),
        ):
            # With nlink == 1, a deleted cwd cannot prove whether this
            # same-UID relative symlink disguised Kimi, so it must not turn
            # every unrelated Node process into a global blocker.
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )

    def test_node_option_values_and_later_tokens_are_all_scanned(self):
        preload = self.project / "preload.mjs"
        ordinary = self.project / "ordinary-main.mjs"
        alias = self.project / "main-alias.mjs"
        preload.write_text("", encoding="utf-8")
        alias.symlink_to(self.executable)
        ordinary.write_text("", encoding="utf-8")

        no_main = [(111, 111, "node", "node --require preload.mjs")]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=no_main,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.project),
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_called_once()

        ordinary_main = [
            (
                112,
                112,
                "node",
                "node --require preload.mjs ordinary-main.mjs",
            )
        ]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=ordinary_main,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.project),
        ):
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )

        alias_commands = (
            "node --require main-alias.mjs",
            "node --require ordinary-main.mjs main-alias.mjs",
            "node -r./main-alias.mjs",
            "node --require=./main-alias.mjs",
            "node --import=./main-alias.mjs",
            "node --loader=./main-alias.mjs",
            "node --experimental-loader=./main-alias.mjs",
            "node --loader={}".format(alias),
            "node --inspect-port 9230 main-alias.mjs",
            "node --inspect-port=9230 main-alias.mjs",
            "node --loader preload.mjs main-alias.mjs",
            "node -r preload.mjs main-alias.mjs",
            "node -rpreload.mjs main-alias.mjs",
            "node -p expression main-alias.mjs",
        )
        for command in alias_commands:
            with self.subTest(command=command), mock.patch(
                "sidecar.process._strict_process_rows",
                return_value=[(113, 113, "node", command)],
            ), mock.patch(
                "sidecar.process._strict_candidate_cwd",
                side_effect=lambda *_args: self._cwd_record(self.project),
            ):
                with self.assertRaises(ProcessInspectionError):
                    assert_no_live_kimi_in_project(
                        self.project_identity,
                        self.executable_identity,
                    )

        ordinary_attached = (
            "node -r./preload.mjs",
            "node --require=./preload.mjs",
            "node --import=./preload.mjs",
            "node --loader=./preload.mjs",
            "node --experimental-loader=./preload.mjs",
        )
        for command in ordinary_attached:
            with self.subTest(command=command), mock.patch(
                "sidecar.process._strict_process_rows",
                return_value=[(114, 114, "node", command)],
            ), mock.patch(
                "sidecar.process._strict_candidate_cwd",
                side_effect=lambda *_args: self._cwd_record(self.project),
            ) as cwd_lookup:
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )
            cwd_lookup.assert_called_once()

        absolute_ordinary = "node --loader={}".format(preload)
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[(114, 114, "node", absolute_ordinary)],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=AssertionError("absolute ordinary path read cwd"),
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_not_called()

        code_commands = (
            "node -e./main-alias.mjs",
            "node -p ./main-alias.mjs",
            "node --eval=./main-alias.mjs",
            "node --print ./main-alias.mjs",
        )
        for command in code_commands:
            with self.subTest(command=command), mock.patch(
                "sidecar.process._strict_process_rows",
                return_value=[(115, 115, "node", command)],
            ), mock.patch(
                "sidecar.process._strict_candidate_cwd",
                side_effect=AssertionError("eval code was treated as a path"),
            ) as cwd_lookup:
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )
            cwd_lookup.assert_not_called()

    def test_node_terminator_scans_dash_prefixed_file_tokens(self):
        alias = self.project / "-alias.mjs"
        ordinary = self.project / "-ordinary.mjs"
        alias.symlink_to(self.executable)
        ordinary.write_text("", encoding="utf-8")

        bound_commands = (
            "node -- -alias.mjs",
            "node -- {}".format(alias),
        )
        for command in bound_commands:
            with self.subTest(command=command), mock.patch(
                "sidecar.process._strict_process_rows",
                return_value=[(116, 116, "node", command)],
            ), mock.patch(
                "sidecar.process._strict_candidate_cwd",
                side_effect=lambda *_args: self._cwd_record(self.project),
            ):
                with self.assertRaises(ProcessInspectionError):
                    assert_no_live_kimi_in_project(
                        self.project_identity,
                        self.executable_identity,
                    )

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[(117, 117, "node", "node -- -ordinary.mjs")],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.project),
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_called_once()

        absolute_ordinary = "node -- {}".format(ordinary)
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[(118, 118, "node", absolute_ordinary)],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=AssertionError("absolute ordinary path read cwd"),
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_not_called()

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[(119, 119, "node", "node --")],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_not_called()

    def test_kimi_node_candidates_keep_cwd_fail_closed(self):
        commands = (
            "node {}".format(self.executable),
            "node --require={} ordinary.mjs".format(self.executable),
            "node /missing/kimi",
            "node /packages/kimi-code/main.mjs",
        )
        for command in commands:
            with self.subTest(command=command), mock.patch(
                "sidecar.process._strict_process_rows",
                return_value=[(107, 107, "node", command)],
            ), mock.patch(
                "sidecar.process._strict_candidate_cwd",
                side_effect=ProcessInspectionError(),
            ) as cwd_lookup:
                with self.assertRaises(ProcessInspectionError):
                    assert_no_live_kimi_in_project(
                        self.project_identity,
                        self.executable_identity,
                    )
            cwd_lookup.assert_called_once()

    def test_malformed_node_only_blocks_with_exact_kimi_indicator(self):
        ordinary_commands = (
            "node 'unterminated",
            "node unreadable-\udcff",
            "node '/missing/kimiko",
        )
        for command in ordinary_commands:
            with self.subTest(command=command), mock.patch(
                "sidecar.process._strict_process_rows",
                return_value=[(108, 108, "node", command)],
            ), mock.patch(
                "sidecar.process._strict_candidate_cwd",
                side_effect=AssertionError("ordinary Node cwd was inspected"),
            ) as cwd_lookup:
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )
            cwd_lookup.assert_not_called()

        suspicious_commands = (
            "node '/missing/kimi",
            "node '/packages/kimi-code/main.mjs",
            "kimi unreadable-\udcff",
        )
        for command in suspicious_commands:
            with self.subTest(command=command), mock.patch(
                "sidecar.process._strict_process_rows",
                return_value=[(109, 109, "node", command)],
            ), mock.patch(
                "sidecar.process._strict_candidate_cwd",
                side_effect=ProcessInspectionError(),
            ) as cwd_lookup:
                with self.assertRaises(ProcessInspectionError):
                    assert_no_live_kimi_in_project(
                        self.project_identity,
                        self.executable_identity,
                    )
            cwd_lookup.assert_called_once()

    def test_ps_and_candidate_cwd_failures_fail_closed(self):
        with mock.patch(
            "sidecar.process._strict_process_rows",
            side_effect=ProcessInspectionError(),
        ):
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[(104, 104, "kimi", "kimi acp")],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=ProcessInspectionError(),
        ):
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[(104, 104, "kimi", "kimi acp")],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=ProcessInspectionError(),
        ):
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )

    def test_excluded_owned_process_group_is_the_only_exemption(self):
        rows = [
            (105, 700, "kimi", "kimi acp"),
            (106, 701, "kimi", "kimi acp"),
        ]

        def cwd_lookup(pid, _deadline):
            if pid == 105:
                raise AssertionError("excluded process was inspected")
            return self._cwd_record(self.project)

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=cwd_lookup,
        ):
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                    excluded_process_group=700,
                )

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[rows[0]],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd"
        ) as strict_cwd:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
                excluded_process_group=700,
            )
        strict_cwd.assert_not_called()

        for invalid_group in (0, -1, True, "700"):
            with self.subTest(invalid_group=invalid_group):
                with self.assertRaises(ProcessInspectionError):
                    assert_no_live_kimi_in_project(
                        self.project_identity,
                        self.executable_identity,
                        excluded_process_group=invalid_group,
                    )

    def test_candidate_budget_and_global_deadline_fail_closed(self):
        rows = [
            (pid, pid, "node", "node /missing/kimi")
            for pid in range(1000, 1065)
        ]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=lambda *_args: self._cwd_record(self.other_project),
        ) as cwd_lookup:
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )
        self.assertEqual(64, cwd_lookup.call_count)

        clock = iter((10.0, 12.1))
        with mock.patch(
            "sidecar.process.time.monotonic",
            side_effect=lambda: next(clock),
        ), mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[],
        ):
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )

    def test_realistic_node_token_counts_skip_ordinary_and_find_late_kimi(self):
        ordinary_command = " ".join(
            ["node"]
            + ["--ordinary-{}".format(index) for index in range(294)]
        )
        self.assertEqual(295, len(ordinary_command.split()))
        ordinary_rows = [
            (pid, pid, "node", ordinary_command)
            for pid in range(2000, 2006)
        ]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=ordinary_rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=AssertionError("ordinary Node became a candidate"),
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_not_called()

        late_kimi = " ".join(
            ["node"]
            + ["--ordinary-{}".format(index) for index in range(293)]
            + ["/missing/kimi"]
        )
        self.assertEqual(295, len(late_kimi.split()))
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[(2007, 2007, "node", late_kimi)],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=ProcessInspectionError(),
        ) as cwd_lookup:
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )
        cwd_lookup.assert_called_once()

    def test_node_per_command_and_global_token_budgets_are_bounded(self):
        at_limit = " ".join(
            ["node"]
            + ["--ordinary-{}".format(index) for index in range(4095)]
        )
        over_limit = at_limit + " --ordinary-over-limit"
        hinted_over_limit = at_limit + " /missing/kimi"

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[(2100, 2100, "node", over_limit)],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_not_called()

        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=[(2101, 2101, "node", hinted_over_limit)],
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
        ) as cwd_lookup:
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )
        cwd_lookup.assert_not_called()

        rows = [
            (pid, pid, "node", at_limit)
            for pid in range(2200, 2205)
        ]
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
        ) as cwd_lookup:
            with self.assertRaises(ProcessInspectionError):
                assert_no_live_kimi_in_project(
                    self.project_identity,
                    self.executable_identity,
                )
        cwd_lookup.assert_not_called()

    def test_node_token_inspection_uncertainty_fails_closed(self):
        commands = (
            "node /uncertain-token",
            "node '/uncertain-token",
        )
        for command in commands:
            with self.subTest(command=command), mock.patch(
                "sidecar.process._strict_process_rows",
                return_value=[(1200, 1200, "node", command)],
            ), mock.patch(
                "sidecar.process._strict_candidate_cwd",
                side_effect=lambda *_args: self._cwd_record(self.project),
            ), mock.patch(
                "sidecar.process._candidate_token_identity",
                side_effect=ProcessInspectionError(),
            ):
                with self.assertRaises(ProcessInspectionError):
                    assert_no_live_kimi_in_project(
                        self.project_identity,
                        self.executable_identity,
                    )

    @mock.patch("sidecar.process.run_bounded")
    def test_strict_ps_is_bounded_and_rejects_overflow(self, run_bounded):
        run_bounded.return_value = BoundedProcessResult(
            args=("ps",),
            returncode=0,
            stdout=b"201 201 kimi kimi acp\n",
            stderr=b"",
        )
        self.assertEqual(
            [(201, 201, "kimi", "kimi acp")],
            _strict_process_rows(),
        )
        kwargs = run_bounded.call_args.kwargs
        self.assertEqual(1024 * 1024, kwargs["stdout_limit"])
        self.assertEqual(4096, kwargs["stderr_limit"])
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 2.0)
        self.assertEqual(
            {
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            },
            kwargs["env"],
        )

        run_bounded.return_value = BoundedProcessResult(
            args=("ps",),
            returncode=0,
            stdout=b"",
            stderr=b"",
            overflow="stdout",
        )
        with self.assertRaises(ProcessInspectionError):
            _strict_process_rows()

    @mock.patch("sidecar.process.run_bounded")
    def test_strict_ps_rejects_unsafe_candidate_rows_and_encoding(
        self,
        run_bounded,
    ):
        structurally_unsafe_kimi = (
            b"not-enough kimi\n",
            b"pid pgid kimi kimi acp\n",
        )
        for output in structurally_unsafe_kimi:
            with self.subTest(output=output):
                run_bounded.return_value = BoundedProcessResult(
                    args=("ps",),
                    returncode=0,
                    stdout=output,
                    stderr=b"",
                )
                with self.assertRaises(ProcessInspectionError):
                    _strict_process_rows()

        candidate_outputs = (
            b"201 201 kimi 'unterminated\n",
            b"201 201 kimi kimi \xff\n",
        )
        for output in candidate_outputs:
            with self.subTest(output=output):
                run_bounded.return_value = BoundedProcessResult(
                    args=("ps",),
                    returncode=0,
                    stdout=output,
                    stderr=b"",
                )
                rows = _strict_process_rows()
                with mock.patch(
                    "sidecar.process._strict_process_rows",
                    return_value=rows,
                ), mock.patch(
                    "sidecar.process._strict_candidate_cwd",
                    side_effect=ProcessInspectionError(),
                ):
                    with self.assertRaises(ProcessInspectionError):
                        assert_no_live_kimi_in_project(
                            self.project_identity,
                            self.executable_identity,
                        )

        run_bounded.return_value = BoundedProcessResult(
            args=("ps",),
            returncode=0,
            stdout=(
                b"bad unrelated\n"
                b"1 -2 node node script.mjs\n"
                b"201 201 node node ordinary-\xff\n"
                b"202 202 python python -c pass\n"
            ),
            stderr=b"",
        )
        ordinary_rows = _strict_process_rows()
        self.assertEqual(
            [
                (201, 201, "node", "node ordinary-\udcff"),
                (202, 202, "python", "python -c pass"),
            ],
            ordinary_rows,
        )
        with mock.patch(
            "sidecar.process._strict_process_rows",
            return_value=ordinary_rows,
        ), mock.patch(
            "sidecar.process._strict_candidate_cwd",
            side_effect=AssertionError("ordinary Node cwd was inspected"),
        ) as cwd_lookup:
            assert_no_live_kimi_in_project(
                self.project_identity,
                self.executable_identity,
            )
        cwd_lookup.assert_not_called()

    def test_strict_cwd_and_command_helpers_fail_closed(self):
        with mock.patch(
            "sidecar.process.os.readlink",
            return_value=str(self.project),
        ):
            self.assertEqual(
                str(self.project),
                _strict_linux_pid_cwd(301),
            )
        for value in ("", b"/tmp/project"):
            with self.subTest(value=value), mock.patch(
                "sidecar.process.os.readlink",
                return_value=value,
            ):
                with self.assertRaises(ProcessInspectionError):
                    _strict_linux_pid_cwd(302)
        with mock.patch(
            "sidecar.process.os.readlink",
            side_effect=OSError("gone"),
        ):
            with self.assertRaises(ProcessInspectionError):
                _strict_linux_pid_cwd(303)

        good = BoundedProcessResult(
            args=("lsof",),
            returncode=0,
            stdout=b"p304\nfcwd\nn/tmp/project\n",
            stderr=b"",
        )
        with mock.patch("sidecar.process._strict_run", return_value=good):
            self.assertEqual("/tmp/project", _strict_macos_pid_cwd(304))
        bad_values = (
            b"p305\nfcwd\n",
            b"n/one\nn/two\n",
            b"n/bad-\xff\n",
        )
        for output in bad_values:
            with self.subTest(output=output), mock.patch(
                "sidecar.process._strict_run",
                return_value=BoundedProcessResult(
                    args=("lsof",),
                    returncode=0,
                    stdout=output,
                    stderr=b"",
                ),
            ):
                with self.assertRaises(ProcessInspectionError):
                    _strict_macos_pid_cwd(305)

        self.assertEqual(
            ("node", "--inspect", "script.mjs"),
            _strict_command_tokens("node --inspect script.mjs"),
        )
        with self.assertRaises(ProcessInspectionError):
            _strict_command_tokens("'unterminated")

        self.assertEqual(
            (self.executable_identity.dev, self.executable_identity.ino),
            _path_identity(str(self.executable)),
        )
        with self.assertRaises(ProcessInspectionError):
            _path_identity(str(self.project))
        with self.assertRaises(ProcessInspectionError):
            _path_identity(str(Path(self.temporary.name) / "missing"))

        cwd_descriptor = os.open(str(self.project), os.O_RDONLY)
        try:
            relative = self.project / "relative.mjs"
            relative.write_text("", encoding="utf-8")
            relative_identity = _Identity(relative)
            self.assertEqual(
                (relative_identity.dev, relative_identity.ino),
                _candidate_token_identity("relative.mjs", cwd_descriptor),
            )
            self.assertIsNone(
                _candidate_token_identity("missing.mjs", cwd_descriptor)
            )
            self.assertIsNone(_candidate_token_identity("--", cwd_descriptor))
        finally:
            os.close(cwd_descriptor)

    def test_os_cwd_identity_rejects_path_replacement_and_missing_fields(self):
        project_metadata = os.stat(self.project)
        output = (
            b"p1300\0\nfcwd\0D"
            + hex(project_metadata.st_dev).encode("ascii")
            + b"\0i"
            + str(project_metadata.st_ino).encode("ascii")
            + b"\0n"
            + str(self.project).encode("utf-8")
            + b"\0\n"
        )
        completed = BoundedProcessResult(
            args=("lsof",),
            returncode=0,
            stdout=output,
            stderr=b"",
        )
        with mock.patch(
            "sidecar.process._strict_run",
            return_value=completed,
        ):
            dev, ino, descriptor = _strict_macos_cwd_identity(
                1300,
                time.monotonic() + 1,
            )
        try:
            self.assertEqual(
                (project_metadata.st_dev, project_metadata.st_ino),
                (dev, ino),
            )
        finally:
            os.close(descriptor)

        real_open = os.open

        def swapped_open(_path, flags):
            return real_open(str(self.other_project), flags)

        with mock.patch(
            "sidecar.process._strict_run",
            return_value=completed,
        ), mock.patch(
            "sidecar.process.os.open",
            side_effect=swapped_open,
        ):
            with self.assertRaises(ProcessInspectionError):
                _strict_macos_cwd_identity(
                    1300,
                    time.monotonic() + 1,
                )

        missing_identity = dataclasses.replace(
            completed,
            stdout=b"p1300\0\nfcwd\0n/tmp\0\n",
        )
        with mock.patch(
            "sidecar.process._strict_run",
            return_value=missing_identity,
        ):
            with self.assertRaises(ProcessInspectionError):
                _strict_macos_cwd_identity(
                    1300,
                    time.monotonic() + 1,
                )

        project_fd = os.open(str(self.project), os.O_RDONLY)
        try:
            with mock.patch(
                "sidecar.process.os.open",
                return_value=os.dup(project_fd),
            ):
                linux_dev, linux_ino, linux_fd = _strict_linux_cwd_identity(
                    1301
                )
            try:
                self.assertEqual(
                    (project_metadata.st_dev, project_metadata.st_ino),
                    (linux_dev, linux_ino),
                )
            finally:
                os.close(linux_fd)
        finally:
            os.close(project_fd)

    def test_candidate_cwd_deadline_failure_never_leaks_descriptor(self):
        baseline = len(os.listdir("/dev/fd"))
        opened = []
        remaining_calls = {"count": 0}

        def identity(_pid):
            descriptor = os.open(str(self.project), os.O_RDONLY)
            opened.append(descriptor)
            metadata = os.fstat(descriptor)
            return metadata.st_dev, metadata.st_ino, descriptor

        def remaining(_deadline):
            remaining_calls["count"] += 1
            if remaining_calls["count"] % 2 == 0:
                raise ProcessInspectionError()
            return 1.0

        with mock.patch(
            "sidecar.process.sys.platform",
            "linux",
        ), mock.patch(
            "sidecar.process._strict_linux_cwd_identity",
            side_effect=identity,
        ), mock.patch(
            "sidecar.process._inspection_remaining",
            side_effect=remaining,
        ):
            for _index in range(100):
                with self.assertRaises(ProcessInspectionError):
                    _strict_candidate_cwd(1400, 1.0)

        self.assertEqual(100, len(opened))
        self.assertEqual(baseline, len(os.listdir("/dev/fd")))
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_invalid_bound_identity_is_stable_busy(self):
        invalid_values = (
            object(),
            mock.Mock(dev=True, ino=1, canonical_path="/tmp"),
            mock.Mock(dev=1, ino=0, canonical_path="/tmp"),
            mock.Mock(dev=1, ino=1, canonical_path=b"/tmp"),
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ProcessInspectionError) as raised:
                    assert_no_live_kimi_in_project(
                        value,
                        self.executable_identity,
                    )
                self.assertEqual("session_busy", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
