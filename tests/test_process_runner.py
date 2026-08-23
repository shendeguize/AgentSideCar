import dataclasses
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from sidecar.process_runner import (
    MAX_TIMEOUT_SECONDS,
    BoundedProcessResult,
    bounded_execution_signal_guard,
    run_bounded,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ProcessRunnerTests(unittest.TestCase):
    def test_result_is_frozen_and_run_supports_input_env_and_path_cwd(self):
        code = (
            "import os,sys;"
            "data=sys.stdin.buffer.read();"
            "sys.stdout.buffer.write("
            "os.getcwd().encode()+b'|'+os.environ['MARKER'].encode()+b'|'+data"
            ");"
            "sys.stderr.buffer.write(b'error')"
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = run_bounded(
                [sys.executable, "-c", code],
                b"input",
                input_limit=32,
                stdout_limit=4096,
                stderr_limit=4096,
                timeout=900,
                env={"MARKER": "present"},
                cwd=Path(temporary),
            )

        self.assertIsInstance(result, BoundedProcessResult)
        self.assertEqual(0, result.returncode)
        self.assertEqual(
            "{}|present|input".format(Path(temporary).resolve()).encode("utf-8"),
            result.stdout,
        )
        self.assertEqual(b"error", result.stderr)
        self.assertIsNone(result.overflow)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.returncode = 1

    @mock.patch("sidecar.process_runner.subprocess.Popen")
    def test_input_overflow_is_reported_before_spawn(self, popen):
        result = run_bounded(
            [sys.executable, "-c", "pass"],
            b"too large",
            input_limit=3,
            stdout_limit=10,
            stderr_limit=10,
            timeout=1,
        )

        popen.assert_not_called()
        self.assertEqual("input", result.overflow)
        self.assertEqual(-1, result.returncode)
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX pipe selectors required")
    def test_stdout_and_stderr_overflow_are_capped_and_reaped(self):
        for descriptor, stream_name in ((1, "stdout"), (2, "stderr")):
            with self.subTest(stream=stream_name):
                with tempfile.TemporaryDirectory() as temporary:
                    pid_path = Path(temporary) / "child.pid"
                    code = (
                        "import os,sys;"
                        "open(sys.argv[1],'w').write(str(os.getpid()));"
                        "fd=int(sys.argv[2]);chunk=b'x'*65536;"
                        "\nwhile True: os.write(fd,chunk)"
                    )

                    result = run_bounded(
                        [
                            sys.executable,
                            "-c",
                            code,
                            str(pid_path),
                            str(descriptor),
                        ],
                        input_limit=1,
                        stdout_limit=1024,
                        stderr_limit=1024,
                        timeout=2,
                    )

                    pid = int(pid_path.read_text(encoding="ascii"))
                    self.assertEqual(stream_name, result.overflow)
                    self.assertLessEqual(len(result.stdout), 1024)
                    self.assertLessEqual(len(result.stderr), 1024)
                    self.assertFalse(process_exists(pid))

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_timeout_preserves_output_and_kills_descendant_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid_path = root / "child.pid"
            descendant_pid_path = root / "descendant.pid"
            descendant_code = (
                "import os,sys,time;"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "time.sleep(60)"
            )
            code = (
                "import os,subprocess,sys,time;"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "subprocess.Popen([sys.executable,'-c',sys.argv[3],sys.argv[2]]);"
                "os.write(1,b'bounded-out');os.write(2,b'bounded-err');"
                "time.sleep(60)"
            )
            started = time.monotonic()

            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                run_bounded(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(child_pid_path),
                        str(descendant_pid_path),
                        descendant_code,
                    ],
                    input_limit=1,
                    stdout_limit=1024,
                    stderr_limit=1024,
                    timeout=0.5,
                )

            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(b"bounded-out", raised.exception.output)
            self.assertEqual(b"bounded-err", raised.exception.stderr)
            child_pid = int(child_pid_path.read_text(encoding="ascii"))
            descendant_pid = int(descendant_pid_path.read_text(encoding="ascii"))
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and (
                process_exists(child_pid) or process_exists(descendant_pid)
            ):
                time.sleep(0.01)
            self.assertFalse(process_exists(child_pid))
            self.assertFalse(process_exists(descendant_pid))

    @unittest.skipUnless(os.name == "posix", "POSIX pipe selectors required")
    def test_timeout_survives_child_closing_both_output_pipes(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "child.pid"
            code = (
                "import os,sys,time;"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "os.close(1);os.close(2);time.sleep(60)"
            )
            started = time.monotonic()

            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded(
                    [sys.executable, "-c", code, str(pid_path)],
                    input_limit=1,
                    stdout_limit=1,
                    stderr_limit=1,
                    timeout=0.2,
                )

            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(process_exists(int(pid_path.read_text(encoding="ascii"))))

    def test_cancel_raises_timeout_expired_with_bounded_output(self):
        cancel_event = threading.Event()
        timer = threading.Timer(0.15, cancel_event.set)
        timer.start()
        code = "import os,time;os.write(1,b'ready');time.sleep(60)"
        started = time.monotonic()
        try:
            with self.assertRaises(subprocess.TimeoutExpired) as raised:
                run_bounded(
                    [sys.executable, "-c", code],
                    input_limit=1,
                    stdout_limit=1024,
                    stderr_limit=1024,
                    timeout=2,
                    cancel_event=cancel_event,
                )
        finally:
            timer.cancel()
            timer.join()

        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(b"ready", raised.exception.output)

    @mock.patch("sidecar.process_runner.subprocess.Popen")
    def test_preexisting_cancel_is_honored_before_spawn(self, popen):
        cancel_event = threading.Event()
        cancel_event.set()

        with self.assertRaises(subprocess.TimeoutExpired) as raised:
            run_bounded(
                [sys.executable, "-c", "pass"],
                input_limit=1,
                stdout_limit=1,
                stderr_limit=1,
                timeout=1,
                cancel_event=cancel_event,
            )

        popen.assert_not_called()
        self.assertEqual(b"", raised.exception.output)
        self.assertEqual(b"", raised.exception.stderr)

    def _assert_parent_signal_cleanup(self, exit_signal):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid_path = root / "child.pid"
            descendant_pid_path = root / "descendant.pid"
            child_code = (
                "import os,subprocess,sys,time;"
                'code="import os,sys,time;'
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                'time.sleep(60)";'
                "subprocess.Popen([sys.executable,'-c',code,sys.argv[2]]);"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "time.sleep(60)"
            )
            parent_code = (
                "import signal,sys\n"
                "from sidecar.process_runner import run_bounded\n"
                "try:\n"
                "    run_bounded(\n"
                "        [sys.executable,'-c',sys.argv[3],sys.argv[1],sys.argv[2]],\n"
                "        input_limit=1,stdout_limit=1024,"
                "stderr_limit=1024,timeout=30,\n"
                "    )\n"
                "except KeyboardInterrupt:\n"
                "    values=(signal.SIGTERM,signal.SIGHUP)\n"
                "    if any(signal.getsignal(value) != signal.SIG_DFL "
                "for value in values):\n"
                "        raise SystemExit(7)\n"
                "    raise SystemExit(130)\n"
            )
            parent = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    parent_code,
                    str(child_pid_path),
                    str(descendant_pid_path),
                    child_code,
                ],
                cwd=str(REPO_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid = None
            descendant_pid = None
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and (
                    not child_pid_path.exists() or not descendant_pid_path.exists()
                ):
                    if parent.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertTrue(child_pid_path.exists())
                self.assertTrue(descendant_pid_path.exists())
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                descendant_pid = int(descendant_pid_path.read_text(encoding="ascii"))

                started = time.monotonic()
                os.kill(parent.pid, exit_signal)
                returncode = parent.wait(timeout=3)

                self.assertLess(time.monotonic() - started, 2)
                self.assertEqual(130, returncode)
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and (
                    process_exists(child_pid) or process_exists(descendant_pid)
                ):
                    time.sleep(0.01)
                self.assertFalse(process_exists(child_pid))
                self.assertFalse(process_exists(descendant_pid))
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait()
                for pid in (child_pid, descendant_pid):
                    if pid is not None and process_exists(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    @unittest.skipUnless(os.name == "posix", "exit signals require POSIX")
    def test_parent_exit_signals_kill_and_reap_child_process_group(self):
        for exit_signal in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(exit_signal=exit_signal):
                self._assert_parent_signal_cleanup(exit_signal)

    @unittest.skipUnless(os.name == "posix", "exit signals require POSIX")
    def test_exit_signal_handlers_restore_custom_ignore_and_popen_error(self):
        previous_handlers = {
            value: signal.getsignal(value) for value in (signal.SIGTERM, signal.SIGHUP)
        }

        def custom_handler(_signum, _frame):
            return None

        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGHUP, signal.SIG_DFL)
            run_bounded(
                [sys.executable, "-c", "pass"],
                input_limit=1,
                stdout_limit=1,
                stderr_limit=1,
                timeout=1,
            )
            self.assertEqual(
                signal.SIG_DFL,
                signal.getsignal(signal.SIGTERM),
            )
            self.assertEqual(
                signal.SIG_DFL,
                signal.getsignal(signal.SIGHUP),
            )

            with mock.patch(
                "sidecar.process_runner.subprocess.Popen",
                side_effect=OSError("spawn failed"),
            ):
                with self.assertRaises(OSError):
                    run_bounded(
                        [sys.executable, "-c", "pass"],
                        input_limit=1,
                        stdout_limit=1,
                        stderr_limit=1,
                        timeout=1,
                    )
            self.assertEqual(
                signal.SIG_DFL,
                signal.getsignal(signal.SIGTERM),
            )
            self.assertEqual(
                signal.SIG_DFL,
                signal.getsignal(signal.SIGHUP),
            )

            signal.signal(signal.SIGTERM, custom_handler)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            run_bounded(
                [sys.executable, "-c", "pass"],
                input_limit=1,
                stdout_limit=1,
                stderr_limit=1,
                timeout=1,
            )
            self.assertIs(
                custom_handler,
                signal.getsignal(signal.SIGTERM),
            )
            self.assertEqual(
                signal.SIG_IGN,
                signal.getsignal(signal.SIGHUP),
            )
        finally:
            for value, previous_handler in previous_handlers.items():
                signal.signal(value, previous_handler)

    @unittest.skipUnless(os.name == "posix", "SIGTERM handlers require POSIX")
    def test_worker_thread_does_not_change_signal_handlers(self):
        errors = []

        def run_in_worker():
            try:
                run_bounded(
                    [sys.executable, "-c", "pass"],
                    input_limit=1,
                    stdout_limit=1,
                    stderr_limit=1,
                    timeout=1,
                )
            except BaseException as error:
                errors.append(error)

        with mock.patch("sidecar.process_runner.signal.signal") as set_handler:
            thread = threading.Thread(target=run_in_worker)
            thread.start()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        set_handler.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "exit signals require POSIX")
    def test_nested_process_guard_restores_handlers_and_mask(self):
        managed_signals = (signal.SIGTERM, signal.SIGHUP)
        previous_handlers = {
            value: signal.getsignal(value) for value in managed_signals
        }
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

        def custom_handler(_signum, _frame):
            return None

        try:
            for value in managed_signals:
                signal.signal(value, signal.SIG_DFL)

            with bounded_execution_signal_guard() as outer_registry:
                for value in managed_signals:
                    self.assertNotEqual(
                        signal.SIG_DFL,
                        signal.getsignal(value),
                    )
                with bounded_execution_signal_guard() as inner_registry:
                    self.assertIs(outer_registry, inner_registry)
                    run_bounded(
                        [sys.executable, "-c", "pass"],
                        input_limit=1,
                        stdout_limit=1,
                        stderr_limit=1,
                        timeout=1,
                    )

            for value in managed_signals:
                self.assertEqual(
                    signal.SIG_DFL,
                    signal.getsignal(value),
                )

            signal.signal(signal.SIGTERM, custom_handler)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            with bounded_execution_signal_guard():
                self.assertIs(
                    custom_handler,
                    signal.getsignal(signal.SIGTERM),
                )
                self.assertEqual(
                    signal.SIG_IGN,
                    signal.getsignal(signal.SIGHUP),
                )
            self.assertIs(
                custom_handler,
                signal.getsignal(signal.SIGTERM),
            )
            self.assertEqual(
                signal.SIG_IGN,
                signal.getsignal(signal.SIGHUP),
            )
            self.assertEqual(
                previous_mask,
                signal.pthread_sigmask(signal.SIG_BLOCK, set()),
            )
        finally:
            for value, previous_handler in previous_handlers.items():
                signal.signal(value, previous_handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    @unittest.skipUnless(os.name == "posix", "process registry requires POSIX")
    def test_process_guard_closes_spawn_to_registration_race(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "child.pid"
            child_code = (
                "import os,sys,time;"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "time.sleep(60)"
            )
            real_popen = subprocess.Popen
            spawned = threading.Event()
            release_spawn = threading.Event()
            errors = []

            def delayed_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                spawned.set()
                release_spawn.wait(timeout=2)
                return process

            def run_in_worker():
                try:
                    run_bounded(
                        [sys.executable, "-c", child_code, str(pid_path)],
                        input_limit=1,
                        stdout_limit=1,
                        stderr_limit=1,
                        timeout=5,
                    )
                except BaseException as error:
                    errors.append(error)

            with bounded_execution_signal_guard() as registry:
                with mock.patch(
                    "sidecar.process_runner.subprocess.Popen",
                    side_effect=delayed_popen,
                ):
                    worker = threading.Thread(target=run_in_worker)
                    worker.start()
                    self.assertTrue(spawned.wait(timeout=2))
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and not pid_path.exists():
                        time.sleep(0.01)
                    self.assertTrue(pid_path.exists())
                    child_pid = int(pid_path.read_text(encoding="ascii"))

                    cleanup = threading.Thread(target=registry.kill_all)
                    cleanup.start()
                    release_spawn.set()
                    cleanup.join(timeout=2)
                    worker.join(timeout=2)

            self.assertFalse(cleanup.is_alive())
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertFalse(process_exists(child_pid))

    def test_invalid_arguments_are_rejected_before_spawn(self):
        valid = {
            "input_limit": 1,
            "stdout_limit": 1,
            "stderr_limit": 1,
            "timeout": 1,
        }
        cases = (
            ([], {}, ValueError),
            ("echo", {}, ValueError),
            ([sys.executable, ""], {}, ValueError),
            ([sys.executable, 3], {}, ValueError),
            ([sys.executable], {"input_limit": 0}, ValueError),
            ([sys.executable], {"stdout_limit": True}, ValueError),
            ([sys.executable], {"stderr_limit": -1}, ValueError),
            ([sys.executable], {"timeout": 0}, ValueError),
            ([sys.executable], {"timeout": float("nan")}, ValueError),
            ([sys.executable], {"timeout": float("inf")}, ValueError),
            (
                [sys.executable],
                {"timeout": MAX_TIMEOUT_SECONDS + 1},
                ValueError,
            ),
            ([sys.executable], {"cwd": b"/tmp"}, TypeError),
        )

        with mock.patch("sidecar.process_runner.subprocess.Popen") as popen:
            for argv, changes, error_type in cases:
                kwargs = dict(valid)
                kwargs.update(changes)
                with self.subTest(argv=argv, changes=changes):
                    with self.assertRaises(error_type):
                        run_bounded(argv, **kwargs)
            with self.assertRaises(TypeError):
                run_bounded([sys.executable], input_data="text", **valid)

        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
