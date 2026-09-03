import fcntl
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidecar.client import SidecarClientError
from sidecar.daemon import RUNTIME_ENV
from sidecar.launchd import (
    LABEL,
    LOCK_NAME,
    PATH_VALUE,
    PLIST_NAME,
    ServiceResult,
    build_plist,
    install_service,
    plist_bytes,
    service_status,
    uninstall_service,
)


def make_executable(path):
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class FakeLaunchctl:
    def __init__(
        self,
        *,
        loaded=False,
        bootstrap_codes=(),
        print_code=None,
        pid=4321,
        state="running",
        print_stdout=None,
    ):
        self.loaded = loaded
        self.bootstrap_codes = list(bootstrap_codes)
        self.print_code = print_code
        self.pid = pid
        self.state = state
        self.print_stdout = print_stdout
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), dict(kwargs)))
        operation = argv[1]
        if operation == "print":
            code = (
                self.print_code
                if self.print_code is not None
                else 0 if self.loaded else 113
            )
            if code == 0:
                stderr = b""
                if self.print_stdout is None:
                    lines = [
                        "{}/{} = {{".format(argv[2].rsplit("/", 1)[0], LABEL),
                        "\tstate = {}".format(self.state),
                    ]
                    if self.pid is not None:
                        lines.append("\tpid = {}".format(self.pid))
                    lines.append("}")
                    stdout = ("\n".join(lines) + "\n").encode("utf-8")
                else:
                    stdout = self.print_stdout
            elif code == 113:
                stderr = b"Could not find service"
                stdout = b""
            else:
                stderr = b"/private/secret launchctl diagnostic"
                stdout = b""
        elif operation == "bootstrap":
            code = self.bootstrap_codes.pop(0) if self.bootstrap_codes else 0
            if code == 0:
                self.loaded = True
            stderr = b"" if code == 0 else b"bootstrap details must stay private"
            stdout = b""
        elif operation == "bootout":
            if self.loaded:
                self.loaded = False
                code = 0
                stderr = b""
                stdout = b""
            else:
                code = 113
                stderr = b"Could not find service"
                stdout = b""
        else:
            raise AssertionError(operation)
        return subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


class FakeClient:
    def __init__(
        self,
        runner=None,
        *,
        always_running=False,
        offline=False,
        http=None,
        pid=4321,
    ):
        self.runner = runner
        self.always_running = always_running
        self.offline = offline
        self.http = http
        self.pid = pid

    def ping(self):
        running = self.always_running or (
            self.runner is not None and self.runner.loaded
        )
        if self.offline or not running:
            raise SidecarClientError("offline", code="connection_failed")
        response = {"ok": True, "op": "ping", "pid": self.pid}
        if self.http is not None:
            response["http"] = dict(self.http)
        return response


class LaunchdTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.launchctl = make_executable(self.root / "launchctl")
        self.entry = make_executable(self.root / "agent-sidecar")
        self.runtime = self.root / "runtime"
        self.prefix = (str(self.entry.resolve()),)

    def tearDown(self):
        self.temporary.cleanup()

    @property
    def plist_path(self):
        return self.home / "Library" / "LaunchAgents" / PLIST_NAME

    @property
    def lock_path(self):
        return self.home / "Library" / "LaunchAgents" / LOCK_NAME

    def install(self, runner=None, client=None, **kwargs):
        active_runner = FakeLaunchctl() if runner is None else runner
        active_client = (
            FakeClient(active_runner) if client is None else client
        )
        runtime_dir = kwargs.pop("runtime_dir", self.runtime)
        result = install_service(
            runner=active_runner,
            client=active_client,
            prefix=self.prefix,
            runtime_dir=runtime_dir,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
            **kwargs
        )
        return result, active_runner

    def write_plist(self, *, http=False, http_port=None, prefix=None):
        self.plist_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = plist_bytes(
            build_plist(
                self.prefix if prefix is None else prefix,
                runtime_dir=self.runtime,
                http=http,
                http_port=http_port,
            )
        )
        self.plist_path.write_bytes(payload)
        self.plist_path.chmod(0o644)
        return payload

    def write_document(self, document):
        self.plist_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = plist_bytes(document)
        self.plist_path.write_bytes(payload)
        self.plist_path.chmod(0o644)
        return payload

    def test_install_writes_launchd_compatible_xml_and_strict_argv(self):
        runner = FakeLaunchctl()
        result, _ = self.install(runner=runner)

        self.assertEqual(ServiceResult(0, "service installed and running"), result)
        payload = self.plist_path.read_bytes()
        document = plistlib.loads(payload)
        self.assertTrue(payload.startswith(b"<?xml"))
        self.assertEqual(LABEL, document["Label"])
        self.assertEqual(
            [self.prefix[0], "daemon", "run"],
            document["ProgramArguments"],
        )
        self.assertTrue(document["RunAtLoad"])
        self.assertTrue(document["KeepAlive"])
        self.assertEqual("Background", document["ProcessType"])
        self.assertEqual("/dev/null", document["StandardOutPath"])
        self.assertEqual("/dev/null", document["StandardErrorPath"])
        self.assertNotIn("WorkingDirectory", document)
        self.assertEqual(
            {
                RUNTIME_ENV: str(self.runtime.resolve()),
                "PATH": PATH_VALUE,
            },
            document["EnvironmentVariables"],
        )
        self.assertEqual(0o644, self.plist_path.stat().st_mode & 0o777)
        bootstrap = [call for call, _ in runner.calls if call[1] == "bootstrap"]
        self.assertEqual(
            (
                str(self.launchctl.resolve()),
                "bootstrap",
                "gui/{}".format(os.geteuid()),
                str(self.plist_path),
            ),
            bootstrap[0],
        )
        for _argv, kwargs in runner.calls:
            self.assertNotIn("shell", kwargs)
            self.assertLessEqual(kwargs["stdout_limit"], 64 * 1024)
            self.assertLessEqual(kwargs["stderr_limit"], 64 * 1024)

    def test_http_flags_are_exact_and_no_secret_environment_is_inherited(self):
        http = {"enabled": True, "host": "127.0.0.1", "port": 43123}
        runner = FakeLaunchctl()
        result, _ = self.install(
            runner=runner,
            client=FakeClient(runner, http=http),
            http=True,
            http_port=43123,
        )

        self.assertEqual(0, result.exit_code)
        document = plistlib.loads(self.plist_path.read_bytes())
        self.assertEqual(
            [
                self.prefix[0],
                "daemon",
                "run",
                "--http",
                "--http-port",
                "43123",
            ],
            document["ProgramArguments"],
        )
        environment = document["EnvironmentVariables"]
        self.assertEqual({"AGENT_SIDECAR_RUNTIME_DIR", "PATH"}, set(environment))
        self.assertNotIn("HOME", environment)
        self.assertNotIn("token", repr(document).casefold())

    def test_identical_install_is_idempotent(self):
        runner = FakeLaunchctl()
        first, _ = self.install(runner=runner)
        inode = self.plist_path.stat().st_ino
        runner.calls.clear()

        second, _ = self.install(runner=runner)

        self.assertEqual(0, first.exit_code)
        self.assertEqual(0, second.exit_code)
        self.assertIn("already", second.message)
        self.assertEqual(inode, self.plist_path.stat().st_ino)
        self.assertFalse(any(call[0][1] == "bootstrap" for call in runner.calls))

    def test_different_definition_requires_force_and_force_reloads(self):
        runner = FakeLaunchctl()
        first, _ = self.install(runner=runner)
        self.assertEqual(0, first.exit_code)

        refused, _ = self.install(
            runner=runner,
            client=FakeClient(
                runner,
                http={"enabled": True, "host": "127.0.0.1", "port": 4444},
            ),
            http=True,
            http_port=4444,
        )
        self.assertEqual(2, refused.exit_code)
        runner.calls.clear()

        replaced, _ = self.install(
            runner=runner,
            client=FakeClient(
                runner,
                http={"enabled": True, "host": "127.0.0.1", "port": 4444},
            ),
            http=True,
            http_port=4444,
            force=True,
        )

        self.assertEqual(0, replaced.exit_code)
        operations = [call[0][1] for call in runner.calls]
        self.assertEqual(["print", "bootout", "bootstrap", "print"], operations)

    def test_install_refuses_manual_daemon(self):
        runner = FakeLaunchctl()
        result, _ = self.install(
            runner=runner,
            client=FakeClient(always_running=True),
        )

        self.assertEqual(2, result.exit_code)
        self.assertIn("manually", result.message)
        self.assertFalse(self.plist_path.exists())
        self.assertFalse(any(call[0][1] == "bootstrap" for call in runner.calls))

    def test_bootstrap_failure_removes_new_plist_and_boots_out(self):
        runner = FakeLaunchctl(bootstrap_codes=[5])
        result, _ = self.install(runner=runner)

        self.assertEqual(1, result.exit_code)
        self.assertFalse(self.plist_path.exists())
        self.assertFalse(runner.loaded)
        self.assertEqual(
            ["print", "bootstrap", "bootout"],
            [call[0][1] for call in runner.calls],
        )

    def test_readiness_timeout_boots_out_and_removes_new_plist(self):
        runner = FakeLaunchctl()
        result, _ = self.install(
            runner=runner,
            client=FakeClient(runner, offline=True),
            ready_timeout=0,
        )

        self.assertEqual(1, result.exit_code)
        self.assertFalse(self.plist_path.exists())
        self.assertFalse(runner.loaded)
        self.assertEqual(
            ["print", "bootstrap", "print", "bootout"],
            [call[0][1] for call in runner.calls],
        )

    def test_default_readiness_window_outlasts_a_first_index_scan(self):
        # A daemon answers nothing until its first index scan ends, and that
        # scan grows with the index: 22s on a 1,952-session machine. A window
        # shorter than the scan boots out a healthy service and reports it as
        # a daemon that never became ready.
        runner = FakeLaunchctl()
        clock = [0.0]

        def advance(value):
            clock[0] += value

        client = FakeClient(runner, pid=4321)
        answers_at = 22.0
        original_ping = client.ping

        def ping():
            if clock[0] < answers_at:
                raise SidecarClientError("scanning", code="connection_failed")
            return original_ping()

        client.ping = ping

        result, _ = self.install(
            runner=runner,
            client=client,
            monotonic=lambda: clock[0],
            sleep=advance,
        )

        self.assertEqual(0, result.exit_code)
        self.assertTrue(self.plist_path.exists())
        self.assertTrue(runner.loaded)

    def test_atomic_write_failure_leaves_no_service_or_plist(self):
        runner = FakeLaunchctl()
        with mock.patch(
            "sidecar.launchd.os.replace",
            side_effect=OSError("replace failed"),
        ):
            result, _ = self.install(runner=runner)

        self.assertEqual(1, result.exit_code)
        self.assertFalse(self.plist_path.exists())
        self.assertFalse(runner.loaded)
        self.assertEqual(["print"], [call[0][1] for call in runner.calls])

    def test_install_rejects_writable_runtime_command_before_persisting(self):
        self.entry.chmod(0o777)

        result, runner = self.install()

        self.assertEqual(2, result.exit_code)
        self.assertFalse(self.plist_path.exists())
        self.assertEqual([], runner.calls)

    def test_in_place_plist_change_aborts_before_force_bootout(self):
        self.write_plist()
        foreign = dict(build_plist(self.prefix, runtime_dir=self.runtime))
        foreign["Label"] = "com.example.foreign"
        foreign_payload = plist_bytes(foreign)
        plist_path = self.plist_path

        class MutatingPrint(FakeLaunchctl):
            def __init__(self):
                super().__init__(loaded=True)
                self.mutated = False

            def __call__(self, argv, **kwargs):
                result = super().__call__(argv, **kwargs)
                if argv[1] == "print" and not self.mutated:
                    self.mutated = True
                    plist_path.write_bytes(foreign_payload)
                    plist_path.chmod(0o644)
                return result

        runner = MutatingPrint()
        result, _ = self.install(
            runner=runner,
            http=True,
            force=True,
        )

        self.assertEqual(2, result.exit_code)
        self.assertEqual(foreign_payload, self.plist_path.read_bytes())
        self.assertTrue(runner.loaded)
        self.assertFalse(any(call[0][1] == "bootout" for call in runner.calls))

    def test_in_place_plist_change_aborts_overwrite_and_unlink(self):
        foreign = dict(build_plist(self.prefix, runtime_dir=self.runtime))
        foreign["Label"] = "com.example.foreign"
        foreign_payload = plist_bytes(foreign)
        plist_path = self.plist_path

        class MutatingPrint(FakeLaunchctl):
            def __init__(self):
                super().__init__(loaded=False)

            def __call__(self, argv, **kwargs):
                result = super().__call__(argv, **kwargs)
                if argv[1] == "print":
                    plist_path.write_bytes(foreign_payload)
                    plist_path.chmod(0o644)
                return result

        self.write_plist()
        overwritten, _ = self.install(
            runner=MutatingPrint(),
            http=True,
            force=True,
        )
        self.assertEqual(2, overwritten.exit_code)
        self.assertEqual(foreign_payload, self.plist_path.read_bytes())

        self.plist_path.unlink()
        self.write_plist()
        runner = MutatingPrint()
        uninstalled = uninstall_service(
            runner=runner,
            client=FakeClient(runner),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        self.assertEqual(2, uninstalled.exit_code)
        self.assertEqual(foreign_payload, self.plist_path.read_bytes())

    def test_rollback_does_not_overwrite_in_place_foreign_content(self):
        self.write_plist()
        foreign = dict(build_plist(self.prefix, runtime_dir=self.runtime))
        foreign["Label"] = "com.example.foreign"
        foreign_payload = plist_bytes(foreign)
        plist_path = self.plist_path

        class MutatingBootstrap(FakeLaunchctl):
            def __init__(self):
                super().__init__(loaded=True, bootstrap_codes=[9])

            def __call__(self, argv, **kwargs):
                result = super().__call__(argv, **kwargs)
                if argv[1] == "bootstrap" and result.returncode != 0:
                    plist_path.write_bytes(foreign_payload)
                    plist_path.chmod(0o644)
                return result

        runner = MutatingBootstrap()
        result, _ = self.install(
            runner=runner,
            http=True,
            force=True,
        )

        self.assertEqual(1, result.exit_code)
        self.assertIn("rollback incomplete", result.message)
        self.assertEqual(foreign_payload, self.plist_path.read_bytes())

    def test_force_bootout_failure_preserves_previous_definition(self):
        previous = self.write_plist()

        class BootoutFailure(FakeLaunchctl):
            def __call__(self, argv, **kwargs):
                if argv[1] == "bootout":
                    self.calls.append((tuple(argv), dict(kwargs)))
                    return subprocess.CompletedProcess(
                        argv,
                        5,
                        stdout=b"",
                        stderr=b"private failure",
                    )
                return super().__call__(argv, **kwargs)

        runner = BootoutFailure(loaded=True)
        result, _ = self.install(
            runner=runner,
            client=FakeClient(
                runner,
                http={"enabled": True, "host": "127.0.0.1", "port": 4444},
            ),
            http=True,
            http_port=4444,
            force=True,
        )

        self.assertEqual(1, result.exit_code)
        self.assertEqual(previous, self.plist_path.read_bytes())
        self.assertTrue(any(call[0][1] == "bootstrap" for call in runner.calls))
        self.assertIn("rollback incomplete", result.message)

    def test_force_failure_restores_previous_bytes_and_loaded_state(self):
        previous = self.write_plist()
        runner = FakeLaunchctl(loaded=True, bootstrap_codes=[9, 0])

        result, _ = self.install(
            runner=runner,
            client=FakeClient(runner, offline=True),
            http=True,
            force=True,
        )

        self.assertEqual(1, result.exit_code)
        self.assertEqual(previous, self.plist_path.read_bytes())
        self.assertTrue(runner.loaded)
        self.assertEqual(
            ["print", "bootout", "bootstrap", "bootout", "bootstrap"],
            [call[0][1] for call in runner.calls],
        )

    def test_managed_plist_requires_exact_keys_values_and_environment(self):
        mutations = {
            "program": lambda value: value.update({"Program": self.prefix[0]}),
            "working-directory": lambda value: value.update(
                {"WorkingDirectory": str(self.root)}
            ),
            "environment": lambda value: value["EnvironmentVariables"].update(
                {"HOME": str(self.home)}
            ),
            "user": lambda value: value.update({"UserName": "root"}),
            "group": lambda value: value.update({"GroupName": "wheel"}),
            "root": lambda value: value.update({"RootDirectory": "/"}),
            "session": lambda value: value.update({"SessionCreate": True}),
            "override": lambda value: value.update(
                {"LimitLoadToSessionType": "Aqua"}
            ),
            "wrong-path": lambda value: value["EnvironmentVariables"].update(
                {"PATH": "/tmp"}
            ),
            "relative-runtime": lambda value: value["EnvironmentVariables"].update(
                {RUNTIME_ENV: "relative/runtime"}
            ),
            "typed-bool": lambda value: value.update({"RunAtLoad": 1}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                document = dict(build_plist(self.prefix, runtime_dir=self.runtime))
                document["EnvironmentVariables"] = dict(
                    document["EnvironmentVariables"]
                )
                mutate(document)
                self.write_document(document)
                runner = FakeLaunchctl(loaded=True)

                result, _ = self.install(runner=runner, force=True)

                self.assertEqual(2, result.exit_code)
                self.assertTrue(runner.loaded)
                self.assertFalse(
                    any(call[0][1] == "bootout" for call in runner.calls)
                )
                self.plist_path.unlink()

    def test_custom_stored_runtime_survives_environment_change(self):
        custom_runtime = self.root / "custom-runtime"
        runner = FakeLaunchctl()
        http = {"enabled": True, "host": "127.0.0.1", "port": 43123}
        installed, _ = self.install(
            runner=runner,
            client=FakeClient(runner, http=http),
            runtime_dir=custom_runtime,
            http=True,
            http_port=43123,
        )
        self.assertEqual(0, installed.exit_code)
        selected = []

        def client_factory(runtime_dir):
            selected.append(Path(runtime_dir))
            return FakeClient(runner, http=http)

        with mock.patch.dict(os.environ, {}, clear=True):
            status = service_status(
                runner=runner,
                client_factory=client_factory,
                prefix=self.prefix,
                home=self.home,
                platform="darwin",
                launchctl_path=self.launchctl,
                euid=os.geteuid(),
            )
        with mock.patch.dict(
            os.environ,
            {RUNTIME_ENV: str(self.root / "changed-runtime")},
            clear=True,
        ):
            removed = uninstall_service(
                runner=runner,
                client_factory=client_factory,
                prefix=self.prefix,
                home=self.home,
                platform="darwin",
                launchctl_path=self.launchctl,
                euid=os.geteuid(),
            )

        self.assertEqual(0, status.exit_code)
        self.assertEqual(0, removed.exit_code)
        self.assertEqual(
            [custom_runtime.resolve(), custom_runtime.resolve()],
            selected,
        )
        self.assertFalse(self.plist_path.exists())

    def test_runtime_change_requires_force_and_switches_readiness_client(self):
        first_runtime = self.root / "runtime-one"
        second_runtime = self.root / "runtime-two"
        runner = FakeLaunchctl()
        first, _ = self.install(
            runner=runner,
            client=FakeClient(runner),
            runtime_dir=first_runtime,
        )
        self.assertEqual(0, first.exit_code)
        selected = []

        def client_factory(runtime_dir):
            selected.append(Path(runtime_dir))
            return FakeClient(runner)

        refused, _ = self.install(
            runner=runner,
            client_factory=client_factory,
            runtime_dir=second_runtime,
        )
        self.assertEqual(2, refused.exit_code)
        self.assertFalse(any(call[0][1] == "bootout" for call in runner.calls[-2:]))

        replaced, _ = self.install(
            runner=runner,
            client_factory=client_factory,
            runtime_dir=second_runtime,
            force=True,
        )

        self.assertEqual(0, replaced.exit_code)
        self.assertIn(first_runtime.resolve(), selected)
        self.assertIn(second_runtime.resolve(), selected)
        document = plistlib.loads(self.plist_path.read_bytes())
        self.assertEqual(
            str(second_runtime.resolve()),
            document["EnvironmentVariables"][RUNTIME_ENV],
        )

    def test_stored_runtime_symlink_and_unsafe_values_are_rejected(self):
        target = self.root / "runtime-target"
        target.mkdir(mode=0o700)
        linked = self.root / "runtime-link"
        linked.symlink_to(target, target_is_directory=True)
        values = (
            str(linked),
            "relative/runtime",
            "/" + "x" * 4097,
            "/tmp/bad\nruntime",
        )
        for value in values:
            with self.subTest(value=value[:80]):
                document = dict(build_plist(self.prefix, runtime_dir=self.runtime))
                document["EnvironmentVariables"] = dict(
                    document["EnvironmentVariables"]
                )
                document["EnvironmentVariables"][RUNTIME_ENV] = value
                self.write_document(document)
                runner = FakeLaunchctl(loaded=True)

                status = service_status(
                    runner=runner,
                    client=FakeClient(runner),
                    prefix=self.prefix,
                    home=self.home,
                    platform="darwin",
                    launchctl_path=self.launchctl,
                    euid=os.geteuid(),
                )

                self.assertEqual(2, status.exit_code)
                self.plist_path.unlink()

    def test_force_refuses_loaded_label_without_restorable_plist(self):
        runner = FakeLaunchctl(loaded=True)

        result, _ = self.install(
            runner=runner,
            client=FakeClient(runner, offline=True),
            force=True,
        )

        self.assertEqual(2, result.exit_code)
        self.assertIn("restorable", result.message)
        self.assertTrue(runner.loaded)
        self.assertEqual(["print"], [call[0][1] for call in runner.calls])

    def test_status_and_uninstall_reject_execution_overrides(self):
        document = dict(build_plist(self.prefix, runtime_dir=self.runtime))
        document["Program"] = self.prefix[0]
        self.write_document(document)
        status_runner = FakeLaunchctl(loaded=True)
        status = service_status(
            runner=status_runner,
            client=FakeClient(status_runner),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        uninstall_runner = FakeLaunchctl(loaded=True)
        uninstall = uninstall_service(
            runner=uninstall_runner,
            client=FakeClient(uninstall_runner),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )

        self.assertEqual(2, status.exit_code)
        self.assertEqual(2, uninstall.exit_code)
        self.assertTrue(self.plist_path.exists())
        self.assertFalse(
            any(call[0][1] == "bootout" for call in uninstall_runner.calls)
        )

    def test_interruptions_after_each_mutation_restore_state_and_reraise(self):
        class InterruptingRunner(FakeLaunchctl):
            def __init__(self, operation, **kwargs):
                super().__init__(**kwargs)
                self.operation = operation
                self.interrupted = False

            def __call__(self, argv, **kwargs):
                if argv[1] == self.operation and not self.interrupted:
                    self.interrupted = True
                    self.calls.append((tuple(argv), dict(kwargs)))
                    if self.operation == "bootout":
                        self.loaded = False
                    elif self.operation == "bootstrap":
                        self.loaded = True
                    raise KeyboardInterrupt("original interruption")
                return super().__call__(argv, **kwargs)

        previous = self.write_plist()
        bootout_runner = InterruptingRunner("bootout", loaded=True)
        with self.assertRaisesRegex(KeyboardInterrupt, "original interruption"):
            self.install(
                runner=bootout_runner,
                client=FakeClient(
                    bootout_runner,
                    http={
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": 4444,
                    },
                ),
                http=True,
                http_port=4444,
                force=True,
            )
        self.assertEqual(previous, self.plist_path.read_bytes())
        self.assertTrue(bootout_runner.loaded)
        self.plist_path.unlink()

        real_atomic_write = __import__(
            "sidecar.launchd",
            fromlist=["_atomic_write"],
        )._atomic_write
        write_calls = []

        def interrupt_after_write(*args, **kwargs):
            real_atomic_write(*args, **kwargs)
            write_calls.append(True)
            if len(write_calls) == 1:
                raise SystemExit(17)

        with mock.patch(
            "sidecar.launchd._atomic_write",
            side_effect=interrupt_after_write,
        ):
            with self.assertRaises(SystemExit) as raised:
                self.install(runner=FakeLaunchctl())
        self.assertEqual(17, raised.exception.code)
        self.assertFalse(self.plist_path.exists())

        bootstrap_runner = InterruptingRunner("bootstrap")
        with self.assertRaisesRegex(KeyboardInterrupt, "original interruption"):
            self.install(
                runner=bootstrap_runner,
                client=FakeClient(bootstrap_runner),
            )
        self.assertFalse(self.plist_path.exists())
        self.assertFalse(bootstrap_runner.loaded)

        class ReadinessInterruptClient(FakeClient):
            def ping(self):
                if self.runner is not None and self.runner.loaded:
                    raise KeyboardInterrupt("readiness interruption")
                return super().ping()

        readiness_runner = FakeLaunchctl()
        with self.assertRaisesRegex(KeyboardInterrupt, "readiness interruption"):
            self.install(
                runner=readiness_runner,
                client=ReadinessInterruptClient(readiness_runner),
            )
        self.assertFalse(self.plist_path.exists())
        self.assertFalse(readiness_runner.loaded)

    def test_rollback_failure_does_not_mask_original_interrupt(self):
        self.write_plist()

        class PersistentInterruptRunner(FakeLaunchctl):
            def __call__(self, argv, **kwargs):
                if argv[1] == "bootout":
                    self.calls.append((tuple(argv), dict(kwargs)))
                    raise KeyboardInterrupt("original interruption")
                return super().__call__(argv, **kwargs)

        runner = PersistentInterruptRunner(loaded=True)
        with self.assertRaisesRegex(
            KeyboardInterrupt,
            "original interruption",
        ) as raised:
            self.install(
                runner=runner,
                client=FakeClient(
                    runner,
                    http={
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": 4444,
                    },
                ),
                http=True,
                http_port=4444,
                force=True,
            )

        rollback = getattr(raised.exception, "launchd_rollback_error", None)
        self.assertIsNotNone(rollback)
        self.assertIn("rollback incomplete", str(rollback))
        self.assertNotIn(str(self.plist_path), str(rollback))

    def test_symlink_wrong_mode_foreign_and_owner_are_refused(self):
        cases = []

        target = self.root / "foreign-target"
        target.write_bytes(b"unchanged")
        self.plist_path.parent.mkdir(mode=0o700, parents=True)
        self.plist_path.symlink_to(target)
        cases.append(("symlink", target))

        for name, watched in cases:
            with self.subTest(case=name):
                result, _ = self.install(runner=FakeLaunchctl(), force=True)
                self.assertEqual(2, result.exit_code)
                self.assertEqual(b"unchanged", watched.read_bytes())
        self.plist_path.unlink()

        self.write_plist()
        self.plist_path.chmod(0o600)
        result, _ = self.install(runner=FakeLaunchctl(), force=True)
        self.assertEqual(2, result.exit_code)

        self.plist_path.unlink()
        document = dict(build_plist(self.prefix, runtime_dir=self.runtime))
        document["Label"] = "com.example.foreign"
        self.plist_path.write_bytes(plist_bytes(document))
        self.plist_path.chmod(0o644)
        result, _ = self.install(runner=FakeLaunchctl(), force=True)
        self.assertEqual(2, result.exit_code)

        wrong_owner = install_service(
            runner=FakeLaunchctl(),
            client=FakeClient(),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid() + 1,
        )
        self.assertEqual(2, wrong_owner.exit_code)

    def test_symlink_directory_and_concurrent_lock_are_refused(self):
        linked = self.root / "linked-library"
        linked.mkdir()
        (self.home / "Library").symlink_to(linked, target_is_directory=True)
        result, _ = self.install(runner=FakeLaunchctl())
        self.assertEqual(2, result.exit_code)
        (self.home / "Library").unlink()

        self.lock_path.parent.mkdir(mode=0o700, parents=True)
        descriptor = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            runner = FakeLaunchctl()
            result, _ = self.install(runner=runner)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.assertEqual(2, result.exit_code)
        self.assertIn("another", result.message)
        self.assertEqual([], runner.calls)

    def test_uninstall_boots_out_valid_service_and_retains_runtime_state(self):
        self.write_plist()
        self.runtime.mkdir(mode=0o700)
        retained = self.runtime / "audit.jsonl"
        retained.write_text("keep\n", encoding="utf-8")
        runner = FakeLaunchctl(loaded=True)

        result = uninstall_service(
            runner=runner,
            client=FakeClient(runner),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )

        self.assertEqual(0, result.exit_code)
        self.assertFalse(self.plist_path.exists())
        self.assertTrue(retained.exists())
        self.assertFalse(runner.loaded)

    def test_uninstall_refuses_foreign_or_manual_daemon_and_missing_is_idempotent(self):
        self.write_plist(prefix=(str(self.launchctl.resolve()),))
        foreign = uninstall_service(
            runner=FakeLaunchctl(),
            client=FakeClient(),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        self.assertEqual(2, foreign.exit_code)
        self.plist_path.unlink()

        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        self.plist_path.symlink_to(victim)
        linked = uninstall_service(
            runner=FakeLaunchctl(),
            client=FakeClient(),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        self.assertEqual(2, linked.exit_code)
        self.assertEqual(b"keep", victim.read_bytes())
        self.plist_path.unlink()

        manual = uninstall_service(
            runner=FakeLaunchctl(),
            client=FakeClient(always_running=True),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        self.assertEqual(2, manual.exit_code)

        missing = uninstall_service(
            runner=FakeLaunchctl(),
            client=FakeClient(),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        self.assertEqual(0, missing.exit_code)

    def test_missing_fcntl_is_a_clear_unsupported_control_error(self):
        with mock.patch("sidecar.launchd.fcntl", None):
            result, _ = self.install(runner=FakeLaunchctl())

        self.assertEqual(2, result.exit_code)
        self.assertIn("locking", result.message)

    def test_status_distinguishes_running_loaded_unloaded_and_control_error(self):
        self.write_plist()

        running_runner = FakeLaunchctl(loaded=True)
        running = service_status(
            runner=running_runner,
            client=FakeClient(running_runner),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        loaded = service_status(
            # Loaded with nothing behind it: launchd holds the job but no
            # process, which is the only silence that means "not running".
            runner=FakeLaunchctl(loaded=True, pid=None, state="waiting"),
            client=FakeClient(offline=True),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        unloaded = service_status(
            runner=FakeLaunchctl(),
            client=FakeClient(),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        control = service_status(
            runner=FakeLaunchctl(print_code=2),
            client=FakeClient(),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )

        self.assertEqual(0, running.exit_code)
        self.assertIn("running", running.message)
        self.assertEqual(1, loaded.exit_code)
        self.assertIn("loaded", loaded.message)
        self.assertEqual(1, unloaded.exit_code)
        self.assertIn("unloaded", unloaded.message)
        self.assertEqual(2, control.exit_code)
        self.assertNotIn("private", control.message)
        self.assertNotIn(str(self.plist_path), control.message)

    def test_status_names_a_supervised_pid_that_has_not_answered_yet(self):
        # launchd keeps reporting a live pid throughout the daemon's first
        # scan, and the socket answers nothing until that scan ends. Calling
        # that window "not running" contradicts the pid launchd just gave.
        self.write_plist()
        warming_runner = FakeLaunchctl(loaded=True)

        warming = service_status(
            runner=warming_runner,
            client=FakeClient(offline=True),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )

        self.assertEqual(1, warming.exit_code)
        self.assertIn(str(warming_runner.pid), warming.message)
        self.assertIn("not answering", warming.message)
        self.assertNotIn("not running", warming.message)

    def test_status_requires_parsed_running_state_and_exact_ping_pid(self):
        self.write_plist()
        mismatch_runner = FakeLaunchctl(loaded=True, pid=9001)
        mismatch = service_status(
            runner=mismatch_runner,
            client=FakeClient(mismatch_runner, pid=9002),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        reused_runner = FakeLaunchctl(
            loaded=True,
            pid=4321,
            state="exited",
        )
        reused = service_status(
            runner=reused_runner,
            client=FakeClient(reused_runner, pid=4321),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )

        self.assertEqual(1, mismatch.exit_code)
        self.assertIn("degraded", mismatch.message)
        self.assertEqual(1, reused.exit_code)
        self.assertIn("degraded", reused.message)

    def test_launchctl_print_uses_only_top_level_job_identity(self):
        self.write_plist()
        payload = (
            "gui/{}/{} = {{\n"
            "\tactive count = 2\n"
            "\tstate = running\n"
            "\tpath = \"/tmp/a{{literal}}\"\n"
            "\tcoalitions = {{\n"
            "\t\tservice = {{\n"
            "\t\t\tstate = waiting\n"
            "\t\t\tpid = invalid-nested-value\n"
            "\t\t}}\n"
            "\t}}\n"
            "\tendpoints = {{\n"
            "\t\tlistener = {{\n"
            "\t\t\tstate = active\n"
            "\t\t\tpid = 9999\n"
            "\t\t}}\n"
            "\t}}\n"
            "\tpid = 4321\n"
            "}}\n"
        ).format(os.geteuid(), LABEL).encode("utf-8")
        runner = FakeLaunchctl(loaded=True, print_stdout=payload)

        result = service_status(
            runner=runner,
            client=FakeClient(runner, pid=4321),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )

        self.assertEqual(0, result.exit_code)
        self.assertIn("running", result.message)

    def test_launchctl_print_missing_pid_or_malformed_record_fails_closed(self):
        self.write_plist()
        cases = (
            FakeLaunchctl(loaded=True, pid=None),
            FakeLaunchctl(loaded=True, print_stdout=b"state = running\npid = 4321\n"),
            FakeLaunchctl(
                loaded=True,
                print_stdout=b"x" * (64 * 1024 + 1),
            ),
            FakeLaunchctl(
                loaded=True,
                print_stdout=(
                    "gui/{}/{} = {{\nstate = waiting\npid = 0\n}}\n"
                ).format(os.geteuid(), LABEL).encode("utf-8"),
            ),
            FakeLaunchctl(
                loaded=True,
                print_stdout=(
                    "gui/{}/{} = {{\n"
                    "state = running\n"
                    "state = waiting\n"
                    "pid = 4321\n"
                    "}}\n"
                ).format(os.geteuid(), LABEL).encode("utf-8"),
            ),
            FakeLaunchctl(
                loaded=True,
                print_stdout=(
                    "gui/{}/{} = {{\n"
                    "state = running\n"
                    "nested = {{\n"
                    "pid = 9999\n"
                    "}}\n"
                ).format(os.geteuid(), LABEL).encode("utf-8"),
            ),
            FakeLaunchctl(
                loaded=True,
                print_stdout=(
                    "gui/{}/{} = {{\n"
                    "state = running\n"
                    "pid = 4321\n"
                    "pid = 4322\n"
                    "}}\n"
                ).format(os.geteuid(), LABEL).encode("utf-8"),
            ),
        )
        for runner in cases:
            with self.subTest(stdout=runner.print_stdout, pid=runner.pid):
                result = service_status(
                    runner=runner,
                    client=FakeClient(runner),
                    prefix=self.prefix,
                    runtime_dir=self.runtime,
                    home=self.home,
                    platform="darwin",
                    launchctl_path=self.launchctl,
                    euid=os.geteuid(),
                )
                self.assertEqual(2, result.exit_code)
                self.assertNotIn("gui/", result.message)

        self.plist_path.unlink()
        readiness_runner = FakeLaunchctl(
            print_stdout=(
                "gui/{}/{} = {{\nstate = running\n}}\n"
            ).format(os.geteuid(), LABEL).encode("utf-8"),
        )
        readiness, _ = self.install(
            runner=readiness_runner,
            client=FakeClient(readiness_runner),
        )
        self.assertEqual(2, readiness.exit_code)
        self.assertFalse(readiness_runner.loaded)
        self.assertFalse(self.plist_path.exists())

    def test_install_pid_mismatch_conflicts_before_or_rolls_back_after_mutation(self):
        self.write_plist()
        loaded_runner = FakeLaunchctl(loaded=True, pid=7001)
        conflict, _ = self.install(
            runner=loaded_runner,
            client=FakeClient(loaded_runner, pid=7002),
            force=True,
        )
        self.assertEqual(2, conflict.exit_code)
        self.assertTrue(loaded_runner.loaded)
        self.assertFalse(
            any(call[0][1] == "bootout" for call in loaded_runner.calls)
        )
        self.plist_path.unlink()

        started_runner = FakeLaunchctl(pid=8001)
        failed, _ = self.install(
            runner=started_runner,
            client=FakeClient(started_runner, pid=8002),
        )
        self.assertEqual(1, failed.exit_code)
        self.assertFalse(started_runner.loaded)
        self.assertFalse(self.plist_path.exists())
        self.assertEqual(
            ["print", "bootstrap", "print", "bootout"],
            [call[0][1] for call in started_runner.calls],
        )

    def test_unsupported_platform_and_missing_launchctl_are_exit_two(self):
        unsupported = install_service(
            client=FakeClient(),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="linux",
            launchctl_path=self.launchctl,
            euid=os.geteuid(),
        )
        missing = install_service(
            client=FakeClient(),
            prefix=self.prefix,
            runtime_dir=self.runtime,
            home=self.home,
            platform="darwin",
            launchctl_path=self.root / "missing-launchctl",
            euid=os.geteuid(),
        )

        self.assertEqual(2, unsupported.exit_code)
        self.assertIn("macOS", unsupported.message)
        self.assertEqual(2, missing.exit_code)
        self.assertIn("launchctl", missing.message)


if __name__ == "__main__":
    unittest.main()
