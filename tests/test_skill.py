import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "install-skill.sh"
SKILL_DIR = REPO_ROOT / "skills" / "agent-sidecar"
SKILL_FILE = SKILL_DIR / "SKILL.md"
REFERENCE_FILE = SKILL_DIR / "reference.md"
CLI_SOURCE = REPO_ROOT / "agent-sidecar"


def run_installer(home, *arguments):
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    return subprocess.run(
        ["/bin/sh", str(INSTALLER), *arguments],
        cwd=str(REPO_ROOT),
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
    )


def installed_paths(home):
    return (
        home / ".cursor" / "skills" / "agent-sidecar",
        home / ".claude" / "skills" / "agent-sidecar",
        home / ".dsh" / "skills" / "agent-sidecar",
        home / ".local" / "bin" / "agent-sidecar",
    )


class SkillFormatTests(unittest.TestCase):
    def test_frontmatter_is_valid_discoverable_and_concise(self):
        lines = SKILL_FILE.read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), 500)
        self.assertEqual("---", lines[0])
        closing = lines.index("---", 1)

        metadata = {}
        for line in lines[1:closing]:
            key, separator, value = line.partition(":")
            self.assertEqual(":", separator)
            self.assertRegex(key, r"^[a-z][a-z0-9-]*$")
            self.assertTrue(value.strip())
            metadata[key] = value.strip()

        self.assertEqual("agent-sidecar", metadata["name"])
        self.assertRegex(metadata["name"], r"^[a-z0-9-]{1,64}$")
        description = metadata["description"]
        self.assertLessEqual(len(description), 1024)
        self.assertTrue(description.startswith("Monitors "))
        for trigger in (
            "agent status",
            "session progress",
            "monitor agents",
            "which agent is waiting or working",
        ):
            self.assertIn(trigger, description.lower())

    def test_frontmatter_and_body_stay_dsh_loader_compatible(self):
        text = SKILL_FILE.read_text(encoding="utf-8")
        lines = text.splitlines()
        closing = lines.index("---", 1)
        metadata = {}
        for line in lines[1:closing]:
            key, _, value = line.partition(":")
            metadata[key] = value.strip()

        # The dsh filesystem loader requires a kebab-case name plus a
        # description and rejects the legacy camelCase invocation keys.
        self.assertRegex(metadata["name"], r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertIn("description", metadata)
        self.assertEqual("true", metadata.get("user-invocable"))
        for legacy_key in (
            "disableModelInvocation",
            "modelInvocable",
            "userInvocable",
        ):
            self.assertNotIn(legacy_key, metadata)

        # DSH injection guidance routes through the plugin surface instead
        # of a direct send.
        self.assertIn("@shendeguize/dsh-agent-sidecar", text)
        self.assertIn("/plugins/agent-sidecar/api/action", text)
        self.assertIn("~/.dsh/skills/agent-sidecar", text)

    def test_reference_documents_daemon_replay_and_subscribe_protocol(self):
        reference_text = REFERENCE_FILE.read_text(encoding="utf-8")

        for token in (
            "`ping`",
            "`status`",
            "`subscribe`",
            "`replay`",
            "`agents`",
            "`session_id`",
            "`after_seq`",
            "`limit`",
            "`last_seq`",
            "`truncated`",
            "`replay_unsupported`",
            "`unknown_session`",
            "`invalid_request`",
            "`replay_failed`",
        ):
            self.assertIn(token, reference_text)
        self.assertIn("`1024`", reference_text)
        self.assertIn("`256`", reference_text)
        self.assertIn("truncated: false", reference_text)

    def test_reference_is_linked_one_level_deep_and_documents_schema(self):
        skill_text = SKILL_FILE.read_text(encoding="utf-8")
        reference_text = REFERENCE_FILE.read_text(encoding="utf-8")

        self.assertRegex(skill_text, re.escape("[reference.md](reference.md)"))
        self.assertTrue(REFERENCE_FILE.is_file())
        for field in (
            "agent",
            "session_id",
            "project",
            "transcript",
            "updated_at",
            "title",
            "status",
            "extra",
            "parent_id",
        ):
            self.assertIn("`{}`".format(field), reference_text)
        for status in ("working", "waiting", "idle", "dead"):
            self.assertIn("`{}`".format(status), reference_text)


class InstallerTests(unittest.TestCase):
    def test_install_creates_skill_and_cli_links_without_reserved_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            reserved = home / ".cursor" / "skills-cursor"
            reserved.mkdir(parents=True)
            sentinel = reserved / "keep.txt"
            sentinel.write_text("untouched", encoding="utf-8")

            result = run_installer(home)

            self.assertEqual(0, result.returncode, result.stderr)
            cursor_skill, claude_skill, dsh_skill, cli = installed_paths(home)
            for skill_link in (cursor_skill, claude_skill, dsh_skill):
                self.assertTrue(skill_link.is_symlink())
                self.assertEqual(SKILL_DIR.resolve(), skill_link.resolve())
            self.assertTrue(cli.is_symlink())
            self.assertEqual(CLI_SOURCE.resolve(), cli.resolve())
            self.assertTrue((dsh_skill / "SKILL.md").is_file())
            self.assertEqual("untouched", sentinel.read_text(encoding="utf-8"))

            cli_environment = {
                **os.environ,
                "HOME": str(home),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            cli_environment.pop("PYTHONPATH", None)
            help_result = subprocess.run(
                [str(cli), "--help"],
                cwd=str(home),
                env=cli_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, help_result.returncode, help_result.stderr)
            self.assertIn("agent-sidecar", help_result.stdout)

    def test_installed_cli_manages_daemon_from_arbitrary_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            runtime = home / "runtime"
            install = run_installer(home)
            self.assertEqual(0, install.returncode, install.stderr)
            cli = installed_paths(home)[3]
            environment = {
                **os.environ,
                "HOME": str(home),
                "AGENT_SIDECAR_RUNTIME_DIR": str(runtime),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            environment.pop("PYTHONPATH", None)

            def daemon_command(action):
                return subprocess.run(
                    [str(cli), "daemon", action],
                    cwd="/tmp",
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=10,
                )

            running = False
            try:
                start = daemon_command("start")
                running = start.returncode == 0
                self.assertEqual(0, start.returncode, start.stderr)
                self.assertIn("daemon started", start.stdout)

                status = daemon_command("status")
                self.assertEqual(0, status.returncode, status.stderr)
                self.assertIn("daemon is running", status.stdout)

                stop = daemon_command("stop")
                self.assertEqual(0, stop.returncode, stop.stderr)
                self.assertIn("daemon stopped", stop.stdout)
                running = False
            finally:
                if running:
                    daemon_command("stop")

            self.assertFalse((runtime / "daemon.sock").exists())
            self.assertFalse((runtime / "daemon.pid").exists())

    def test_install_is_idempotent_and_updates_only_repo_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            first = run_installer(home)
            self.assertEqual(0, first.returncode, first.stderr)
            links = installed_paths(home)
            inodes = tuple(path.lstat().st_ino for path in links)

            second = run_installer(home)

            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(
                inodes,
                tuple(path.lstat().st_ino for path in links),
            )

            cursor_skill = links[0]
            cursor_skill.unlink()
            cursor_skill.symlink_to(REPO_ROOT / "README.md")
            update = run_installer(home)
            self.assertEqual(0, update.returncode, update.stderr)
            self.assertEqual(SKILL_DIR.resolve(), cursor_skill.resolve())

    def test_uninstall_removes_only_links_into_this_repo(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            install = run_installer(home)
            self.assertEqual(0, install.returncode, install.stderr)
            cursor_skill, claude_skill, dsh_skill, cli = installed_paths(home)

            unrelated = home / "unrelated-skill"
            unrelated.mkdir()
            claude_skill.unlink()
            claude_skill.symlink_to(unrelated, target_is_directory=True)

            result = run_installer(home, "--uninstall")

            self.assertEqual(0, result.returncode, result.stderr)
            for removed in (cursor_skill, dsh_skill, cli):
                self.assertFalse(removed.exists())
                self.assertFalse(removed.is_symlink())
            self.assertTrue(claude_skill.is_symlink())
            self.assertEqual(unrelated.resolve(), claude_skill.resolve())

    def test_install_refuses_real_paths_and_unrelated_symlinks(self):
        for blocker_kind in ("file", "directory", "symlink"):
            with self.subTest(blocker_kind=blocker_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    home = Path(temporary)
                    cursor_skill, claude_skill, dsh_skill, cli = installed_paths(home)
                    cursor_skill.parent.mkdir(parents=True)

                    if blocker_kind == "file":
                        cursor_skill.write_text("keep", encoding="utf-8")
                    elif blocker_kind == "directory":
                        cursor_skill.mkdir()
                    else:
                        unrelated = home / "unrelated"
                        unrelated.mkdir()
                        cursor_skill.symlink_to(unrelated, target_is_directory=True)

                    result = run_installer(home)

                    self.assertNotEqual(0, result.returncode)
                    self.assertIn("refusing", result.stderr)
                    if blocker_kind == "file":
                        self.assertEqual("keep", cursor_skill.read_text(encoding="utf-8"))
                    elif blocker_kind == "directory":
                        self.assertTrue(cursor_skill.is_dir())
                        self.assertFalse(cursor_skill.is_symlink())
                    else:
                        self.assertTrue(cursor_skill.is_symlink())
                        self.assertEqual(unrelated.resolve(), cursor_skill.resolve())
                    for untouched in (claude_skill, dsh_skill, cli):
                        self.assertFalse(untouched.exists())
                        self.assertFalse(untouched.is_symlink())

    def test_install_refuses_unrelated_dsh_destination_before_any_link(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            cursor_skill, claude_skill, dsh_skill, cli = installed_paths(home)
            dsh_skill.parent.mkdir(parents=True)
            dsh_skill.mkdir()
            sentinel = dsh_skill / "SKILL.md"
            sentinel.write_text("user-owned skill", encoding="utf-8")

            result = run_installer(home)

            self.assertNotEqual(0, result.returncode)
            self.assertIn("refusing", result.stderr)
            self.assertIn(str(dsh_skill), result.stderr)
            self.assertTrue(dsh_skill.is_dir())
            self.assertFalse(dsh_skill.is_symlink())
            self.assertEqual(
                "user-owned skill", sentinel.read_text(encoding="utf-8")
            )
            for untouched in (cursor_skill, claude_skill, cli):
                self.assertFalse(untouched.exists())
                self.assertFalse(untouched.is_symlink())

            uninstall = run_installer(home, "--uninstall")

            self.assertEqual(0, uninstall.returncode, uninstall.stderr)
            self.assertIn("left unrelated path unchanged", uninstall.stdout)
            self.assertTrue(dsh_skill.is_dir())
            self.assertEqual(
                "user-owned skill", sentinel.read_text(encoding="utf-8")
            )

    def test_install_refuses_self_and_cyclic_symlinks_promptly(self):
        for cycle_kind in ("self", "mutual"):
            with self.subTest(cycle_kind=cycle_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    home = Path(temporary)
                    cursor_skill, claude_skill, dsh_skill, cli = installed_paths(home)
                    cursor_skill.parent.mkdir(parents=True)
                    if cycle_kind == "self":
                        cursor_skill.symlink_to(cursor_skill)
                    else:
                        peer = cursor_skill.parent / "cycle-peer"
                        cursor_skill.symlink_to(peer)
                        peer.symlink_to(cursor_skill)

                    result = run_installer(home)

                    self.assertNotEqual(0, result.returncode)
                    self.assertIn("refusing", result.stderr)
                    self.assertTrue(cursor_skill.is_symlink())
                    self.assertFalse(claude_skill.exists())
                    self.assertFalse(dsh_skill.exists())
                    self.assertFalse(cli.exists())


if __name__ == "__main__":
    unittest.main()
