import dataclasses
import json
import os
import select
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import sidecar.process_runner as process_runner_module
from sidecar.process_runner import (
    MAX_STREAM_INPUT_BYTES,
    MAX_STREAM_LINE_BYTES,
    MAX_STREAM_STDERR_BYTES,
    MAX_TIMEOUT_SECONDS,
    BoundedDuplexLineProcess,
    BoundedDuplexLineProcessCancelledError,
    BoundedDuplexLineProcessEOFError,
    BoundedDuplexLineProcessError,
    BoundedDuplexLineProcessOverflowError,
    BoundedDuplexLineProcessTimeoutError,
    BoundedLineStream,
    BoundedLineStreamCancelledError,
    BoundedLineStreamEndReason,
    BoundedLineStreamOverflowError,
    BoundedLineStreamProcessError,
    BoundedLineStreamTimeoutError,
    BoundedProcessResult,
    DescendantContainmentUnsupportedError,
    DuplexWriteBoundary,
    _DarwinKqueueDescendantTracker,
    _LinuxProcessGroupTracker,
    _ProcessGroupOwnership,
    _ProcessRegistry,
    _kill_process_group,
    bounded_execution_signal_guard,
    run_bounded,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def process_exists(pid):
    if sys.platform.startswith("linux"):
        try:
            payload = Path("/proc/{}/stat".format(pid)).read_text(encoding="ascii")
        except FileNotFoundError:
            return False
        except OSError:
            pass
        else:
            suffix = payload.rsplit(")", 1)
            fields = suffix[1].split() if len(suffix) == 2 else ()
            if fields:
                return fields[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid_when_ready(path, deadline):
    candidate = None
    while time.monotonic() < deadline:
        try:
            payload = path.read_text(encoding="ascii")
        except FileNotFoundError:
            payload = ""
        if payload.isascii() and payload.isdecimal():
            value = int(payload)
            if value > 0 and str(value) == payload:
                if value == candidate:
                    return value
                candidate = value
            else:
                candidate = None
        else:
            candidate = None
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    raise AssertionError("PID file was not ready before deadline")


class ProcessRunnerTests(unittest.TestCase):
    @staticmethod
    def _proc_entry(pid, stat_line):
        entry = mock.MagicMock()
        entry.name = str(pid)
        entry.__truediv__.return_value.read_text.return_value = stat_line
        return entry

    def test_linux_tracker_counts_live_group_members_and_ignores_zombies(self):
        live = self._proc_entry(1235, "1235 (worker) S 1 1234\n")
        zombie = self._proc_entry(1236, "1236 (worker) Z 1 1234\n")
        other_group = self._proc_entry(1237, "1237 (worker) S 1 9999\n")
        malformed = self._proc_entry(1238, "not a proc stat line")
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(
            os, "getpgid", return_value=1234
        ), mock.patch.object(os, "killpg"), mock.patch.object(
            Path, "iterdir", return_value=(live, zombie, other_group, malformed)
        ):
            tracker = _LinuxProcessGroupTracker(1234)
            self.assertEqual((1234,), tracker.sample(force=True))
            self.assertFalse(tracker.terminate())
            self.assertTrue(tracker.reliable)
            self.assertFalse(tracker.cleanup_incomplete)
            tracker.close()

    def test_linux_tracker_degrades_closed_proc_and_accepts_empty_group(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(
            os, "getpgid", return_value=1234
        ), mock.patch.object(os, "killpg"), mock.patch.object(
            Path, "iterdir", side_effect=OSError("proc unavailable")
        ):
            tracker = _LinuxProcessGroupTracker(1234)
            self.assertEqual((1234,), tracker.sample())
            self.assertFalse(tracker.terminate())

        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(
            os, "getpgid", return_value=1234
        ), mock.patch.object(os, "killpg"), mock.patch.object(
            Path, "iterdir", return_value=()
        ):
            tracker = _LinuxProcessGroupTracker(1234)
            self.assertEqual((), tracker.sample())
            self.assertTrue(tracker.terminate())

    def test_process_exists_distinguishes_linux_live_and_zombie_states(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(
            Path,
            "read_text",
            side_effect=(
                "123 (live child) S 1 2 3\n",
                "124 (zombie child) Z 1 2 3\n",
            ),
        ), mock.patch.object(os, "kill") as kill:
            self.assertTrue(process_exists(123))
            self.assertFalse(process_exists(124))
        kill.assert_not_called()

        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(
            os,
            "kill",
        ) as kill:
            self.assertTrue(process_exists(125))
        kill.assert_called_once_with(125, 0)

    def test_read_pid_when_ready_retries_missing_empty_and_partial_files(self):
        path = mock.Mock(spec=Path)
        path.read_text.side_effect = (
            FileNotFoundError(),
            "",
            "12",
            "123",
            "123",
        )

        self.assertEqual(123, read_pid_when_ready(path, time.monotonic() + 1))
        with self.assertRaises(AssertionError):
            read_pid_when_ready(path, time.monotonic())

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
        self.assertFalse(result.cleanup_incomplete)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.returncode = 1

    @unittest.skipUnless(os.name == "posix", "POSIX pipe selectors required")
    def test_spurious_blocking_write_retries_multichunk_input_exactly(self):
        input_data = bytes(range(256)) * 600 + b"exact-tail"
        write_attempts = []
        real_write = os.write

        def write_with_spurious_block(fd, data):
            write_attempts.append(bytes(data))
            if len(write_attempts) == 1:
                raise BlockingIOError("spurious would-block")
            return real_write(fd, data)

        with mock.patch(
            "sidecar.process_runner.os.write",
            side_effect=write_with_spurious_block,
        ):
            result = run_bounded(
                [
                    sys.executable,
                    "-c",
                    "import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())",
                ],
                input_data,
                input_limit=len(input_data),
                stdout_limit=len(input_data),
                stderr_limit=1,
                timeout=5,
            )

        self.assertEqual(0, result.returncode)
        self.assertEqual(input_data, result.stdout)
        self.assertEqual(b"", result.stderr)
        self.assertIsNone(result.overflow)
        self.assertGreater(len(write_attempts), 2)
        self.assertEqual(write_attempts[0], write_attempts[1])
        self.assertLess(len(write_attempts[0]), len(input_data))

    @mock.patch("sidecar.process_runner.subprocess.Popen")
    def test_input_overflow_is_reported_before_spawn(self, popen):
        pre_spawn = mock.Mock()
        result = run_bounded(
            [sys.executable, "-c", "pass"],
            b"too large",
            input_limit=3,
            stdout_limit=10,
            stderr_limit=10,
            timeout=1,
            pre_spawn=pre_spawn,
        )

        popen.assert_not_called()
        pre_spawn.assert_not_called()
        self.assertEqual("input", result.overflow)
        self.assertEqual(-1, result.returncode)
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)

    def test_pre_spawn_runs_after_registry_bookkeeping_immediately_before_popen(
        self,
    ):
        events = []
        registry = _ProcessRegistry()
        begin_spawn = registry.begin_spawn
        abort_spawn = registry.abort_spawn
        popen_error = OSError("spawn failed")
        pre_spawn = mock.Mock(side_effect=lambda: events.append("pre_spawn"))

        def recording_begin_spawn():
            events.append("begin_spawn")
            begin_spawn()

        def recording_abort_spawn():
            events.append("abort_spawn")
            abort_spawn()

        def failing_popen(*_args, **_kwargs):
            events.append("popen")
            raise popen_error

        with mock.patch.object(
            registry,
            "begin_spawn",
            side_effect=recording_begin_spawn,
        ), mock.patch.object(
            registry,
            "abort_spawn",
            side_effect=recording_abort_spawn,
        ), mock.patch(
            "sidecar.process_runner._current_process_registry",
            return_value=registry,
        ), mock.patch(
            "sidecar.process_runner.subprocess.Popen",
            side_effect=failing_popen,
        ):
            with self.assertRaises(OSError) as raised:
                run_bounded(
                    [sys.executable, "-c", "pass"],
                    input_limit=1,
                    stdout_limit=1,
                    stderr_limit=1,
                    timeout=1,
                    pre_spawn=pre_spawn,
                )

        self.assertIs(popen_error, raised.exception)
        self.assertEqual(
            ["begin_spawn", "pre_spawn", "popen", "abort_spawn"],
            events,
        )
        pre_spawn.assert_called_once_with()
        self.assertEqual(0, registry._spawning)
        self.assertTrue(registry.wait_empty(timeout=0))

    @unittest.skipUnless(os.name == "posix", "signal restoration requires POSIX")
    def test_pre_spawn_revalidation_exception_prevents_spawn_without_leak(self):
        expected_identity = object()
        current_identity = {"value": expected_identity}
        rejection = RuntimeError("identity changed")
        previous_handlers = {
            value: signal.getsignal(value) for value in (signal.SIGTERM, signal.SIGHUP)
        }
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())

        def revalidate_identity():
            if current_identity["value"] is not expected_identity:
                raise rejection

        pre_spawn = mock.Mock(side_effect=revalidate_identity)
        current_identity["value"] = object()
        registry = _ProcessRegistry()

        with mock.patch(
            "sidecar.process_runner._current_process_registry",
            return_value=registry,
        ), mock.patch("sidecar.process_runner.subprocess.Popen") as popen:
            with self.assertRaises(RuntimeError) as raised:
                run_bounded(
                    [sys.executable, "-c", "pass"],
                    input_limit=1,
                    stdout_limit=1,
                    stderr_limit=1,
                    timeout=1,
                    pre_spawn=pre_spawn,
                )

        self.assertIs(rejection, raised.exception)
        pre_spawn.assert_called_once_with()
        popen.assert_not_called()
        self.assertEqual(0, registry._spawning)
        self.assertTrue(registry.wait_empty(timeout=0))
        for value, previous_handler in previous_handlers.items():
            self.assertEqual(previous_handler, signal.getsignal(value))
        self.assertEqual(
            previous_mask,
            signal.pthread_sigmask(signal.SIG_BLOCK, set()),
        )

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

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_timeout_kills_descendant_after_group_leader_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            descendant_pid_path = Path(temporary) / "descendant.pid"
            descendant_code = (
                "import os,sys,time;"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "os.write(1,b'descendant-ready');"
                "time.sleep(60)"
            )
            leader_code = (
                "import subprocess,sys;"
                "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]])"
            )
            descendant_pid = None

            try:
                with self.assertRaises(subprocess.TimeoutExpired) as raised:
                    run_bounded(
                        [
                            sys.executable,
                            "-c",
                            leader_code,
                            str(descendant_pid_path),
                            descendant_code,
                        ],
                        input_limit=1,
                        stdout_limit=1024,
                        stderr_limit=1024,
                        timeout=0.5,
                    )

                self.assertEqual(b"descendant-ready", raised.exception.output)
                descendant_pid = int(
                    descendant_pid_path.read_text(encoding="ascii")
                )
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and process_exists(descendant_pid):
                    time.sleep(0.01)
                self.assertFalse(process_exists(descendant_pid))
            finally:
                if descendant_pid is None and descendant_pid_path.exists():
                    descendant_pid = int(
                        descendant_pid_path.read_text(encoding="ascii")
                    )
                if descendant_pid is not None and process_exists(descendant_pid):
                    try:
                        os.kill(descendant_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(os.name == "posix", "lease inheritance requires POSIX")
    def test_detached_descendant_lease_delays_success_until_exit(self):
        code = (
            "import os,time;"
            "pid=os.fork();"
            "\nif pid == 0:\n"
            " os.setsid();os.close(0);os.close(1);os.close(2);"
            "time.sleep(.35);os._exit(0)\n"
            "time.sleep(.1);os._exit(0)"
        )
        started = time.monotonic()

        result = run_bounded(
            [sys.executable, "-c", code],
            input_limit=1,
            stdout_limit=1,
            stderr_limit=1,
            timeout=2,
        )

        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual(0, result.returncode)
        self.assertFalse(result.cleanup_incomplete)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin kqueue required")
    def test_twenty_fast_detached_runs_are_never_complete(self):
        pre_exec = mock.Mock()
        target = (
            "import os;"
            "pid=os.fork();"
            "\nif pid == 0:\n"
            " os.setsid();pid=os.fork();"
            "\n if pid == 0:\n"
            "  os.closerange(0,256);os._exit(0)\n"
            " os._exit(0)\n"
            "os._exit(0)"
        )
        for _index in range(20):
            result = run_bounded(
                [sys.executable, "-c", target],
                input_limit=1,
                stdout_limit=1,
                stderr_limit=1,
                timeout=1,
                require_descendant_containment=True,
                pre_exec=pre_exec,
            )
            self.assertEqual(0, result.returncode)
            self.assertTrue(result.cleanup_incomplete)
        self.assertEqual(20, pre_exec.call_count)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin kqueue required")
    def test_synchronous_child_fork_is_conservatively_incomplete(self):
        target = (
            "import os;"
            "pid=os.fork();"
            "\nif pid == 0: os._exit(0)\n"
            "os.waitpid(pid,0)"
        )
        result = run_bounded(
            [sys.executable, "-c", target],
            input_limit=1,
            stdout_limit=1,
            stderr_limit=1,
            timeout=1,
            require_descendant_containment=True,
        )
        self.assertEqual(0, result.returncode)
        self.assertTrue(result.cleanup_incomplete)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin kqueue required")
    def test_pre_exec_failure_never_releases_target_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "target-ran"
            rejection = RuntimeError("identity changed")
            with self.assertRaises(RuntimeError) as raised:
                run_bounded(
                    [
                        sys.executable,
                        "-c",
                        "open(__import__('sys').argv[1],'w').write('ran')",
                        str(marker),
                    ],
                    input_limit=1,
                    stdout_limit=1,
                    stderr_limit=1,
                    timeout=1,
                    require_descendant_containment=True,
                    pre_exec=lambda: (_ for _ in ()).throw(rejection),
                )

            self.assertIs(rejection, raised.exception)
            self.assertFalse(marker.exists())

    @unittest.skipUnless(sys.platform == "darwin", "Darwin kqueue required")
    def test_supervisor_preserves_exact_stdin_argv_and_hides_target_argv(self):
        secret_argument = "TARGET-SECRET-ARGUMENT"
        input_data = b"exact supervisor stdin"
        popen_argv = []
        real_popen = subprocess.Popen

        def recording_popen(argv, **kwargs):
            popen_argv.append(tuple(argv))
            return real_popen(argv, **kwargs)

        code = (
            "import json,sys;"
            "data=sys.stdin.buffer.read();"
            "sys.stdout.write(json.dumps([sys.argv[1],data.decode('ascii')]))"
        )
        with mock.patch(
            "sidecar.process_runner.subprocess.Popen",
            side_effect=recording_popen,
        ):
            result = run_bounded(
                [sys.executable, "-c", code, secret_argument],
                input_data,
                input_limit=len(input_data),
                stdout_limit=1024,
                stderr_limit=1024,
                timeout=2,
                require_descendant_containment=True,
            )

        self.assertEqual(
            [secret_argument, input_data.decode("ascii")],
            json.loads(result.stdout.decode("utf-8")),
        )
        self.assertEqual(1, len(popen_argv))
        self.assertNotIn(secret_argument, repr(popen_argv[0]))
        self.assertFalse(result.cleanup_incomplete)

    def test_default_runners_never_initialize_kqueue_tracking(self):
        with mock.patch(
            "sidecar.process_runner._DarwinKqueueDescendantTracker",
            side_effect=AssertionError("unexpected process tracking"),
        ):
            result = run_bounded(
                [sys.executable, "-c", "print('snapshot')"],
                input_limit=1,
                stdout_limit=64,
                stderr_limit=64,
                timeout=1,
            )
            with BoundedLineStream(
                [sys.executable, "-c", "print('stream')"],
                line_limit=64,
                startup_timeout=1,
            ) as stream:
                self.assertEqual([b"stream"], list(stream))

        self.assertEqual(b"snapshot\n", result.stdout)

    @mock.patch("sidecar.process_runner.subprocess.Popen")
    def test_required_containment_unsupported_never_spawns_target(self, popen):
        with mock.patch.object(
            process_runner_module,
            "_containment_tracker_class",
            return_value=None,
        ):
            with self.assertRaises(DescendantContainmentUnsupportedError):
                run_bounded(
                    [sys.executable, "-c", "raise SystemExit('target ran')"],
                    input_limit=1,
                    stdout_limit=1,
                    stderr_limit=1,
                    timeout=1,
                    require_descendant_containment=True,
                )
        popen.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "Darwin kqueue required")
    def test_kqueue_fork_is_permanent_incomplete_without_pid_kill(self):
        class FakeKqueue:
            def __init__(self):
                self.batches = [
                    [
                        mock.Mock(
                            ident=1234,
                            fflags=select.KQ_NOTE_FORK | select.KQ_NOTE_EXIT,
                        )
                    ]
                ]

            def control(self, changes, _maximum, _timeout):
                if changes is not None:
                    return []
                return self.batches.pop(0) if self.batches else []

            def close(self):
                return None

        fake = FakeKqueue()
        with mock.patch("sidecar.process_runner.select.kqueue", return_value=fake):
            tracker = _DarwinKqueueDescendantTracker(1234)
            with mock.patch("sidecar.process_runner.os.kill") as kill:
                self.assertEqual((), tracker.sample())
                self.assertTrue(tracker.cleanup_incomplete)
                self.assertFalse(tracker.terminate())
            kill.assert_not_called()
            tracker.close()

    @unittest.skipUnless(sys.platform == "darwin", "Darwin kqueue required")
    def test_kqueue_event_overflow_is_cleanup_incomplete(self):
        class FakeKqueue:
            def __init__(self):
                self.batches = [
                    [
                        mock.Mock(ident=1234, fflags=select.KQ_NOTE_FORK)
                        for _index in range(16)
                    ]
                ]

            def control(self, changes, _maximum, _timeout):
                if changes is not None:
                    return []
                return self.batches.pop(0) if self.batches else []

            def close(self):
                return None

        with mock.patch(
            "sidecar.process_runner.select.kqueue",
            return_value=FakeKqueue(),
        ):
            tracker = _DarwinKqueueDescendantTracker(1234)
            with mock.patch("sidecar.process_runner.os.kill") as kill:
                self.assertFalse(tracker.terminate())
                self.assertTrue(tracker.cleanup_incomplete)
            kill.assert_not_called()
            tracker.close()

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
                child_pid = read_pid_when_ready(child_pid_path, deadline)
                descendant_pid = read_pid_when_ready(descendant_pid_path, deadline)

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

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    @mock.patch("sidecar.process_runner.os.killpg")
    def test_kill_process_group_does_not_signal_after_leader_reaped(self, killpg):
        process = mock.Mock()
        process.pid = 1234
        process.poll.return_value = 0
        ownership = _ProcessGroupOwnership(process.pid)

        self.assertEqual(0, ownership.poll(process))
        _kill_process_group(process, ownership)

        self.assertEqual(2, process.poll.call_count)
        killpg.assert_not_called()
        process.kill.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    @mock.patch("sidecar.process_runner.os.killpg")
    def test_kill_process_group_signals_owned_group_after_leader_exits(
        self,
        killpg,
    ):
        process = mock.Mock()
        process.pid = 1234
        process.poll.return_value = 0
        ownership = _ProcessGroupOwnership(process.pid)

        _kill_process_group(process, ownership)

        process.poll.assert_not_called()
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        process.kill.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    @mock.patch("sidecar.process_runner.os.killpg")
    def test_delayed_registry_kill_cannot_run_after_ownership_release(
        self,
        killpg,
    ):
        process = mock.Mock()
        process.pid = 1234
        process.poll.return_value = 0
        ownership = _ProcessGroupOwnership(process.pid)
        registry = _ProcessRegistry()
        registry.begin_spawn()
        registry.complete_spawn(process, ownership)
        kill_delayed = threading.Event()
        allow_kill = threading.Event()
        real_kill_process_group = _kill_process_group

        def delayed_kill(delayed_process, delayed_ownership):
            kill_delayed.set()
            allow_kill.wait(timeout=2)
            real_kill_process_group(delayed_process, delayed_ownership)

        with mock.patch(
            "sidecar.process_runner._kill_process_group",
            side_effect=delayed_kill,
        ):
            cleanup = threading.Thread(target=registry.kill_all)
            cleanup.start()
            try:
                self.assertTrue(kill_delayed.wait(timeout=2))
                registry.unregister(process)
            finally:
                allow_kill.set()
                cleanup.join(timeout=2)

        self.assertFalse(cleanup.is_alive())
        killpg.assert_not_called()
        process.kill.assert_not_called()
        self.assertTrue(registry.wait_empty())

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
                    child_pid = read_pid_when_ready(
                        pid_path, time.monotonic() + 2
                    )

                    cleanup = threading.Thread(target=registry.kill_all)
                    cleanup.start()
                    release_spawn.set()
                    cleanup.join(timeout=2)
                    worker.join(timeout=2)

            self.assertFalse(cleanup.is_alive())
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertFalse(process_exists(child_pid))

    @unittest.skipUnless(os.name == "posix", "POSIX pipe selectors required")
    def test_line_stream_uploads_multichunk_input_and_assembles_lines(self):
        input_data = bytes(range(256)) * 700 + b"exact-tail"
        real_write = os.write
        writes = []

        def write_with_spurious_block(fd, data):
            writes.append(bytes(data))
            if len(writes) == 1:
                raise BlockingIOError("spurious would-block")
            return real_write(fd, data)

        code = (
            "import os,sys,time;"
            "data=sys.stdin.buffer.read();"
            "assert data.endswith(b'exact-tail');"
            "os.write(1,b'first');time.sleep(.02);"
            "os.write(1,b'-line\\nsecond\\nthird');"
        )
        with mock.patch(
            "sidecar.process_runner.os.write",
            side_effect=write_with_spurious_block,
        ):
            with BoundedLineStream(
                [sys.executable, "-c", code],
                input_data,
                line_limit=64,
                startup_timeout=2,
            ) as stream:
                self.assertEqual(
                    [b"first-line", b"second", b"third"],
                    list(stream),
                )
                result = stream.result

        self.assertIsNotNone(result)
        self.assertEqual(BoundedLineStreamEndReason.EOF, result.end_reason)
        self.assertEqual(0, result.returncode)
        self.assertEqual(3, result.lines_yielded)
        self.assertEqual(len(b"first-line\nsecond\nthird"), result.stdout_bytes_read)
        self.assertEqual(writes[0], writes[1])
        self.assertGreater(len(writes), 2)

    def test_line_stream_line_overflow_is_typed_and_exactly_bounded(self):
        code = "import os,time;os.write(1,b'x'*65);time.sleep(60)"
        with BoundedLineStream(
            [sys.executable, "-c", code],
            line_limit=64,
            startup_timeout=2,
        ) as stream:
            with self.assertRaises(BoundedLineStreamOverflowError) as raised:
                next(stream)

        self.assertEqual(
            BoundedLineStreamEndReason.LINE_OVERFLOW,
            raised.exception.result.end_reason,
        )
        self.assertEqual(65, raised.exception.result.stdout_bytes_read)
        self.assertEqual(b"", raised.exception.result.stderr)
        self.assertEqual(0, raised.exception.result.lines_yielded)

    def test_line_stream_stderr_overflow_is_typed_and_capped(self):
        code = "import os,time;os.write(2,b'e'*65);time.sleep(60)"
        with BoundedLineStream(
            [sys.executable, "-c", code],
            line_limit=64,
            stderr_limit=64,
            startup_timeout=2,
        ) as stream:
            with self.assertRaises(BoundedLineStreamOverflowError) as raised:
                next(stream)

        result = raised.exception.result
        self.assertEqual(BoundedLineStreamEndReason.STDERR_OVERFLOW, result.end_reason)
        self.assertEqual(b"e" * 64, result.stderr)

    def test_line_stream_startup_timeout_kills_and_reaps(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "child.pid"
            code = (
                "import os,sys,time;"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "time.sleep(60)"
            )
            stream = BoundedLineStream(
                [sys.executable, "-c", code, str(pid_path)],
                line_limit=64,
                startup_timeout=0.15,
            )
            with self.assertRaises(BoundedLineStreamTimeoutError) as raised:
                next(stream)

            pid = int(pid_path.read_text(encoding="ascii"))
            self.assertEqual(
                BoundedLineStreamEndReason.STARTUP_TIMEOUT,
                raised.exception.result.end_reason,
            )
            self.assertFalse(process_exists(pid))

    def test_line_stream_is_indefinite_after_first_line_then_cancels(self):
        cancel_event = threading.Event()
        startup_timeout = 2
        clock_offset = 0.0

        def monotonic():
            return time.monotonic() + clock_offset

        code = (
            "import os,time;"
            "os.write(1,b'ready\\nstill-running\\n');"
            "time.sleep(60)"
        )
        with BoundedLineStream(
            [sys.executable, "-c", code],
            line_limit=64,
            startup_timeout=startup_timeout,
            cancel_event=cancel_event,
            monotonic=monotonic,
        ) as stream:
            self.assertEqual(b"ready", next(stream))
            self.assertTrue(stream.ready)
            clock_offset += startup_timeout + 1
            self.assertEqual(b"still-running", next(stream))
            cancel_event.set()
            with self.assertRaises(BoundedLineStreamCancelledError) as raised:
                next(stream)

        self.assertEqual(
            BoundedLineStreamEndReason.CANCELLED,
            raised.exception.result.end_reason,
        )

    def test_line_stream_supports_caller_defined_ready_and_deadline_reset(self):
        startup_timeout = 2
        reset_timeout = 5
        clock_offset = 0.0

        def monotonic():
            return time.monotonic() + clock_offset

        code = "import os;os.write(1,b'connecting\\nready\\n')"
        with BoundedLineStream(
            [sys.executable, "-c", code],
            line_limit=64,
            startup_timeout=startup_timeout,
            ready_on_first_line=False,
            monotonic=monotonic,
        ) as stream:
            self.assertEqual(b"connecting", next(stream))
            self.assertFalse(stream.ready)
            stream.reset_startup_timeout(reset_timeout)
            clock_offset += startup_timeout + 1
            self.assertEqual(b"ready", next(stream))
            stream.mark_ready()
            clock_offset += reset_timeout + 1
            with self.assertRaises(StopIteration):
                next(stream)

        self.assertEqual(BoundedLineStreamEndReason.EOF, stream.end_reason)

    def test_line_stream_early_close_kills_owned_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "child.pid"
            code = (
                "import os,sys,time;"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "os.write(1,b'ready\\n');time.sleep(60)"
            )
            with BoundedLineStream(
                [sys.executable, "-c", code, str(pid_path)],
                line_limit=64,
                startup_timeout=2,
            ) as stream:
                self.assertEqual(b"ready", next(stream))
                pid = int(pid_path.read_text(encoding="ascii"))
                stream.close()
                self.assertEqual(BoundedLineStreamEndReason.CLOSED, stream.end_reason)
                self.assertIsNotNone(stream.returncode)

            self.assertFalse(process_exists(pid))

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_line_stream_close_kills_descendant_after_leader_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            descendant_pid_path = Path(temporary) / "descendant.pid"
            descendant_code = (
                "import os,sys,time;"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "os.write(1,b'ready\\n');time.sleep(60)"
            )
            leader_code = (
                "import subprocess,sys;"
                "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]])"
            )
            with BoundedLineStream(
                [
                    sys.executable,
                    "-c",
                    leader_code,
                    str(descendant_pid_path),
                    descendant_code,
                ],
                line_limit=64,
                startup_timeout=2,
            ) as stream:
                self.assertEqual(b"ready", next(stream))
                descendant_pid = int(
                    descendant_pid_path.read_text(encoding="ascii")
                )
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and stream.returncode is None:
                    time.sleep(0.01)
                self.assertEqual(0, stream.returncode)

            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and process_exists(descendant_pid):
                time.sleep(0.01)
            self.assertFalse(process_exists(descendant_pid))

    @unittest.skipUnless(os.name == "posix", "process registry requires POSIX")
    def test_line_stream_worker_is_owned_by_outer_signal_guard(self):
        started = threading.Event()
        errors = []

        def run_in_worker():
            try:
                with BoundedLineStream(
                    [
                        sys.executable,
                        "-c",
                        "import os,time;os.write(1,b'ready\\n');time.sleep(60)",
                    ],
                    line_limit=64,
                    startup_timeout=2,
                ) as stream:
                    self.assertEqual(b"ready", next(stream))
                    started.set()
                    next(stream)
            except BoundedLineStreamProcessError:
                pass
            except BaseException as error:
                errors.append(error)

        with bounded_execution_signal_guard():
            worker = threading.Thread(target=run_in_worker)
            worker.start()
            self.assertTrue(started.wait(timeout=2))

        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)

    def test_line_stream_yields_unterminated_eof_line_once(self):
        with BoundedLineStream(
            [sys.executable, "-c", "import os;os.write(1,b'tail')"],
            line_limit=64,
            startup_timeout=2,
        ) as stream:
            self.assertEqual([b"tail"], list(stream))

        self.assertEqual(BoundedLineStreamEndReason.EOF, stream.end_reason)
        self.assertEqual(0, stream.returncode)
        self.assertEqual(1, stream.result.lines_yielded)

    def test_line_stream_has_no_total_cap_for_many_lines(self):
        count = 1000
        code = (
            "import os,sys;"
            "count=int(sys.argv[1]);"
            "[os.write(1,(str(i)+'\\n').encode()) for i in range(count)]"
        )
        expected = [str(value).encode("ascii") for value in range(count)]
        with BoundedLineStream(
            [sys.executable, "-c", code, str(count)],
            line_limit=8,
            startup_timeout=2,
        ) as stream:
            self.assertEqual(expected, list(stream))

        expected_bytes = sum(len(line) + 1 for line in expected)
        self.assertEqual(count, stream.result.lines_yielded)
        self.assertEqual(expected_bytes, stream.result.stdout_bytes_read)

    def test_line_stream_nonzero_eof_is_not_silent_success(self):
        with BoundedLineStream(
            [
                sys.executable,
                "-c",
                "import os,sys;os.write(1,b'last\\n');sys.exit(7)",
            ],
            line_limit=64,
            startup_timeout=2,
        ) as stream:
            self.assertEqual(b"last", next(stream))
            with self.assertRaises(BoundedLineStreamProcessError) as raised:
                next(stream)

        self.assertEqual(7, raised.exception.result.returncode)
        self.assertEqual(
            BoundedLineStreamEndReason.NONZERO_EXIT,
            raised.exception.result.end_reason,
        )

    @unittest.skipUnless(os.name == "posix", "POSIX pipe selectors required")
    def test_duplex_process_assembles_split_and_coalesced_lines(self):
        code = (
            "import os,time;"
            "os.write(1,b'first');time.sleep(.02);"
            "os.write(1,b'-line\\nsecond\\nthird\\n');"
            "time.sleep(.02)"
        )
        with BoundedDuplexLineProcess(
            [sys.executable, "-c", code],
            line_limit=64,
            stdout_limit=256,
        ) as process:
            deadline = time.monotonic() + 2
            self.assertEqual(b"first-line", process.read_line(deadline=deadline))
            self.assertEqual(b"second", process.read_line(deadline=deadline))
            self.assertEqual(b"third", process.read_line(deadline=deadline))
            self.assertIsNone(process.read_line(deadline=deadline))
            process.close_stdin()
            result = process.wait_clean(deadline=deadline)

        self.assertEqual(0, result.returncode)
        self.assertTrue(result.clean_exit)
        self.assertTrue(result.cleanup_complete)
        self.assertEqual(
            len(b"first-line\nsecond\nthird\n"),
            result.stdout_bytes_read,
        )

    def test_duplex_process_write_boundary_tracks_final_newline(self):
        clock = {"value": 0.0}
        real_write = os.write
        with BoundedDuplexLineProcess(
            [
                sys.executable,
                "-c",
                "import sys,time;sys.stdin.buffer.read();time.sleep(60)",
            ],
            line_limit=64,
            stdout_limit=64,
            monotonic=lambda: (
                time.monotonic()
                if clock["value"] is None
                else clock["value"]
            ),
        ) as process:
            def partial_then_expire(fd, data):
                count = real_write(fd, bytes(data[:2]))
                clock["value"] = 2.0
                return count

            with mock.patch(
                "sidecar.process_runner.os.write",
                side_effect=partial_then_expire,
            ):
                partial = process.write_line(b"private-frame", deadline=1.0)
            clock["value"] = None

        self.assertEqual(DuplexWriteBoundary.PARTIAL, partial.boundary)
        self.assertEqual(2, partial.bytes_written)
        self.assertEqual(len(b"private-frame\n"), partial.bytes_total)
        self.assertNotIn(b"private-frame", repr(partial).encode("utf-8"))

        real_write = os.write
        write_sizes = []

        def short_write(fd, data):
            count = min(2, len(data))
            write_sizes.append(count)
            return real_write(fd, bytes(data[:count]))

        with BoundedDuplexLineProcess(
            [
                sys.executable,
                "-c",
                "import os,sys;os.write(1,sys.stdin.buffer.readline())",
            ],
            line_limit=64,
            stdout_limit=64,
        ) as process:
            with mock.patch(
                "sidecar.process_runner.os.write",
                side_effect=short_write,
            ):
                complete = process.write_line(
                    b"abcdef",
                    deadline=time.monotonic() + 2,
                )
            self.assertEqual(
                b"abcdef",
                process.read_line(deadline=time.monotonic() + 2),
            )
            process.close_stdin()
            result = process.wait_clean(deadline=time.monotonic() + 2)

        self.assertEqual(DuplexWriteBoundary.COMPLETE, complete.boundary)
        self.assertEqual(7, complete.bytes_written)
        self.assertGreater(len(write_sizes), 2)
        self.assertTrue(result.clean_exit)

    def test_duplex_partial_write_boundary_survives_exception_paths(self):
        def new_process(cancel_event=None):
            return BoundedDuplexLineProcess(
                [
                    sys.executable,
                    "-c",
                    "import sys,time;sys.stdin.buffer.read();time.sleep(60)",
                ],
                line_limit=64,
                stdout_limit=64,
                cancel_event=cancel_event,
            )

        process = new_process()
        try:
            calls = {"count": 0}

            def overflow_after_partial(_deadline):
                calls["count"] += 1
                if calls["count"] == 1:
                    key = next(
                        key
                        for key in process._selector.get_map().values()
                        if key.data[0] == "stdin"
                    )
                    return ((key, selectors.EVENT_WRITE),)
                process._raise_overflow("stdout")

            with mock.patch.object(
                process,
                "_select",
                side_effect=overflow_after_partial,
            ), mock.patch(
                "sidecar.process_runner.os.write",
                return_value=2,
            ):
                with self.assertRaises(
                    BoundedDuplexLineProcessOverflowError
                ) as raised:
                    process.write_line(
                        b"private-frame",
                        deadline=time.monotonic() + 2,
                    )
            self.assertEqual(
                DuplexWriteBoundary.PARTIAL,
                raised.exception.write_result.boundary,
            )
            self.assertIs(raised.exception.write_result, process.write_result)
        finally:
            process.close()

        cancel_event = threading.Event()
        process = new_process(cancel_event)
        try:
            def write_then_cancel(_fd, _data):
                cancel_event.set()
                return 2

            with mock.patch(
                "sidecar.process_runner.os.write",
                side_effect=write_then_cancel,
            ):
                with self.assertRaises(
                    BoundedDuplexLineProcessCancelledError
                ) as raised:
                    process.write_line(
                        b"private-frame",
                        deadline=time.monotonic() + 2,
                    )
            self.assertEqual(
                DuplexWriteBoundary.PARTIAL,
                raised.exception.write_result.boundary,
            )
        finally:
            process.close()

        process = new_process()
        try:
            calls = {"count": 0}

            def selector_failure(_deadline):
                calls["count"] += 1
                if calls["count"] == 1:
                    key = next(
                        key
                        for key in process._selector.get_map().values()
                        if key.data[0] == "stdin"
                    )
                    return ((key, selectors.EVENT_WRITE),)
                raise RuntimeError("selector detail")

            with mock.patch.object(
                process,
                "_select",
                side_effect=selector_failure,
            ), mock.patch(
                "sidecar.process_runner.os.write",
                return_value=2,
            ):
                with self.assertRaises(
                    BoundedDuplexLineProcessError
                ) as raised:
                    process.write_line(
                        b"private-frame",
                        deadline=time.monotonic() + 2,
                    )
            self.assertEqual(
                DuplexWriteBoundary.PARTIAL,
                raised.exception.write_result.boundary,
            )
            self.assertNotIn("selector detail", str(raised.exception))
        finally:
            process.close()

    def test_duplex_rechecks_deadline_after_selector_before_io(self):
        clock = {"value": 0.0}

        def monotonic():
            if clock["value"] is None:
                return time.monotonic()
            return clock["value"]

        with BoundedDuplexLineProcess(
            [sys.executable, "-c", "import time;time.sleep(60)"],
            line_limit=64,
            stdout_limit=64,
            monotonic=monotonic,
        ) as process:
            def write_ready_after_deadline(_deadline):
                clock["value"] = 2.0
                key = next(
                    key
                    for key in process._selector.get_map().values()
                    if key.data[0] == "stdin"
                )
                return ((key, selectors.EVENT_WRITE),)

            with mock.patch.object(
                process,
                "_select",
                side_effect=write_ready_after_deadline,
            ), mock.patch("sidecar.process_runner.os.write") as write:
                result = process.write_line(b"frame", deadline=1.0)
            self.assertEqual(DuplexWriteBoundary.NONE, result.boundary)
            write.assert_not_called()
            clock["value"] = None

        clock["value"] = 0.0
        with BoundedDuplexLineProcess(
            [sys.executable, "-c", "import time;time.sleep(60)"],
            line_limit=64,
            stdout_limit=64,
            monotonic=monotonic,
        ) as process:
            def read_ready_after_deadline(_deadline):
                clock["value"] = 2.0
                key = next(
                    key
                    for key in process._selector.get_map().values()
                    if key.data[0] == "stdout"
                )
                return ((key, selectors.EVENT_READ),)

            with mock.patch.object(
                process,
                "_select",
                side_effect=read_ready_after_deadline,
            ), mock.patch("sidecar.process_runner.os.read") as read:
                with self.assertRaises(
                    BoundedDuplexLineProcessTimeoutError
                ):
                    process.read_line(deadline=1.0)
            read.assert_not_called()
            clock["value"] = None

    def test_duplex_process_absolute_deadlines_before_and_after_write(self):
        clock = {"value": 5.0}
        with BoundedDuplexLineProcess(
            [sys.executable, "-c", "import time;time.sleep(60)"],
            line_limit=64,
            stdout_limit=64,
            monotonic=lambda: (
                time.monotonic()
                if clock["value"] is None
                else clock["value"]
            ),
        ) as process:
            unwritten = process.write_line(b"frame", deadline=4.0)
            self.assertEqual(DuplexWriteBoundary.NONE, unwritten.boundary)
            with self.assertRaises(
                BoundedDuplexLineProcessTimeoutError
            ) as raised:
                process.read_line(deadline=4.0)
            clock["value"] = None

        self.assertNotIn("frame", str(raised.exception))
        self.assertNotIn("frame", repr(raised.exception))

        with BoundedDuplexLineProcess(
            [
                sys.executable,
                "-c",
                "import sys,time;sys.stdin.buffer.readline();time.sleep(60)",
            ],
            line_limit=64,
            stdout_limit=64,
        ) as process:
            complete = process.write_line(
                b"private-frame",
                deadline=time.monotonic() + 2,
            )
            with self.assertRaises(BoundedDuplexLineProcessTimeoutError):
                process.read_line(deadline=time.monotonic() + 0.05)
        self.assertEqual(DuplexWriteBoundary.COMPLETE, complete.boundary)

    def test_duplex_process_line_aggregate_stderr_and_eof_are_bounded(self):
        cases = (
            (
                "import os,time;os.write(1,b'x'*65+b'\\n');time.sleep(60)",
                {"line_limit": 64, "stdout_limit": 128},
                "stdout_line",
            ),
            (
                "import os,time;os.write(1,b'a\\n'*33);time.sleep(60)",
                {"line_limit": 8, "stdout_limit": 64},
                "stdout",
            ),
            (
                "import os,time;os.write(2,b'e'*65);time.sleep(60)",
                {"line_limit": 64, "stdout_limit": 64, "stderr_limit": 64},
                "stderr",
            ),
        )
        for code, kwargs, expected in cases:
            with self.subTest(expected=expected):
                with BoundedDuplexLineProcess(
                    [sys.executable, "-c", code],
                    **kwargs,
                ) as process:
                    with self.assertRaises(
                        BoundedDuplexLineProcessOverflowError
                    ):
                        process.read_line(deadline=time.monotonic() + 2)
                    self.assertEqual(expected, process._overflow)
                    self.assertLessEqual(len(process.stderr), 64)

        with BoundedDuplexLineProcess(
            [sys.executable, "-c", "import os;os.write(1,b'partial')"],
            line_limit=64,
            stdout_limit=64,
        ) as process:
            with self.assertRaises(BoundedDuplexLineProcessEOFError):
                process.read_line(deadline=time.monotonic() + 2)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_duplex_wait_then_terminate_reaps_lingering_descendant(self):
        with tempfile.TemporaryDirectory() as temporary:
            child_pid_path = Path(temporary) / "child.pid"
            child_code = (
                "import os,sys,time;"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "time.sleep(60)"
            )
            leader_code = (
                "import os,subprocess,sys;"
                "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]]);"
                "os.write(1,b'ready\\n')"
            )
            with BoundedDuplexLineProcess(
                [
                    sys.executable,
                    "-c",
                    leader_code,
                    str(child_pid_path),
                    child_code,
                ],
                line_limit=64,
                stdout_limit=64,
            ) as process:
                self.assertEqual(
                    b"ready",
                    process.read_line(deadline=time.monotonic() + 2),
                )
                child_pid = read_pid_when_ready(
                    child_pid_path,
                    time.monotonic() + 2,
                )
                observed = process.wait_clean(deadline=time.monotonic() + 0.05)
                self.assertFalse(observed.cleanup_complete)
                result = process.terminate_tree(
                    deadline=time.monotonic() + 2
                )

            self.assertIsNotNone(result.returncode)
            self.assertTrue(result.cleanup_complete)
            self.assertFalse(process_exists(child_pid))

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_duplex_terminate_escalates_past_ignored_term(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "pid"
            code = (
                "import os,signal,sys,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "os.write(1,b'ready\\n');time.sleep(60)"
            )
            with BoundedDuplexLineProcess(
                [sys.executable, "-c", code, str(pid_path)],
                line_limit=64,
                stdout_limit=64,
            ) as process:
                self.assertEqual(
                    b"ready",
                    process.read_line(deadline=time.monotonic() + 2),
                )
                pid = int(pid_path.read_text(encoding="ascii"))
                result = process.cancel(deadline=time.monotonic() + 1)

            self.assertIsNotNone(result.returncode)
            self.assertNotEqual(0, result.returncode)
            self.assertTrue(result.cleanup_complete)
            self.assertFalse(process_exists(pid))

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "fork"),
        "forked process groups require POSIX",
    )
    def test_duplex_terminate_kills_forked_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "fork.pid"
            code = (
                "import os,sys,time;"
                "pid=os.fork();"
                "\nif pid == 0:\n"
                " open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(60)\n"
                "os.write(1,b'ready\\n');time.sleep(60)"
            )
            with BoundedDuplexLineProcess(
                [sys.executable, "-c", code, str(pid_path)],
                line_limit=64,
                stdout_limit=64,
            ) as process:
                self.assertEqual(
                    b"ready",
                    process.read_line(deadline=time.monotonic() + 2),
                )
                fork_pid = read_pid_when_ready(
                    pid_path,
                    time.monotonic() + 2,
                )
                result = process.terminate_tree(
                    deadline=time.monotonic() + 2
                )

            self.assertTrue(result.cleanup_complete)
            self.assertFalse(process_exists(fork_pid))

    @unittest.skipUnless(os.name == "posix", "containment lease requires POSIX")
    def test_duplex_darwin_observer_uncertainty_is_conservative(self):
        class UncertainObserver:
            reliable = True
            cleanup_incomplete = True

            def __init__(self):
                self.closed = False

            def sample(self, *, force=False):
                del force
                return ()

            def close(self):
                self.closed = True

        process = BoundedDuplexLineProcess(
            [sys.executable, "-c", "pass"],
            line_limit=64,
            stdout_limit=64,
        )
        observer = UncertainObserver()
        try:
            process._started.descendant_tracker = observer
            result = process.wait_clean(deadline=time.monotonic() + 2)
            retried = process.terminate_tree(deadline=time.monotonic() + 1)

            self.assertEqual(0, result.returncode)
            self.assertFalse(result.cleanup_complete)
            self.assertFalse(result.clean_exit)
            self.assertFalse(process._closed)
            self.assertFalse(observer.closed)
            self.assertFalse(retried.cleanup_complete)
            self.assertFalse(process._closed)
            self.assertFalse(observer.closed)
        finally:
            process.close()
        self.assertTrue(observer.closed)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin kqueue required")
    def test_duplex_darwin_setsid_escape_remains_retryable_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "escaped.pid"
            code = (
                "import os,sys,time;"
                "pid=os.fork();"
                "\nif pid == 0:\n"
                " os.setsid();"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "os.closerange(0,256);time.sleep(60);os._exit(0)\n"
                "os.write(1,b'ready\\n');os._exit(0)"
            )
            process = BoundedDuplexLineProcess(
                [sys.executable, "-c", code, str(pid_path)],
                line_limit=64,
                stdout_limit=64,
                require_descendant_containment=True,
            )
            escaped_pid = None
            try:
                self.assertEqual(
                    b"ready",
                    process.read_line(deadline=time.monotonic() + 2),
                )
                escaped_pid = read_pid_when_ready(
                    pid_path, time.monotonic() + 2
                )
                observed = process.wait_clean(
                    deadline=time.monotonic() + 1
                )
                retried = process.terminate_tree(
                    deadline=time.monotonic() + 1
                )

                self.assertFalse(observed.cleanup_complete)
                self.assertFalse(retried.cleanup_complete)
                self.assertFalse(process._closed)
                self.assertIsNone(process.result)
                self.assertTrue(process_exists(escaped_pid))
            finally:
                if escaped_pid is not None and process_exists(escaped_pid):
                    os.kill(escaped_pid, signal.SIGKILL)
                    deadline = time.monotonic() + 2
                    while process_exists(escaped_pid) and time.monotonic() < deadline:
                        time.sleep(0.01)
                process.close()

    @unittest.skipUnless(os.name == "posix", "process cleanup requires POSIX")
    def test_duplex_constructor_and_close_exceptions_still_reap_processes(self):
        real_start = process_runner_module._start_bounded_process
        started_pids = []

        def capture_start(*args, **kwargs):
            started = real_start(*args, **kwargs)
            started_pids.append(started.process.pid)
            return started

        with mock.patch.object(
            process_runner_module,
            "_start_bounded_process",
            side_effect=capture_start,
        ), mock.patch.object(
            BoundedDuplexLineProcess,
            "_configure_streams",
            side_effect=OSError("synthetic stream setup failure"),
        ):
            with self.assertRaises(OSError):
                BoundedDuplexLineProcess(
                    [sys.executable, "-c", "import time;time.sleep(60)"],
                    line_limit=64,
                    stdout_limit=64,
                )

        self.assertEqual(1, len(started_pids))
        self.assertFalse(process_exists(started_pids[0]))

        process = BoundedDuplexLineProcess(
            [sys.executable, "-c", "import time;time.sleep(60)"],
            line_limit=64,
            stdout_limit=64,
        )
        pid = process.identity.pid
        with mock.patch.object(
            process,
            "terminate_tree",
            side_effect=RuntimeError("synthetic terminate failure"),
        ):
            with self.assertRaises(RuntimeError):
                process.close()

        self.assertTrue(process._closed)
        self.assertFalse(process_exists(pid))

    @mock.patch("sidecar.process_runner.subprocess.Popen")
    def test_line_stream_validates_hard_limits_before_spawn(self, popen):
        cases = (
            ({"line_limit": 0, "startup_timeout": 1}, ValueError),
            (
                {
                    "line_limit": MAX_STREAM_LINE_BYTES + 1,
                    "startup_timeout": 1,
                },
                ValueError,
            ),
            (
                {
                    "line_limit": 1,
                    "stderr_limit": MAX_STREAM_STDERR_BYTES + 1,
                    "startup_timeout": 1,
                },
                ValueError,
            ),
            ({"line_limit": 1, "startup_timeout": 0}, ValueError),
            (
                {
                    "line_limit": 1,
                    "startup_timeout": 1,
                    "ready_on_first_line": 1,
                },
                TypeError,
            ),
        )
        for kwargs, error_type in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(error_type):
                    BoundedLineStream([sys.executable], **kwargs)

        with self.assertRaises(BoundedLineStreamOverflowError) as raised:
            BoundedLineStream(
                [sys.executable],
                b"x" * (MAX_STREAM_INPUT_BYTES + 1),
                line_limit=1,
                startup_timeout=1,
            )
        self.assertEqual(
            BoundedLineStreamEndReason.INPUT_OVERFLOW,
            raised.exception.result.end_reason,
        )
        popen.assert_not_called()

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
            ([sys.executable], {"pre_spawn": object()}, TypeError),
            ([sys.executable], {"pre_exec": object()}, TypeError),
            (
                [sys.executable],
                {"require_descendant_containment": 1},
                TypeError,
            ),
            ([sys.executable], {"pre_exec": lambda: None}, ValueError),
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
