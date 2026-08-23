import os
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest import mock

from sidecar.runtime_cmd import (
    RuntimeCommandError,
    resolve_runtime_prefix,
    validate_runtime_prefix,
)


def make_executable(path, text="#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


class RuntimeCommandTests(unittest.TestCase):
    def test_executable_release_zipapp_uses_absolute_entry(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = make_executable(Path(temporary) / "agent-sidecar.pyz")

            prefix = resolve_runtime_prefix(
                argv0=str(artifact),
                executable=sys.executable,
                module_file=str(Path(temporary) / "installed" / "runtime_cmd.py"),
            )

        self.assertEqual((str(artifact.resolve()),), prefix)

    def test_relative_release_path_is_canonicalized_at_resolution_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = make_executable(root / "dist" / "agent-sidecar.pyz")
            previous = Path.cwd()
            try:
                os.chdir(str(root))
                prefix = resolve_runtime_prefix(
                    argv0="dist/agent-sidecar.pyz",
                    executable=sys.executable,
                    module_file=str(root / "installed" / "runtime_cmd.py"),
                )
            finally:
                os.chdir(str(previous))

        self.assertEqual((str(artifact.resolve()),), prefix)

    def test_pipx_console_script_resolves_to_regular_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = make_executable(root / "pipx" / "venv" / "bin" / "agent-sidecar")
            entry = root / "bin" / "agent-sidecar"
            entry.parent.mkdir()
            entry.symlink_to(target)

            prefix = resolve_runtime_prefix(
                argv0=str(entry),
                executable=sys.executable,
                module_file=str(root / "site-packages" / "sidecar" / "runtime_cmd.py"),
            )

        self.assertEqual((str(entry),), prefix)

    def test_checkout_root_shim_is_independent_of_current_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            module = root / "sidecar" / "runtime_cmd.py"
            module.parent.mkdir(parents=True)
            module.write_text("# module\n", encoding="utf-8")
            (module.parent / "__init__.py").write_text("", encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
            shim = make_executable(root / "agent-sidecar")
            unrelated = Path(temporary) / "elsewhere"
            unrelated.mkdir()
            previous = Path.cwd()
            try:
                os.chdir(str(unrelated))
                prefix = resolve_runtime_prefix(
                    argv0="agent-sidecar",
                    executable=sys.executable,
                    module_file=str(module),
                )
            finally:
                os.chdir(str(previous))

        self.assertEqual((str(shim.resolve()),), prefix)

    def test_installed_module_falls_back_to_current_interpreter(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "site-packages" / "sidecar" / "runtime_cmd.py"
            module.parent.mkdir(parents=True)
            module.write_text("# module\n", encoding="utf-8")

            prefix = resolve_runtime_prefix(
                argv0="pytest",
                executable=sys.executable,
                module_file=str(module),
            )

        self.assertEqual(
            (str(Path(sys.executable)), "-m", "sidecar"),
            prefix,
        )

    def test_virtualenv_fallback_preserves_lexical_interpreter_and_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = root / "venv"
            venv.EnvBuilder(with_pip=False).create(str(environment))
            interpreter = environment / "bin" / "python"
            module = root / "site-packages" / "sidecar" / "runtime_cmd.py"
            module.parent.mkdir(parents=True)
            module.write_text("# module\n", encoding="utf-8")

            prefix = resolve_runtime_prefix(
                argv0="pytest",
                executable=str(interpreter),
                module_file=str(module),
            )
            completed = subprocess.run(
                prefix + ("--help",),
                cwd=str(Path(__file__).resolve().parent.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            interpreter_probe = subprocess.run(
                (
                    prefix[0],
                    "-c",
                    "import sys; print(sys.prefix)",
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
                text=True,
            )

        self.assertEqual((str(interpreter), "-m", "sidecar"), prefix)
        self.assertEqual(0, completed.returncode, completed.stderr.decode())
        self.assertEqual(0, interpreter_probe.returncode, interpreter_probe.stderr)
        self.assertEqual(
            environment.resolve(),
            Path(interpreter_probe.stdout.strip()).resolve(),
        )

    def test_lexical_symlink_target_in_writable_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lexical_directory = root / "trusted" / "bin"
            lexical_directory.mkdir(parents=True)
            target_directory = root / "writable-target"
            target_directory.mkdir(mode=0o777)
            target_directory.chmod(0o777)
            target = make_executable(target_directory / "python")
            interpreter = lexical_directory / "python"
            interpreter.symlink_to(target)

            with self.assertRaises(RuntimeCommandError):
                validate_runtime_prefix((str(interpreter), "-m", "sidecar"))

    def test_resolved_target_ancestor_swap_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lexical_directory = root / "trusted" / "bin"
            lexical_directory.mkdir(parents=True)
            target_directory = root / "target"
            target_directory.mkdir()
            target = make_executable(target_directory / "python")
            interpreter = lexical_directory / "python"
            interpreter.symlink_to(target)
            moved = root / "target-retained"

            import sidecar.runtime_cmd as runtime_cmd

            retain = runtime_cmd._retain_resolved_path

            def retain_then_swap(snapshots):
                retained = retain(snapshots)
                target_directory.rename(moved)
                target_directory.mkdir()
                return retained

            with mock.patch(
                "sidecar.runtime_cmd._retain_resolved_path",
                side_effect=retain_then_swap,
            ):
                with self.assertRaises(RuntimeCommandError):
                    validate_runtime_prefix((str(interpreter), "-m", "sidecar"))

    def test_unrelated_ancestor_metadata_churn_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = make_executable(root / "bin" / "agent-sidecar")

            import sidecar.runtime_cmd as runtime_cmd

            snapshot = runtime_cmd._snapshot_lexical_path

            def snapshot_then_churn(path):
                result = snapshot(path)
                marker = root / "unrelated"
                marker.write_text("changed", encoding="utf-8")
                marker.unlink()
                return result

            with mock.patch(
                "sidecar.runtime_cmd._snapshot_lexical_path",
                side_effect=snapshot_then_churn,
            ):
                prefix = validate_runtime_prefix((str(executable),))

        self.assertEqual((str(executable),), prefix)

    def test_writable_entrypoint_and_interpreter_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "site-packages" / "sidecar" / "runtime_cmd.py"
            module.parent.mkdir(parents=True)
            module.write_text("# module\n", encoding="utf-8")
            entry = make_executable(root / "agent-sidecar.pyz")
            entry.chmod(0o777)
            interpreter = make_executable(root / "python")
            interpreter.chmod(0o777)

            with self.assertRaises(RuntimeCommandError):
                resolve_runtime_prefix(
                    argv0=str(entry),
                    executable=str(interpreter),
                    module_file=str(module),
                )

    def test_entrypoint_symlink_swap_during_validation_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = make_executable(root / "first")
            second = make_executable(root / "second")
            entry = root / "agent-sidecar"
            entry.symlink_to(first)
            original_resolve = Path.resolve
            swapped = False

            def resolve_with_swap(path, *args, **kwargs):
                nonlocal swapped
                if path == entry and not swapped:
                    swapped = True
                    entry.unlink()
                    entry.symlink_to(second)
                return original_resolve(path, *args, **kwargs)

            with mock.patch.object(Path, "resolve", resolve_with_swap):
                with self.assertRaises(RuntimeCommandError):
                    validate_runtime_prefix((str(entry),))

    def test_invalid_fallback_interpreter_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "site-packages" / "sidecar" / "runtime_cmd.py"
            module.parent.mkdir(parents=True)
            module.write_text("# module\n", encoding="utf-8")

            with self.assertRaises(RuntimeCommandError):
                resolve_runtime_prefix(
                    argv0="pytest",
                    executable=str(Path(temporary) / "missing-python"),
                    module_file=str(module),
                )


if __name__ == "__main__":
    unittest.main()
