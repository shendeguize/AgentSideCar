import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "install.sh"
VERSION = "1.2.3"
TAG = "v{}".format(VERSION)
ARTIFACT_NAME = "agent-sidecar-{}.pyz".format(VERSION)
SHEBANG = b"#!/usr/bin/env python3\n"


def make_zipapp(version):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr(
            "sidecar/__init__.py",
            '__version__ = "{}"\n'.format(version),
        )
        bundle.writestr(
            "sidecar/__main__.py",
            (
                "import sys\n"
                "from sidecar import __version__\n"
                'if sys.argv[1:] == ["--version"]:\n'
                '    print("agent-sidecar {}".format(__version__))\n'
            ),
        )
        bundle.writestr("sidecar/cli.py", "def main():\n    return 0\n")
        bundle.writestr(
            "__main__.py",
            (
                "import sys\n"
                "from sidecar import __version__\n"
                'if sys.argv[1:] == ["--version"]:\n'
                '    print("agent-sidecar {}".format(__version__))\n'
            ),
        )
    return SHEBANG + archive.getvalue()


class ReleaseFixture:
    def __init__(self, root):
        self.root = root
        self.root.mkdir()
        self.artifact = root / ARTIFACT_NAME
        self.checksums = root / "SHA256SUMS"
        self.metadata = root / "release.json"
        self.log = root / "curl.log"
        self.skill = root / "skill"
        self.fake_bin = root / "fake-bin"
        self.fake_bin.mkdir()
        self.skill.mkdir()
        self.skill.joinpath("SKILL.md").write_text(
            "---\nname: agent-sidecar\ndescription: Offline test skill.\n---\n",
            encoding="utf-8",
        )
        self.skill.joinpath("reference.md").write_text(
            "# Offline reference\n",
            encoding="utf-8",
        )
        self.write_release()
        self._write_fake_curl()

    @property
    def artifact_url(self):
        return (
            "https://github.com/shendeguize/AgentSideCar/releases/download/"
            "{}/{}".format(TAG, ARTIFACT_NAME)
        )

    @property
    def checksums_url(self):
        return (
            "https://github.com/shendeguize/AgentSideCar/releases/download/"
            "{}/SHA256SUMS".format(TAG)
        )

    def write_release(self, *, checksum=None, assets=None):
        payload = make_zipapp(VERSION)
        self.artifact.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest() if checksum is None else checksum
        self.checksums.write_text(
            "{}  {}\n".format(digest, ARTIFACT_NAME),
            encoding="ascii",
        )
        if assets is None:
            assets = [
                {
                    "name": ARTIFACT_NAME,
                    "browser_download_url": self.artifact_url,
                },
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": self.checksums_url,
                },
            ]
        self.metadata.write_text(
            json.dumps(
                {
                    "tag_name": TAG,
                    "draft": False,
                    "prerelease": False,
                    "assets": assets,
                }
            ),
            encoding="utf-8",
        )

    def _write_fake_curl(self):
        fake_curl = self.fake_bin / "curl"
        fake_curl.write_text(
            """#!/usr/bin/env python3
import os
import shutil
import sys
from pathlib import Path

root = Path(os.environ["FAKE_RELEASE_ROOT"])
arguments = sys.argv[1:]
try:
    output = Path(arguments[arguments.index("--output") + 1])
except (ValueError, IndexError):
    raise SystemExit("fake curl requires --output")
url = arguments[-1]
with (root / "curl.log").open("a", encoding="utf-8") as stream:
    stream.write(url + "\\n")
if url.endswith("/releases/latest") or "/releases/tags/" in url:
    source = root / "release.json"
elif url.endswith("/SHA256SUMS"):
    source = root / "SHA256SUMS"
elif "/skills/agent-sidecar/SKILL.md" in url:
    source = root / "skill" / "SKILL.md"
elif "/skills/agent-sidecar/reference.md" in url:
    source = root / "skill" / "reference.md"
else:
    source = root / Path(url).name
if not source.is_file():
    raise SystemExit("offline fixture has no asset for " + url)
output.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(source, output)
""",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)


class ReleaseInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = ReleaseFixture(self.root / "release")
        self.home = self.root / "home"
        self.home.mkdir()
        self.prefix = self.root / "prefix"

    def tearDown(self):
        self.temporary.cleanup()

    def environment(self, *, home=None, temp_root=None):
        environment = os.environ.copy()
        environment["HOME"] = str(self.home if home is None else home)
        environment["FAKE_RELEASE_ROOT"] = str(self.fixture.root)
        environment["PATH"] = os.pathsep.join(
            (str(self.fixture.fake_bin), environment.get("PATH", ""))
        )
        if temp_root is not None:
            environment["TMPDIR"] = str(temp_root)
        return environment

    def run_installer(
        self,
        *arguments,
        installer=INSTALLER,
        home=None,
        temp_root=None,
    ):
        return subprocess.run(
            ["/bin/sh", str(installer), *map(str, arguments)],
            cwd=str(REPO_ROOT),
            env=self.environment(home=home, temp_root=temp_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )

    def target(self, prefix=None):
        root = self.prefix if prefix is None else prefix
        return root / "bin" / "agent-sidecar"

    def test_shell_syntax_and_help(self):
        for shell in ("bash", "/bin/sh"):
            with self.subTest(shell=shell):
                result = subprocess.run(
                    [shell, "-n", str(INSTALLER)],
                    cwd=str(REPO_ROOT),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(0, result.returncode, result.stderr)

        help_result = self.run_installer("--help")
        self.assertEqual(0, help_result.returncode, help_result.stderr)
        self.assertIn("--version <vX.Y.Z|latest>", help_result.stdout)
        self.assertIn("--with-skill", help_result.stdout)
        self.assertFalse(self.fixture.log.exists())

    def test_valid_checksum_installs_exact_executable_atomically(self):
        result = self.run_installer(
            "--version",
            TAG,
            "--prefix",
            self.prefix,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        target = self.target()
        self.assertEqual(self.fixture.artifact.read_bytes(), target.read_bytes())
        self.assertEqual(0o755, stat.S_IMODE(target.stat().st_mode))
        self.assertEqual(
            [target.name],
            sorted(item.name for item in target.parent.iterdir()),
        )
        self.assertIn("checksum verified", result.stdout)
        version = subprocess.run(
            [str(target), "--version"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=5,
        )
        self.assertEqual(0, version.returncode, version.stderr)
        self.assertEqual("agent-sidecar {}\n".format(VERSION), version.stdout)

    def test_latest_resolution_uses_exact_assets(self):
        result = self.run_installer("--prefix", self.prefix)

        self.assertEqual(0, result.returncode, result.stderr)
        requests = self.fixture.log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [
                "https://api.github.com/repos/shendeguize/AgentSideCar/releases/latest",
                self.fixture.artifact_url,
                self.fixture.checksums_url,
            ],
            requests,
        )

    def test_checksum_mismatch_leaves_existing_install_untouched(self):
        first = self.run_installer("--prefix", self.prefix)
        self.assertEqual(0, first.returncode, first.stderr)
        target = self.target()
        original = target.read_bytes()
        original_inode = target.stat().st_ino
        self.fixture.write_release(checksum="0" * 64)

        result = self.run_installer("--prefix", self.prefix)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("checksum verification failed", result.stderr)
        self.assertEqual(original, target.read_bytes())
        self.assertEqual(original_inode, target.stat().st_ino)

    def test_malformed_version_is_rejected_without_curl(self):
        for version in ("1.2.3", "v01.2.3", "v1.2", "latest;touch-pwned"):
            with self.subTest(version=version):
                if self.fixture.log.exists():
                    self.fixture.log.unlink()
                result = self.run_installer(
                    "--version",
                    version,
                    "--prefix",
                    self.prefix,
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn("--version must be", result.stderr)
                self.assertFalse(self.fixture.log.exists())

    def test_asset_name_or_url_mismatch_is_rejected(self):
        self.fixture.write_release(
            assets=[
                {
                    "name": ARTIFACT_NAME,
                    "browser_download_url": self.fixture.artifact_url + ".wrong",
                },
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": self.fixture.checksums_url,
                },
            ]
        )

        result = self.run_installer("--prefix", self.prefix)

        self.assertNotEqual(0, result.returncode)
        self.assertIn("exact required assets", result.stderr)
        self.assertFalse(self.target().exists())
        self.assertEqual(
            1,
            len(self.fixture.log.read_text(encoding="utf-8").splitlines()),
        )

    def test_unrelated_target_is_never_replaced_or_removed(self):
        target = self.target()
        target.parent.mkdir(parents=True)
        target.write_text("#!/bin/sh\nprintf unrelated\n", encoding="utf-8")

        install = self.run_installer("--prefix", self.prefix)
        uninstall = self.run_installer("--prefix", self.prefix, "--uninstall")

        self.assertNotEqual(0, install.returncode)
        self.assertNotEqual(0, uninstall.returncode)
        self.assertIn("refusing", install.stderr)
        self.assertIn("refusing", uninstall.stderr)
        self.assertEqual(
            "#!/bin/sh\nprintf unrelated\n",
            target.read_text(encoding="utf-8"),
        )
        self.assertFalse(self.fixture.log.exists())

    def test_uninstall_removes_only_recognized_agent_sidecar_zipapp(self):
        install = self.run_installer("--prefix", self.prefix)
        self.assertEqual(0, install.returncode, install.stderr)

        uninstall = self.run_installer("--prefix", self.prefix, "--uninstall")

        self.assertEqual(0, uninstall.returncode, uninstall.stderr)
        self.assertFalse(self.target().exists())
        self.assertIn("skill installations were left unchanged", uninstall.stdout)

    def test_paths_with_spaces_and_space_backed_temp_directory(self):
        home = self.root / "home with spaces"
        prefix = self.root / "prefix with spaces"
        temp_root = self.root / "temporary files"
        home.mkdir()
        temp_root.mkdir()

        result = self.run_installer(
            "--prefix",
            prefix,
            home=home,
            temp_root=temp_root,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self.target(prefix).is_file())
        self.assertEqual([], list(temp_root.iterdir()))

    def test_with_skill_reuses_checkout_installer_for_default_prefix(self):
        result = self.run_installer("--with-skill")

        self.assertEqual(0, result.returncode, result.stderr)
        target = self.home / ".local" / "bin" / "agent-sidecar"
        cursor_skill = self.home / ".cursor" / "skills" / "agent-sidecar"
        claude_skill = self.home / ".claude" / "skills" / "agent-sidecar"
        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())
        self.assertTrue(cursor_skill.is_symlink())
        self.assertTrue(claude_skill.is_symlink())
        self.assertEqual(
            (REPO_ROOT / "skills" / "agent-sidecar").resolve(),
            cursor_skill.resolve(),
        )
        self.assertEqual(cursor_skill.resolve(), claude_skill.resolve())

        repeated = self.run_installer("--with-skill")
        self.assertEqual(0, repeated.returncode, repeated.stderr)
        self.assertTrue(target.is_file())
        self.assertFalse(target.is_symlink())

    def test_standalone_with_skill_fetches_only_tag_pinned_skill_files(self):
        standalone = self.root / "standalone installer.sh"
        shutil.copyfile(INSTALLER, standalone)

        result = self.run_installer(
            "--prefix",
            self.prefix,
            "--with-skill",
            installer=standalone,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        for root in (".cursor", ".claude"):
            skill = self.home / root / "skills" / "agent-sidecar"
            self.assertTrue(skill.is_dir())
            self.assertFalse(skill.is_symlink())
            self.assertEqual(
                self.fixture.skill.joinpath("SKILL.md").read_bytes(),
                skill.joinpath("SKILL.md").read_bytes(),
            )
            marker = json.loads(
                skill.joinpath(".agent-sidecar-release-skill").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("agent-sidecar-release-skill-v1", marker["signature"])
            self.assertEqual(VERSION, marker["version"])
        requests = self.fixture.log.read_text(encoding="utf-8").splitlines()
        self.assertIn(
            (
                "https://raw.githubusercontent.com/shendeguize/AgentSideCar/"
                "{}/skills/agent-sidecar/SKILL.md".format(TAG)
            ),
            requests,
        )
        self.assertIn(
            (
                "https://raw.githubusercontent.com/shendeguize/AgentSideCar/"
                "{}/skills/agent-sidecar/reference.md".format(TAG)
            ),
            requests,
        )


if __name__ == "__main__":
    unittest.main()
