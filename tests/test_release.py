import hashlib
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile
from email.parser import Parser
from pathlib import Path
from unittest import mock

from sidecar.release import (
    MAX_RELEASE_BYTES,
    RELEASE_SHEBANG,
    ReleaseError,
    build_release_zipapp,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def isolated_environment(home):
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    for variable in (
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "DSHC_HOME",
        "DSH_HOME",
        "KIMI_CODE_HOME",
    ):
        environment.pop(variable, None)
    return environment


class ReleaseZipappTests(unittest.TestCase):
    def test_build_is_reproducible_bounded_executable_and_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first_path = root / "one" / "agent-sidecar.pyz"
            second_path = root / "two" / "agent-sidecar.pyz"

            first = build_release_zipapp(first_path)
            second = build_release_zipapp(second_path)
            first_bytes = first_path.read_bytes()
            second_bytes = second_path.read_bytes()

            self.assertEqual(first_bytes, second_bytes)
            self.assertTrue(first_bytes.startswith(RELEASE_SHEBANG))
            self.assertLessEqual(len(first_bytes), MAX_RELEASE_BYTES)
            self.assertEqual(len(first_bytes), first.size)
            self.assertEqual(
                hashlib.sha256(first_bytes).hexdigest(),
                first.sha256,
            )
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(
                0o755,
                stat.S_IMODE(first_path.stat().st_mode),
            )

            unrelated = root / "unrelated"
            unrelated.mkdir()
            environment = isolated_environment(root / "home")
            version = subprocess.run(
                [sys.executable, "-I", str(first_path), "--version"],
                cwd=str(unrelated),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
            list_help = subprocess.run(
                [sys.executable, "-I", str(first_path), "list", "--help"],
                cwd=str(unrelated),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(0, version.returncode, version.stderr)
            self.assertEqual("agent-sidecar 0.4.0\n", version.stdout)
            self.assertEqual(0, list_help.returncode, list_help.stderr)
            self.assertIn("usage: agent-sidecar list", list_help.stdout)

            if os.name == "posix":
                direct = subprocess.run(
                    [str(first_path), "--version"],
                    cwd=str(unrelated),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(0, direct.returncode, direct.stderr)
                self.assertEqual("agent-sidecar 0.4.0\n", direct.stdout)

    def test_standalone_release_builds_identical_runnable_nested_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            outer = root / "outer.pyz"
            nested = root / "nested" / "agent-sidecar.pyz"
            build_release_zipapp(outer)
            environment = isolated_environment(root / "home")

            package = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(outer),
                    "package",
                    "build",
                    "--output",
                    str(nested),
                ],
                cwd=str(root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=15,
            )
            version = subprocess.run(
                [sys.executable, "-I", str(nested), "--version"],
                cwd=str(root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(0, package.returncode, package.stderr)
            self.assertEqual(outer.read_bytes(), nested.read_bytes())
            self.assertEqual(0, version.returncode, version.stderr)
            self.assertEqual("agent-sidecar 0.4.0\n", version.stdout)

    @unittest.skipUnless(
        sys.platform == "darwin",
        "system symlink paths are macOS-specific",
    )
    def test_real_macos_tempfile_symlink_ancestors_are_supported(self):
        for directory in (None, "/tmp"):
            with self.subTest(directory=directory):
                with tempfile.TemporaryDirectory(dir=directory) as temporary:
                    root = Path(temporary)
                    output = root / "nested" / "agent-sidecar.pyz"

                    result = build_release_zipapp(output)

                    self.assertEqual(output, result.path)
                    self.assertTrue(output.is_file())
                    self.assertTrue(output.read_bytes().startswith(RELEASE_SHEBANG))

    @unittest.skipUnless(os.name == "posix", "symlink behavior requires POSIX")
    def test_refuses_symlink_and_nonregular_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            victim = root / "victim"
            victim.write_bytes(b"unchanged")
            linked = root / "linked.pyz"
            linked.symlink_to(victim)

            with self.assertRaises(ReleaseError):
                build_release_zipapp(linked)
            self.assertEqual(b"unchanged", victim.read_bytes())
            self.assertTrue(linked.is_symlink())

            directory_target = root / "directory.pyz"
            directory_target.mkdir()
            with self.assertRaises(ReleaseError):
                build_release_zipapp(directory_target)
            self.assertTrue(directory_target.is_dir())

    @unittest.skipUnless(os.name == "posix", "dirfd behavior requires POSIX")
    def test_refuses_nested_symlink_before_missing_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            trusted = root / "trusted"
            trusted.mkdir(mode=0o700)
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            (trusted / "nested").symlink_to(
                outside,
                target_is_directory=True,
            )
            output = trusted / "nested" / "missing" / "agent-sidecar.pyz"

            with self.assertRaises(ReleaseError):
                build_release_zipapp(output)

            self.assertEqual([], list(outside.iterdir()))
            self.assertFalse((outside / "missing").exists())

    @unittest.skipUnless(os.name == "posix", "dirfd behavior requires POSIX")
    def test_allows_synthetic_trusted_root_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            system = root / "system"
            canonical = system / "canonical"
            canonical.mkdir(mode=0o700, parents=True)
            alias = system / "alias"
            alias.symlink_to("canonical", target_is_directory=True)
            output = alias / "agent-sidecar.pyz"

            with mock.patch(
                "sidecar.release._ROOT_UID",
                os.geteuid(),
            ):
                result = build_release_zipapp(output)

            self.assertEqual(output, result.path)
            self.assertTrue((canonical / output.name).is_file())

    @unittest.skipUnless(os.name == "posix", "dirfd behavior requires POSIX")
    def test_trusted_relative_parent_target_uses_canonical_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            system = root / "system"
            branch = system / "branch"
            sibling = system / "sibling"
            branch.mkdir(mode=0o700, parents=True)
            sibling.mkdir(mode=0o700)
            alias = branch / "alias"
            alias.symlink_to("../sibling", target_is_directory=True)
            requested = alias / "agent-sidecar.pyz"

            with mock.patch(
                "sidecar.release._ROOT_UID",
                os.geteuid(),
            ):
                result = build_release_zipapp(requested)

            canonical = sibling / requested.name
            self.assertEqual(requested, result.path)
            self.assertEqual(canonical.read_bytes(), requested.read_bytes())
            self.assertTrue(canonical.read_bytes().startswith(RELEASE_SHEBANG))
            self.assertEqual([alias], list(branch.iterdir()))
            self.assertEqual([canonical], list(sibling.iterdir()))

    @unittest.skipUnless(os.name == "posix", "dirfd behavior requires POSIX")
    def test_nested_trusted_links_with_parent_components_stay_canonical(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            system = root / "system"
            branch = system / "branch"
            sibling = system / "sibling"
            nested = sibling / "nested"
            branch.mkdir(mode=0o700, parents=True)
            nested.mkdir(mode=0o700, parents=True)
            first = branch / "first"
            second = sibling / "second"
            first.symlink_to(
                "../sibling/second",
                target_is_directory=True,
            )
            second.symlink_to("nested/..", target_is_directory=True)
            requested = first / "agent-sidecar.pyz"

            with mock.patch(
                "sidecar.release._ROOT_UID",
                os.geteuid(),
            ):
                result = build_release_zipapp(requested)

            canonical = sibling / requested.name
            self.assertEqual(requested, result.path)
            self.assertEqual(canonical.read_bytes(), requested.read_bytes())
            self.assertEqual([], list(nested.iterdir()))
            self.assertEqual(
                {"agent-sidecar.pyz", "nested", "second"},
                {path.name for path in sibling.iterdir()},
            )

    @unittest.skipUnless(os.name == "posix", "dirfd behavior requires POSIX")
    def test_trusted_symlink_cannot_escape_above_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            system = root / "system"
            system.mkdir(mode=0o700)
            escape = system / "escape"
            escape.symlink_to("/..", target_is_directory=True)
            requested = escape / "agent-sidecar.pyz"

            with mock.patch(
                "sidecar.release._ROOT_UID",
                os.geteuid(),
            ):
                with self.assertRaises(ReleaseError):
                    build_release_zipapp(requested)

            self.assertEqual([escape], list(system.iterdir()))

    @unittest.skipUnless(os.name == "posix", "dirfd behavior requires POSIX")
    def test_rejects_trusted_symlink_loop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            system = root / "system"
            system.mkdir(mode=0o700)
            (system / "first").symlink_to(
                "second",
                target_is_directory=True,
            )
            (system / "second").symlink_to(
                "first",
                target_is_directory=True,
            )
            output = system / "first" / "agent-sidecar.pyz"

            with mock.patch(
                "sidecar.release._ROOT_UID",
                os.geteuid(),
            ):
                with self.assertRaises(ReleaseError):
                    build_release_zipapp(output)

            self.assertEqual(
                {"first", "second"},
                {path.name for path in system.iterdir()},
            )

    @unittest.skipUnless(os.name == "posix", "dirfd behavior requires POSIX")
    def test_rejects_trusted_symlink_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            system = root / "system"
            links = system / "links"
            canonical = system / "canonical"
            outside = system / "outside"
            links.mkdir(mode=0o700, parents=True)
            canonical.mkdir(mode=0o700, parents=True)
            outside.mkdir(mode=0o700)
            alias = links / "alias"
            alias.symlink_to("../canonical", target_is_directory=True)
            output = alias / "agent-sidecar.pyz"
            real_readlink = os.readlink
            swapped = []

            def swapping_readlink(path, *args, **kwargs):
                target = real_readlink(path, *args, **kwargs)
                if path == "alias" and not swapped:
                    alias.unlink()
                    alias.symlink_to("../outside", target_is_directory=True)
                    swapped.append(True)
                return target

            with (
                mock.patch(
                    "sidecar.release._ROOT_UID",
                    os.geteuid(),
                ),
                mock.patch(
                    "sidecar.release.os.readlink",
                    side_effect=swapping_readlink,
                ),
            ):
                with self.assertRaises(ReleaseError):
                    build_release_zipapp(output)

            self.assertTrue(swapped)
            self.assertEqual([], list(canonical.iterdir()))
            self.assertEqual([], list(outside.iterdir()))

    @unittest.skipUnless(os.name == "posix", "dirfd behavior requires POSIX")
    def test_ancestor_swap_cannot_redirect_write_outside(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            trusted = root / "trusted"
            nested = trusted / "nested"
            nested.mkdir(mode=0o700, parents=True)
            outside = root / "outside"
            outside.mkdir(mode=0o700)
            moved = root / "moved-trusted"
            output = nested / "agent-sidecar.pyz"
            real_open = os.open
            swapped = []

            def swapping_open(path, flags, *args, **kwargs):
                descriptor = real_open(path, flags, *args, **kwargs)
                if path == "nested" and not swapped:
                    trusted.rename(moved)
                    trusted.symlink_to(outside, target_is_directory=True)
                    swapped.append(True)
                return descriptor

            with mock.patch("sidecar.release.os.open", side_effect=swapping_open):
                with self.assertRaises(ReleaseError):
                    build_release_zipapp(output)

            self.assertTrue(swapped)
            self.assertTrue(trusted.is_symlink())
            self.assertEqual([], list(outside.iterdir()))
            self.assertEqual([], list((moved / "nested").iterdir()))

    @unittest.skipUnless(os.name == "posix", "ownership checks require POSIX")
    def test_refuses_foreign_unsafe_writable_ancestor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            unsafe = root / "unsafe"
            unsafe.mkdir(mode=0o700)
            unsafe.chmod(0o777)
            output = unsafe / "agent-sidecar.pyz"

            with mock.patch(
                "sidecar.release.os.geteuid",
                return_value=os.geteuid() + 1,
            ):
                with self.assertRaises(ReleaseError):
                    build_release_zipapp(output)

            self.assertEqual([], list(unsafe.iterdir()))

    def test_failed_replace_leaves_original_and_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            target = parent / "agent-sidecar.pyz"
            target.write_bytes(b"original")

            with mock.patch(
                "sidecar.release.os.replace",
                side_effect=OSError("injected failure"),
            ):
                with self.assertRaises(ReleaseError):
                    build_release_zipapp(target)

            self.assertEqual(b"original", target.read_bytes())
            self.assertEqual([target], list(parent.iterdir()))

    def test_oversized_artifact_fails_before_creating_output_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary).resolve() / "missing" / "agent-sidecar.pyz"
            with mock.patch(
                "sidecar.release.remote_transport.build_zipapp_bytes",
                return_value=b"x" * MAX_RELEASE_BYTES,
            ):
                with self.assertRaises(ReleaseError):
                    build_release_zipapp(output)

            self.assertFalse(output.parent.exists())


class WheelSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = [
            module
            for module in ("pip", "setuptools", "wheel")
            if importlib.util.find_spec(module) is None
        ]
        if missing:
            raise unittest.SkipTest(
                "local wheel tooling unavailable: {}".format(", ".join(missing))
            )

    def test_wheel_metadata_install_and_console_entry_point(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            shutil.copy2(REPO_ROOT / "pyproject.toml", source / "pyproject.toml")
            shutil.copytree(
                REPO_ROOT / "sidecar",
                source / "sidecar",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            build_environment = os.environ.copy()
            build_environment.pop("PYTHONHOME", None)
            build_environment.pop("PYTHONPATH", None)
            build_environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
            build_environment["PIP_NO_INDEX"] = "1"

            built = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheelhouse),
                    str(source),
                ],
                cwd=str(root),
                env=build_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            wheels = list(wheelhouse.glob("agent_sidecar-0.4.0-*.whl"))
            self.assertEqual(1, len(wheels), built.stdout)
            wheel_path = wheels[0]

            with zipfile.ZipFile(str(wheel_path)) as archive:
                metadata_names = [
                    name
                    for name in archive.namelist()
                    if name.endswith(".dist-info/METADATA")
                ]
                self.assertEqual(1, len(metadata_names))
                metadata = archive.read(metadata_names[0]).decode("utf-8")
                self.assertFalse(
                    any(name.startswith("tests/") for name in archive.namelist())
                )
            self.assertIn("\nName: agent-sidecar\n", "\n" + metadata)
            self.assertIn("\nVersion: 0.4.0\n", "\n" + metadata)
            self.assertIn("\nRequires-Python: >=3.9\n", "\n" + metadata)
            requirements = Parser().parsestr(metadata).get_all(
                "Requires-Dist",
                [],
            )
            self.assertEqual(
                {"coverage", "ruff"},
                {
                    requirement.partition(";")[0].partition("==")[0].strip()
                    for requirement in requirements
                },
            )
            for requirement in requirements:
                marker = requirement.partition(";")[2]
                normalized_marker = "".join(marker.split()).replace("'", '"')
                self.assertEqual('extra=="dev"', normalized_marker)

            environment_dir = root / "venv"
            venv.EnvBuilder(with_pip=True).create(str(environment_dir))
            scripts = environment_dir / ("Scripts" if os.name == "nt" else "bin")
            python = scripts / ("python.exe" if os.name == "nt" else "python")
            console = scripts / (
                "agent-sidecar.exe" if os.name == "nt" else "agent-sidecar"
            )
            environment = isolated_environment(root / "home")
            environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
            environment["PIP_NO_INDEX"] = "1"
            installed = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--no-index",
                    str(wheel_path),
                ],
                cwd=str(root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(0, installed.returncode, installed.stderr)

            unrelated = root / "unrelated"
            unrelated.mkdir()
            version = subprocess.run(
                [str(console), "--version"],
                cwd=str(unrelated),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
            self.assertEqual(0, version.returncode, version.stderr)
            self.assertEqual("agent-sidecar 0.4.0\n", version.stdout)
