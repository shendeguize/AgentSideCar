import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidecar.client import SidecarClientError
from sidecar.daemon import RUNTIME_ENV
from sidecar import systemd
from sidecar.service import (
    _backend,
    install_service as facade_install_service,
    service_status as facade_service_status,
    uninstall_service as facade_uninstall_service,
)
from sidecar.systemd import (
    ServiceResult,
    UNIT_NAME,
    build_unit,
    install_service,
    service_status,
    uninstall_service,
)


class FakeSystemctl:
    def __init__(self, *, loaded=False, active=False, pid=4321):
        self.loaded = loaded
        self.active = active
        self.pid = pid
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), dict(kwargs)))
        operation = argv[2]
        if operation == "show":
            if not self.loaded:
                return subprocess.CompletedProcess(
                    argv, 3, stdout=b"", stderr=b"Unit not found"
                )
            state = (
                "LoadState=loaded\n"
                "ActiveState={}\n"
                "SubState={}\n"
                "MainPID={}\n"
            ).format(
                "active" if self.active else "inactive",
                "running" if self.active else "dead",
                self.pid if self.active else 0,
            )
            return subprocess.CompletedProcess(
                argv, 0, stdout=state.encode("ascii"), stderr=b""
            )
        if operation == "enable":
            self.loaded = True
            self.active = True
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if operation == "disable":
            self.active = False
            self.loaded = False
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        if operation == "daemon-reload":
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        raise AssertionError(operation)


class FakeClient:
    def __init__(self, runner, *, pid=4321, offline=False, always_running=False):
        self.runner = runner
        self.pid = pid
        self.offline = offline
        self.always_running = always_running

    def ping(self):
        if self.offline or (not self.runner.active and not self.always_running):
            raise SidecarClientError("offline", code="connection_failed")
        return {"ok": True, "op": "ping", "pid": self.pid}


