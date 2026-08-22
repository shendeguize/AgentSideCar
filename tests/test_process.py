import json
import subprocess
import unittest
from unittest import mock

from sidecar.process import (
    _linux_pid_cwd,
    _macos_pid_cwd,
    parse_ps_output,
    running_agent_processes,
)


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


if __name__ == "__main__":
    unittest.main()