def executable(path):
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class SystemdTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.home = root / "home"
        self.home.mkdir(mode=0o700)
        self.config = self.home / ".config"
        self.systemctl = executable(root / "systemctl")
        self.entry = executable(root / "agent-sidecar")
        self.runtime = root / "runtime"
        self.prefix = (str(self.entry.resolve()),)

    def tearDown(self):
        self.temporary.cleanup()

    @property
    def unit(self):
        return self.config / "systemd" / "user" / UNIT_NAME

    def install(self, runner=None, client=None, **kwargs):
        active_runner = FakeSystemctl() if runner is None else runner
        active_client = (
            FakeClient(active_runner) if client is None else client
        )
        result = install_service(
            runner=active_runner,
            client=active_client,
            prefix=self.prefix,
            runtime_dir=kwargs.pop("runtime_dir", self.runtime),
            home=self.home,
            config_home=self.config,
            platform="linux",
            systemctl_path=self.systemctl,
            euid=os.geteuid(),
            **kwargs
        )
        return result, active_runner

    def test_install_writes_hardened_user_unit_and_exact_argv(self):
        result, runner = self.install()

        self.assertEqual(ServiceResult(0, "service installed and running"), result)
        text = self.unit.read_text(encoding="utf-8")
        self.assertIn("ExecStart=\"{}\" \"daemon\" \"run\"".format(self.prefix[0]), text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("KillMode=control-group", text)
        self.assertIn("NoNewPrivileges=yes", text)
        self.assertIn("ProtectSystem=strict", text)
        self.assertIn("ProtectHome=read-only", text)
        self.assertIn("ReadWritePaths=\"{}\"".format(self.runtime.resolve()), text)
        self.assertIn(
            'Environment="{}={}"'.format(RUNTIME_ENV, self.runtime.resolve()),
            text,
        )
        self.assertNotIn("HOME=", text)
        self.assertEqual(0o644, self.unit.stat().st_mode & 0o777)
        self.assertEqual(
            ["show", "daemon-reload", "enable", "show"],
            [call[0][2] for call in runner.calls],
        )
        for _argv, kwargs in runner.calls:
            self.assertEqual({"PATH": "/usr/local/bin:/usr/bin:/bin"}, kwargs["env"])
            self.assertLessEqual(kwargs["stdout_limit"], 64 * 1024)
            self.assertLessEqual(kwargs["stderr_limit"], 64 * 1024)

    def test_unit_escapes_percent_and_keeps_spaces_without_shell(self):
        entry = executable(self.home / "side car%runtime")
        payload = build_unit((str(entry),), runtime_dir=self.runtime)
        text = payload.decode("utf-8")
        self.assertIn('"{}"'.format(str(entry).replace("%", "%%")), text)
        self.assertNotIn("ExecStart=sh", text)

    def test_identical_install_is_idempotent(self):
        runner = FakeSystemctl()
        first, _ = self.install(runner=runner)
        runner.calls.clear()
        second, _ = self.install(runner=runner)

        self.assertEqual(0, first.exit_code)
        self.assertEqual(0, second.exit_code)
        self.assertIn("already", second.message)
        self.assertEqual(["show"], [call[0][2] for call in runner.calls])

    def test_install_refuses_manual_daemon_and_uninstall_retains_runtime(self):
        runner = FakeSystemctl()
        result, _ = self.install(
            runner=runner,
            client=FakeClient(runner, pid=9999, always_running=True),
        )
        self.assertEqual(2, result.exit_code)
        self.assertFalse(self.unit.exists())

        installed, runner = self.install()
        self.assertEqual(0, installed.exit_code)
        self.runtime.mkdir(mode=0o700)
        retained = self.runtime / "audit.jsonl"
        retained.write_text("keep\n", encoding="utf-8")
        removed = uninstall_service(
            runner=runner,
            client=FakeClient(runner),
            runtime_dir=self.runtime,
            home=self.home,
            config_home=self.config,
            platform="linux",
            systemctl_path=self.systemctl,
            euid=os.geteuid(),
        )
        self.assertEqual(0, removed.exit_code)
        self.assertFalse(self.unit.exists())
        self.assertTrue(retained.exists())

    def test_status_detects_pid_mismatch_and_missing_unit(self):
        runner = FakeSystemctl(loaded=True, active=True, pid=100)
        self.unit.parent.mkdir(mode=0o700, parents=True)
        self.unit.write_bytes(build_unit(self.prefix, runtime_dir=self.runtime))
        self.unit.chmod(0o644)
        mismatch = service_status(
            runner=runner,
            client=FakeClient(runner, pid=101),
            runtime_dir=self.runtime,
            home=self.home,
            config_home=self.config,
            platform="linux",
            systemctl_path=self.systemctl,
            euid=os.geteuid(),
        )
        self.assertEqual(1, mismatch.exit_code)
        self.assertIn("degraded", mismatch.message)

        self.unit.unlink()
        runner.loaded = False
        runner.active = False
        missing = service_status(
            runner=runner,
            client=FakeClient(runner, offline=True),
            home=self.home,
            config_home=self.config,
            platform="linux",
            systemctl_path=self.systemctl,
            euid=os.geteuid(),
        )
        self.assertEqual(1, missing.exit_code)
        self.assertIn("unloaded", missing.message)

    def test_foreign_unit_and_unsupported_platform_fail_closed(self):
        self.unit.parent.mkdir(mode=0o700, parents=True)
        self.unit.write_text("[Unit]\nDescription=foreign\n", encoding="utf-8")
        self.unit.chmod(0o644)
        foreign, runner = self.install(force=True)
        self.assertEqual(2, foreign.exit_code)
        self.assertEqual([], runner.calls)

        unsupported = install_service(
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            config_home=self.config,
            platform="darwin",
            systemctl_path=self.systemctl,
            euid=os.geteuid(),
        )
        self.assertEqual(2, unsupported.exit_code)

    def test_service_facade_selects_supported_backends_and_rejects_other_platforms(self):
        self.assertIsNotNone(_backend("darwin"))
        self.assertIsNotNone(_backend("linux"))
        self.assertIsNone(_backend("freebsd"))
        for operation in (
            facade_install_service,
            facade_uninstall_service,
            facade_service_status,
        ):
            result = operation(platform="freebsd")
            self.assertEqual(2, result.exit_code)

    def test_validation_helpers_cover_rejected_inputs_and_environment(self):
        with mock.patch.object(systemd.os, "geteuid", None):
            with self.assertRaises(systemd.SystemdControlError):
                systemd._effective_uid()
        with mock.patch.object(systemd.os, "geteuid", return_value=True):
            with self.assertRaises(systemd.SystemdControlError):
                systemd._effective_uid()
        with mock.patch.object(systemd.os, "geteuid", return_value=-1):
            with self.assertRaises(systemd.SystemdControlError):
                systemd._effective_uid()
        with mock.patch.object(systemd.os, "geteuid", return_value=1.0):
            with self.assertRaises(systemd.SystemdControlError):
                systemd._effective_uid()
        with self.assertRaises(systemd.SystemdControlError):
            systemd._selected_platform("darwin")
        with self.assertRaises(systemd.SystemdSecurityError):
            systemd._safe_path(Path("relative"), name="test")
        with self.assertRaises(systemd.SystemdSecurityError):
            systemd._safe_path(Path("bad\0path"), name="test")
        with self.assertRaises(systemd.SystemdSecurityError):
            systemd._safe_path(Path("\ud800"), name="test")
        with self.assertRaises(systemd.SystemdSecurityError):
            systemd._quote("")
        with self.assertRaises(systemd.SystemdSecurityError):
            systemd._quote("bad\nvalue")
        self.assertEqual(
            '"a\\\\b\\"c%%d"',
            systemd._quote('a\\b"c%d'),
        )
        with self.assertRaises(systemd.SystemdSecurityError):
            systemd._validated_runtime_prefix(())
        with self.assertRaises(systemd.SystemdSecurityError):
            systemd._validated_runtime_prefix(("bad\n",))
        with mock.patch.object(
            systemd,
            "resolve_runtime_prefix",
            side_effect=systemd.RuntimeCommandError("unresolved"),
        ):
            with self.assertRaises(systemd.SystemdControlError):
                systemd._resolve_prefix(None)
        with self.assertRaises(systemd.SystemdControlError):
            systemd._ready_timeout("not-a-timeout")
        with self.assertRaises(systemd.SystemdControlError):
            systemd._ready_timeout(float("inf"))
        with self.assertRaises(systemd.SystemdControlError):
            systemd._ready_timeout(-1)
        with self.assertRaises(systemd.SystemdControlError):
            systemd._ready_timeout(61)

        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}):
            paths = systemd.service_paths(euid=os.geteuid(), home=self.home)
        self.assertEqual(self.config.resolve(), paths.config)
        with self.assertRaises(systemd.SystemdSecurityError):
            systemd.service_paths(
                euid=os.geteuid(),
                home=self.home,
                config_home=Path("relative"),
            )
        with self.assertRaises(systemd.SystemdControlError):
            systemd.service_paths(euid=True, home=self.home)

    def test_unit_and_control_helpers_fail_closed(self):
        with self.assertRaises(systemd.SystemdControlError):
            systemd.build_unit(self.prefix, http_port=8080)
        with self.assertRaises(systemd.SystemdControlError):
            systemd.build_unit(self.prefix, http=True, http_port=True)
        with self.assertRaises(systemd.SystemdControlError):
            systemd.build_unit(self.prefix, http=True, http_port=65536)
        http_unit = systemd.build_unit(
            self.prefix,
            runtime_dir=self.runtime,
            http=True,
        )
        self.assertIn(b"--http", http_unit)
        http_port_unit = systemd.build_unit(
            self.prefix,
            runtime_dir=self.runtime,
            http=True,
            http_port=8080,
        )
        self.assertIn(b"--http-port", http_port_unit)
        with self.assertRaises(systemd.SystemdControlError):
            systemd.build_unit(("x" * 70000,))
        self.assertTrue(systemd._looks_managed(build_unit(self.prefix, runtime_dir=self.runtime)))
        self.assertFalse(systemd._looks_managed(b"foreign"))
        self.assertEqual(b"text", systemd._output(mock.Mock(stdout="text"), "stdout"))
        self.assertEqual(b"", systemd._output(mock.Mock(stdout=object()), "stdout"))
        self.assertEqual(3, systemd._returncode(mock.Mock(returncode="3")))
        with self.assertRaises(systemd.SystemdControlError):
            systemd._returncode(mock.Mock())

        self.assertIsNone(systemd._read_unit(self.unit, os.geteuid()))
        self.unit.parent.mkdir(mode=0o700, parents=True)
        self.unit.write_text("foreign\n", encoding="utf-8")
        self.unit.chmod(0o600)
        with self.assertRaises(systemd.SystemdSecurityError):
            systemd._read_unit(self.unit, os.geteuid())
        self.unit.chmod(0o644)
        self.assertEqual(b"foreign\n", systemd._read_unit(self.unit, os.geteuid()).payload)
        with mock.patch.object(systemd, "_read_unit", side_effect=OSError("read failed")):
            with self.assertRaises(systemd.SystemdOperationError):
                systemd._atomic_write(self.unit, b"payload", None)
        with mock.patch.object(Path, "lstat", side_effect=OSError("stat failed")):
            with self.assertRaises(systemd.SystemdSecurityError):
                systemd._read_unit(self.unit, os.geteuid())
        with mock.patch.object(Path, "read_bytes", side_effect=OSError("read failed")):
            with self.assertRaises(systemd.SystemdSecurityError):
                systemd._read_unit(self.unit, os.geteuid())

        runner = mock.Mock(side_effect=OSError("runner failed"))
        with self.assertRaises(systemd.SystemdControlError):
            systemd._control(runner, self.systemctl, ("show",))
        overflowing = subprocess.CompletedProcess(
            (), 0, stdout=b"x" * (systemd.MAX_CONTROL_OUTPUT + 1), stderr=b""
        )
        with self.assertRaises(systemd.SystemdControlError):
            systemd._control(lambda *_args, **_kwargs: overflowing, self.systemctl, ("show",))
        with self.assertRaises(systemd.SystemdControlError):
            systemd._ensure_supported("linux", self.home / "missing-systemctl")
        not_executable = self.home / "not-executable"
        not_executable.write_text("not executable\n", encoding="utf-8")
        with self.assertRaises(systemd.SystemdControlError):
            systemd._ensure_supported("linux", not_executable)
        with self.assertRaises(systemd.SystemdControlError):
            systemd._ensure_supported("darwin", self.systemctl)

    def test_systemctl_state_parsing_and_wait_helpers_cover_terminal_paths(self):
        missing = subprocess.CompletedProcess((), 3, stdout=b"", stderr=b"Unit not found")
        self.assertTrue(systemd._missing(missing))
        self.assertTrue(systemd._missing(subprocess.CompletedProcess((), 1)))
        self.assertFalse(systemd._missing(subprocess.CompletedProcess((), 2, stderr=b"not found")))
        self.assertFalse(systemd._missing(subprocess.CompletedProcess((), 3, stderr=b"permission denied")))
        self.assertEqual(systemd._UnitState(False), systemd._parse_state(missing))
        with self.assertRaises(systemd.SystemdControlError):
            systemd._parse_state(subprocess.CompletedProcess((), 2, stderr=b"permission denied"))
        with self.assertRaises(systemd.SystemdControlError):
            systemd._parse_state(
                subprocess.CompletedProcess((), 0, stdout=b"\xff")
            )
        with self.assertRaises(systemd.SystemdControlError):
            systemd._parse_state(
                subprocess.CompletedProcess((), 0, stdout=b"broken-line\n")
            )
        with self.assertRaises(systemd.SystemdControlError):
            systemd._parse_state(
                subprocess.CompletedProcess((), 0, stdout=b"LoadState=loaded\nMainPID=nope\n")
            )
        with self.assertRaises(systemd.SystemdControlError):
            systemd._parse_state(
                subprocess.CompletedProcess(
                    (), 0, stdout=b"LoadState=loaded\nMainPID=999999999999\n"
                )
            )
        state = systemd._parse_state(
            subprocess.CompletedProcess(
                (),
                0,
                stdout=b"LoadState=loaded\nActiveState=active\n"
                b"SubState=running\nMainPID=42\n",
            )
        )
        self.assertEqual(systemd._UnitState(True, True, 42), state)
        self.assertEqual(
            systemd._UnitState(True, False, 42),
            systemd._parse_state(
                subprocess.CompletedProcess(
                    (),
                    0,
                    stdout=b"LoadState=loaded\nActiveState=active\n"
                    b"SubState=dead\nMainPID=42\n",
                )
            ),
        )
        self.assertEqual(
            systemd._UnitState(True, False, None),
            systemd._parse_state(
                subprocess.CompletedProcess(
                    (),
                    0,
                    stdout=b"LoadState=loaded\nActiveState=inactive\n"
                    b"SubState=dead\nMainPID=0\n",
                )
            ),
        )

        class PingClient:
            def ping_info(self):
                return {"ok": True, "op": "ping", "pid": 42}

        self.assertEqual(42, systemd._ping(PingClient()).pid)
        self.assertEqual(
            42,
            systemd._ping(
                FakeClient(
                    FakeSystemctl(loaded=True, active=True, pid=42),
                    pid=42,
                )
            ).pid,
        )
        self.assertIsNone(systemd._ping(mock.Mock(ping=mock.Mock(side_effect=OSError()))))
        self.assertIs(self.runtime, systemd._client_for_runtime(
            self.runtime, self.runtime, None
        ))
        client = mock.Mock(socket_path=str(self.home / "other" / "sidecar.sock"))
        self.assertIsNot(client, systemd._client_for_runtime(client, self.runtime, None))
        invalid_client = mock.Mock(socket_path=object())
        self.assertIsNot(
            invalid_client,
            systemd._client_for_runtime(invalid_client, self.runtime, None),
        )
        factory = mock.Mock(return_value="factory-client")
        self.assertEqual(
            "factory-client",
            systemd._client_for_runtime(None, self.runtime, factory),
        )

        runner = FakeSystemctl(loaded=False, active=False)
        clock = iter((0.0, 1.0))
        self.assertIsNone(
            systemd._wait_ready(
                FakeClient(runner, offline=True),
                runner=runner,
                systemctl=self.systemctl,
                timeout=0.5,
                monotonic=lambda: next(clock),
                sleep=lambda _value: None,
            )
        )
        stopped_clock = iter((0.0, 1.0))
        self.assertFalse(
            systemd._wait_stopped(
                FakeClient(runner, always_running=True),
                timeout=0.5,
                monotonic=lambda: next(stopped_clock),
                sleep=lambda _value: None,
            )
        )

    def test_operation_lock_and_service_error_paths_are_bounded(self):
        with systemd._operation_lock(self.home / "lock", os.geteuid()):
            self.assertTrue((self.home / "lock").exists())
        with mock.patch.object(systemd, "fcntl", None):
            with self.assertRaises(systemd.SystemdControlError):
                with systemd._operation_lock(self.home / "lock", os.geteuid()):
                    pass

        runner = FakeSystemctl()
        result, _ = self.install(
            runner=runner,
            client=FakeClient(runner, offline=True),
            ready_timeout=0,
        )
        self.assertEqual(1, result.exit_code)
        self.assertFalse(self.unit.exists())
        result = uninstall_service(
            runner=runner,
            client=FakeClient(runner, offline=True),
            runtime_dir=self.runtime,
            home=self.home,
            config_home=self.config,
            platform="linux",
            systemctl_path=self.systemctl,
            euid=os.geteuid(),
            ready_timeout=0,
        )
        self.assertEqual(0, result.exit_code)

        self.assertEqual(
            systemd.ServiceResult(2, "bad"),
            systemd._service_error_result(systemd.SystemdControlError("bad")),
        )


if __name__ == "__main__":
    unittest.main()
