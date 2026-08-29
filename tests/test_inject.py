import dataclasses
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import sidecar.inject as inject_module
from sidecar.inject import (
    KIMI_SUPPORTED_VERSION,
    MAX_MESSAGE_BYTES,
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    SendError,
    SendPlan,
    SendResult,
    build_send_plan,
    execute_send,
    validate_message,
)
from sidecar.kimi_acp import (
    AcpPhase,
    KimiAcpResult,
    PromptWriteBoundary,
    build_kimi_child_env,
    run_kimi_acp,
)
from sidecar.model import Session, Status
from sidecar.process_runner import (
    AcpProcessIdentity,
    BoundedProcessResult,
    BoundedDuplexLineProcess,
    DescendantContainmentUnsupportedError,
    run_bounded,
)


def stat_mode(path):
    return os.stat(str(path)).st_mode & 0o777


def honoring_runner(runner):
    def wrapped(*args, **kwargs):
        pre_exec = kwargs.get("pre_exec")
        if not callable(pre_exec):
            raise AssertionError("runner requires pre_exec")
        pre_exec()
        return runner(*args, **kwargs)

    return wrapped


def copy_private_executable(source, destination):
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise unittest.SkipTest("O_NOFOLLOW is unavailable")
    source_fd = -1
    destination_fd = -1
    try:
        source_fd = os.open(
            str(source),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow,
        )
        destination_fd = os.open(
            str(destination),
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | nofollow,
            0o700,
        )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                written = os.write(destination_fd, chunk[offset:])
                if written <= 0:
                    raise OSError("short private executable write")
                offset += written
        os.fchmod(destination_fd, 0o700)
        os.fsync(destination_fd)
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if source_fd >= 0:
            os.close(source_fd)
    details = destination.lstat()
    effective_uid = (
        os.geteuid() if callable(getattr(os, "geteuid", None)) else details.st_uid
    )
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != effective_uid
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise AssertionError("private executable copy is unsafe")


class InjectionTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.home_environment = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home)},
        )
        self.home_environment.start()
        self.account_home = mock.patch(
            "sidecar.send_audit.pwd.getpwuid",
            return_value=mock.Mock(pw_dir=str(self.home)),
        )
        self.account_home.start()
        self.executable = self.root / "agent-executable"
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executable.chmod(0o700)
        self.transcript = self.root / "transcript"
        self.transcript.write_text("{}\n", encoding="utf-8")
        self.runtime = self.root / "runtime"

    def tearDown(self):
        self.account_home.stop()
        self.home_environment.stop()
        self.temporary.cleanup()

    def session(
        self,
        agent="claude",
        *,
        status=Status.WAITING,
        session_id="session-123",
        project=None,
        parent_id=None,
        extra=None,
    ):
        return Session(
            agent=agent,
            session_id=session_id,
            project=str(self.root if project is None else project),
            transcript=str(self.transcript),
            updated_at=1.0,
            status=status,
            parent_id=parent_id,
            extra={} if extra is None else extra,
        )

    def resolver(self, calls):
        def resolve(name):
            calls.append(name)
            return str(self.executable)

        return resolve

    def plan(self, agent="claude", message="private prompt"):
        return build_send_plan(
            self.session(agent),
            message,
            executable_resolver=lambda _name: str(self.executable),
        )

    def execute_plan(self, plan, **kwargs):
        runner_invokes_pre_exec = kwargs.pop(
            "runner_invokes_pre_exec",
            False,
        )
        runner = kwargs.get("runner")
        if runner is not None and not runner_invokes_pre_exec:
            kwargs["runner"] = honoring_runner(runner)
        kwargs.setdefault(
            "refresher",
            lambda: [
                self.session(
                    plan.agent,
                    session_id=plan.session_id,
                )
            ],
        )
        kwargs.setdefault(
            "executable_resolver",
            lambda _name: str(self.executable),
        )
        kwargs.setdefault("runtime_dir", self.runtime)
        return execute_send(plan, **kwargs)


class MessageValidationTests(InjectionTestCase):
    def test_preserves_exact_utf8_without_terminal_sanitizing(self):
        message = " 你好\n\x1b[31mkeep\tspaces "

        self.assertEqual(message.encode("utf-8"), validate_message(message))

    def test_rejects_type_blank_nul_surrogate_and_oversize(self):
        cases = (
            (b"bytes", "invalid_message_type"),
            ("", "blank_message"),
            (" \t\n", "blank_message"),
            ("has\x00nul", "message_nul"),
            ("\ud800", "invalid_message_utf8"),
            ("x" * (MAX_MESSAGE_BYTES + 1), "message_too_large"),
        )
        for value, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(SendError) as raised:
                    validate_message(value)
                self.assertEqual(code, raised.exception.code)

    def test_size_limit_is_utf8_bytes_and_inclusive(self):
        exact = "é" * (MAX_MESSAGE_BYTES // 2)

        self.assertEqual(MAX_MESSAGE_BYTES, len(validate_message(exact)))
        with self.assertRaises(SendError) as raised:
            validate_message(exact + "é")
        self.assertEqual("message_too_large", raised.exception.code)


class PlanTests(InjectionTestCase):
    def test_claude_exact_argv_stdin_and_cwd(self):
        calls = []
        message = "Claude\n消息"

        plan = build_send_plan(
            self.session("claude"),
            message,
            executable_resolver=self.resolver(calls),
        )

        self.assertEqual(["claude"], calls)
        self.assertEqual(str(self.executable.resolve()), plan.executable)
        self.assertEqual(
            (
                plan.executable,
                "--print",
                "--resume",
                "session-123",
                "--input-format",
                "text",
                "--output-format",
                "json",
            ),
            plan.argv,
        )
        self.assertEqual(message.encode("utf-8"), plan.input_data)
        self.assertEqual("stdin", plan.prompt_transport)
        self.assertEqual(str(self.root), plan.cwd)

    def test_codex_exact_argv_and_stdin(self):
        calls = []
        plan = build_send_plan(
            self.session("codex"),
            "Codex prompt",
            executable_resolver=self.resolver(calls),
        )

        self.assertEqual(["codex"], calls)
        self.assertEqual(
            (
                plan.executable,
                "exec",
                "resume",
                "--json",
                "session-123",
                "-",
            ),
            plan.argv,
        )
        self.assertEqual(b"Codex prompt", plan.input_data)
        self.assertEqual("stdin", plan.prompt_transport)

    def test_cursor_cli_exact_argv_and_argv_transport(self):
        calls = []
        message = "Cursor prompt\nwith spaces"
        plan = build_send_plan(
            self.session("cursor-cli"),
            message,
            executable_resolver=self.resolver(calls),
        )

        self.assertEqual(["cursor-agent"], calls)
        self.assertEqual(
            (
                plan.executable,
                "--print",
                "--output-format",
                "json",
                "--resume",
                "session-123",
                "--",
                message,
            ),
            plan.argv,
        )
        self.assertIsNone(plan.input_data)
        self.assertEqual("argv", plan.prompt_transport)

    def test_plans_are_frozen_and_repr_hides_prompt_fields(self):
        message = "unique secret prompt"
        plan = self.plan("cursor-cli", message)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            plan.cwd = "/other"
        self.assertNotIn(message, repr(plan))
        self.assertNotIn("argv=", repr(plan))
        self.assertNotIn("input_data=", repr(plan))

    def test_explicitly_unsupported_agents_have_stable_reason_codes(self):
        expected = {
            "cursor-ide": "unsupported_cursor_ide",
            "copilot": "unsupported_copilot",
            "dsh": "unsupported_dsh",
            "cursor": "unsupported_agent",
            "CLAUDE": "unsupported_agent",
        }
        for agent, code in expected.items():
            with self.subTest(agent=agent):
                with self.assertRaises(SendError) as raised:
                    build_send_plan(
                        self.session(agent),
                        "prompt",
                        executable_resolver=lambda _name: str(self.executable),
                    )
                self.assertEqual(code, raised.exception.code)

    def test_rejects_working_dead_parent_sidechain_and_remote(self):
        cases = (
            (self.session(status=Status.WORKING), "working_session"),
            (self.session(status=Status.DEAD), "dead_session"),
            (self.session(parent_id="parent"), "child_session"),
            (self.session(extra={"sidechain": True}), "child_session"),
            (self.session(extra={"source": "remote"}), "remote_session"),
            (self.session(extra={"host": "edge"}), "remote_session"),
        )
        for session, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(SendError) as raised:
                    build_send_plan(
                        session,
                        "prompt",
                        executable_resolver=lambda _name: str(self.executable),
                    )
                self.assertEqual(code, raised.exception.code)

    def test_waiting_and_idle_sessions_are_eligible(self):
        for status in (Status.WAITING, Status.IDLE):
            with self.subTest(status=status):
                plan = build_send_plan(
                    self.session(status=status),
                    "prompt",
                    executable_resolver=lambda _name: str(self.executable),
                )
                self.assertEqual(status.value, self.session(status=status).status.value)
                self.assertEqual("session-123", plan.session_id)

    def test_session_identifier_is_ascii_bounded_and_option_safe(self):
        valid = ("a", "a-b_c.d:e", "a" * 512)
        for session_id in valid:
            with self.subTest(valid=session_id):
                plan = build_send_plan(
                    self.session(session_id=session_id),
                    "prompt",
                    executable_resolver=lambda _name: str(self.executable),
                )
                self.assertEqual(session_id, plan.session_id)

        invalid = (
            "",
            "-option",
            "has space",
            "slash/id",
            "back\\slash",
            "control\nid",
            "é",
            "a" * 513,
        )
        for session_id in invalid:
            with self.subTest(invalid=session_id):
                with self.assertRaises(SendError) as raised:
                    build_send_plan(
                        self.session(session_id=session_id),
                        "prompt",
                        executable_resolver=lambda _name: str(self.executable),
                    )
                self.assertEqual("invalid_session_id", raised.exception.code)

    def test_project_must_be_an_existing_absolute_directory(self):
        file_path = self.root / "file"
        file_path.write_text("not a directory", encoding="utf-8")
        for project in (
            "relative/project",
            self.root / "missing",
            file_path,
        ):
            with self.subTest(project=project):
                with self.assertRaises(SendError) as raised:
                    build_send_plan(
                        self.session(project=project),
                        "prompt",
                        executable_resolver=lambda _name: str(self.executable),
                    )
                self.assertEqual("invalid_project", raised.exception.code)

    def test_executable_must_resolve_to_regular_executable(self):
        missing = self.root / "missing"
        non_executable = self.root / "plain"
        non_executable.write_text("plain", encoding="utf-8")
        cases = (
            (lambda _name: None, "executable_not_found"),
            (lambda _name: str(missing), "executable_not_found"),
            (lambda _name: str(self.root), "invalid_executable"),
            (lambda _name: str(non_executable), "invalid_executable"),
            (lambda _name: b"bytes", "invalid_executable"),
        )
        for resolver, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(SendError) as raised:
                    build_send_plan(
                        self.session(),
                        "prompt",
                        executable_resolver=resolver,
                    )
                self.assertEqual(code, raised.exception.code)


class FunctionalSendPreflightTests(InjectionTestCase):
    def test_real_plan_rejects_mutated_paths_and_payload(self):
        plan = self.plan()
        invalid_cwd = dataclasses.replace(
            plan,
            cwd="relative/project",
            target=dataclasses.replace(plan.target, project="relative/project"),
        )
        with self.assertRaises(SendError) as raised:
            inject_module._preflight_plan(invalid_cwd, filesystem=False)
        self.assertEqual("invalid_plan", raised.exception.code)

        invalid_executable = "relative-executable"
        executable_identity = (
            invalid_executable,
        ) + plan.target.executable_identity[1:]
        invalid_executable_plan = dataclasses.replace(
            plan,
            executable=invalid_executable,
            target=dataclasses.replace(
                plan.target,
                executable_identity=executable_identity,
            ),
        )
        with self.assertRaises(SendError) as raised:
            inject_module._preflight_plan(invalid_executable_plan, filesystem=False)
        self.assertEqual("invalid_plan", raised.exception.code)

        changed_payload = dataclasses.replace(plan, input_data=b"\xff")
        with self.assertRaises(SendError) as raised:
            inject_module._preflight_plan(changed_payload, filesystem=False)
        self.assertEqual("invalid_plan", raised.exception.code)

    def test_real_plan_rejects_non_kimi_runtime_and_re_resolved_paths(self):
        plan = self.plan()
        with self.assertRaises(SendError) as raised:
            inject_module._preflight_plan(
                dataclasses.replace(
                    plan,
                    target=dataclasses.replace(plan.target, kimi_runtime=object()),
                ),
                filesystem=False,
            )
        self.assertEqual("invalid_plan", raised.exception.code)

        with mock.patch.object(
            inject_module,
            "_resolve_project",
            return_value=str(self.root / "other"),
        ):
            with self.assertRaises(SendError) as raised:
                inject_module._preflight_plan(plan)
        self.assertEqual("invalid_plan", raised.exception.code)

        with mock.patch.object(
            inject_module,
            "_resolve_executable",
            return_value=str(self.root / "other-executable"),
        ):
            with self.assertRaises(SendError) as raised:
                inject_module._preflight_plan(plan)
        self.assertEqual("invalid_executable", raised.exception.code)

    def test_real_plan_rejects_transport_and_target_contract_mutations(self):
        plan = self.plan()
        mutations = (
            dataclasses.replace(plan, agent="unknown"),
            dataclasses.replace(plan, target=object()),
            dataclasses.replace(plan, prompt_transport="argv"),
            dataclasses.replace(plan, transport="ndjson"),
            dataclasses.replace(plan, input_data=None),
        )
        for mutated in mutations:
            with self.subTest(mutated=mutated):
                with self.assertRaises(SendError) as raised:
                    inject_module._preflight_plan(mutated, filesystem=False)
                self.assertEqual("invalid_plan", raised.exception.code)

        nul_cwd = dataclasses.replace(
            plan,
            cwd="/tmp/\x00project",
            target=dataclasses.replace(plan.target, project="/tmp/\x00project"),
        )
        with self.assertRaises(SendError) as raised:
            inject_module._preflight_plan(nul_cwd, filesystem=False)
        self.assertEqual("invalid_plan", raised.exception.code)

        nul_executable = "/tmp/\x00executable"
        nul_executable_plan = dataclasses.replace(
            plan,
            executable=nul_executable,
            target=dataclasses.replace(
                plan.target,
                executable_identity=(nul_executable,)
                + plan.target.executable_identity[1:],
            ),
        )
        with self.assertRaises(SendError) as raised:
            inject_module._preflight_plan(nul_executable_plan, filesystem=False)
        self.assertEqual("invalid_plan", raised.exception.code)

    def test_real_session_lock_validates_anchored_runtime_during_lifecycle(self):
        runtime = self.root / "functional-lock-runtime"
        with inject_module._session_lock(
            "claude",
            "functional-lock-session",
            runtime,
        ) as validate:
            validate()

    def test_real_session_lock_reports_busy_runtime_without_replacing_owner(self):
        runtime = self.root / "functional-busy-runtime"
        with inject_module._session_lock(
            "claude",
            "functional-busy-session",
            runtime,
        ):
            with self.assertRaises(SendError) as raised:
                with inject_module._session_lock(
                    "claude",
                    "functional-busy-session",
                    runtime,
                ):
                    pass
            self.assertEqual("session_busy", raised.exception.code)

    def test_real_session_lock_translates_unexpected_flock_failure(self):
        runtime = self.root / "functional-flock-runtime"
        with mock.patch.object(
            inject_module.fcntl,
            "flock",
            side_effect=OSError(errno.EIO, "I/O failure"),
        ):
            with self.assertRaises(SendError) as raised:
                with inject_module._session_lock(
                    "claude",
                    "functional-flock-session",
                    runtime,
                ):
                    pass
        self.assertEqual("unsafe_lock", raised.exception.code)


class ExecutionTests(InjectionTestCase):
    def completed(
        self,
        plan,
        *,
        returncode=0,
        stdout=b"",
        stderr=b"",
        overflow=None,
        cleanup_incomplete=False,
    ):
        return BoundedProcessResult(
            args=plan.argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            overflow=overflow,
            cleanup_incomplete=cleanup_incomplete,
        )

    def assert_spawn_boundary_mutation_blocked(
        self,
        plan,
        mutate,
        *,
        refresher=None,
    ):
        spawn_calls = []

        def runner(*args, **kwargs):
            mutate()
            kwargs["pre_exec"]()
            spawn_calls.append((args, kwargs))
            return self.completed(plan)

        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                plan,
                allow_write=True,
                refresher=(
                    (lambda: [self.session()])
                    if refresher is None
                    else refresher
                ),
                runner=runner,
                runner_invokes_pre_exec=True,
            )

        self.assertEqual("session_changed", raised.exception.code)
        self.assertEqual([], spawn_calls)

    def test_hard_gate_is_identity_true_and_never_calls_runner(self):
        plan = self.plan()
        for allow_write in (False, None, 0, 1, "true"):
            calls = []

            def runner(*args, **kwargs):
                calls.append((args, kwargs))
                return self.completed(plan)

            with self.subTest(allow_write=allow_write):
                with self.assertRaises(SendError) as raised:
                    self.execute_plan(
                        plan,
                        allow_write=allow_write,
                        runner=runner,
                    )
                self.assertEqual("write_not_allowed", raised.exception.code)
                self.assertEqual([], calls)

    def test_runner_receives_exact_bounded_contract(self):
        message = "exact stdin"
        plan = self.plan("claude", message)
        calls = []

        def clock():
            return 10.0

        def runner(argv, input_data, **kwargs):
            calls.append((argv, input_data, kwargs))
            return self.completed(
                plan,
                stdout=json.dumps({"result": "native response"}).encode("utf-8"),
                stderr=b"warning",
            )

        result = self.execute_plan(
            plan,
            allow_write=True,
            timeout=900,
            runner=runner,
            monotonic=clock,
        )

        self.assertEqual(1, len(calls))
        argv, input_data, kwargs = calls[0]
        self.assertEqual(plan.argv, argv)
        self.assertEqual(message.encode("utf-8"), input_data)
        self.assertEqual(
            {
                "input_limit": MAX_MESSAGE_BYTES,
                "stdout_limit": MAX_STDOUT_BYTES,
                "stderr_limit": MAX_STDERR_BYTES,
                "timeout": 900.0,
                "env": None,
                "cwd": str(self.root),
                "pre_exec": mock.ANY,
                "require_descendant_containment": True,
                "monotonic": clock,
            },
            kwargs,
        )
        self.assertEqual("completed", result.outcome)
        self.assertEqual("delivered", result.delivery)
        self.assertEqual(0, result.returncode)
        self.assertEqual("native response", result.response)
        self.assertEqual("warning", result.stderr)

    @unittest.skipUnless(sys.platform == "darwin", "Darwin kqueue required")
    def test_default_no_fork_runner_remains_delivered(self):
        plan = self.plan()

        result = self.execute_plan(plan, allow_write=True)

        self.assertEqual("completed", result.outcome)
        self.assertEqual("delivered", result.delivery)
        self.assertEqual(0, result.returncode)

    def test_cursor_runner_gets_no_stdin(self):
        plan = self.plan("cursor-cli", "argv prompt")
        calls = []

        def runner(argv, input_data, **kwargs):
            calls.append((argv, input_data, kwargs))
            return self.completed(plan, stdout=b'{"result":"done"}')

        result = self.execute_plan(plan, allow_write=True, runner=runner)

        self.assertIsNone(calls[0][1])
        self.assertEqual("argv prompt", calls[0][0][-1])
        self.assertEqual("done", result.response)

    def test_timeout_is_unknown_and_uses_bounded_exception_output(self):
        message = "timeout secret"
        plan = self.plan(message=message)

        def runner(argv, input_data, **kwargs):
            del input_data, kwargs
            raise subprocess.TimeoutExpired(
                argv,
                2,
                output=json.dumps({"result": message}).encode("utf-8"),
                stderr=("failed " + message).encode("utf-8"),
            )

        result = self.execute_plan(plan, allow_write=True, runner=runner)

        self.assertEqual("timed_out", result.outcome)
        self.assertEqual("unknown", result.delivery)
        self.assertEqual("timeout", result.error_code)
        self.assertNotIn(message, result.response)
        self.assertNotIn(message, result.stderr)

    def test_incomplete_containment_is_never_reported_delivered(self):
        plan = self.plan()
        incomplete = self.execute_plan(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: self.completed(
                plan,
                returncode=0,
                stdout=b'{"result":"possibly still running"}',
                cleanup_incomplete=True,
            ),
        )

        self.assertEqual("completed", incomplete.outcome)
        self.assertEqual("unknown", incomplete.delivery)
        self.assertEqual("cleanup_incomplete", incomplete.error_code)
        self.assertEqual(0, incomplete.returncode)

        def timed_out(argv, input_data, **kwargs):
            del input_data, kwargs
            error = subprocess.TimeoutExpired(argv, 1)
            error.cleanup_incomplete = True
            raise error

        timeout = self.execute_plan(
            plan,
            allow_write=True,
            runner=timed_out,
        )
        self.assertEqual("timed_out", timeout.outcome)
        self.assertEqual("unknown", timeout.delivery)
        self.assertEqual("cleanup_incomplete", timeout.error_code)

    def test_overflow_wins_over_incomplete_cleanup_and_exposes_limit(self):
        plan = self.plan()
        result = self.execute_plan(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: self.completed(
                plan,
                returncode=-9,
                stderr=b"x" * MAX_STDERR_BYTES,
                overflow="stderr",
                cleanup_incomplete=True,
            ),
        )

        self.assertEqual("overflow", result.outcome)
        self.assertEqual("unknown", result.delivery)
        self.assertEqual("stderr_overflow", result.error_code)
        self.assertEqual("stderr", result.overflow)
        self.assertEqual(MAX_STDERR_BYTES, result.overflow_limit)
        self.assertEqual(MAX_STDERR_BYTES, result.to_dict()["overflow_limit"])

    def test_overflow_and_nonzero_are_unknown(self):
        plan = self.plan()
        overflow = self.execute_plan(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: self.completed(
                plan,
                returncode=-9,
                stdout=b"partial",
                overflow="stdout",
            ),
        )
        failed = self.execute_plan(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: self.completed(
                plan,
                returncode=7,
                stdout=b'{"result":"partial response"}',
                stderr=b"native error",
            ),
        )

        self.assertEqual(("overflow", "unknown"), (overflow.outcome, overflow.delivery))
        self.assertEqual("stdout_overflow", overflow.error_code)
        self.assertEqual("stdout", overflow.overflow)
        self.assertEqual(MAX_STDOUT_BYTES, overflow.overflow_limit)
        self.assertEqual(("failed", "unknown"), (failed.outcome, failed.delivery))
        self.assertEqual(7, failed.returncode)
        self.assertEqual("partial response", failed.response)
        self.assertEqual("native error", failed.stderr)

    def test_spawn_error_is_distinct_from_preflight_not_found(self):
        plan = self.plan()

        result = self.execute_plan(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("spawn failed")
            ),
        )

        self.assertEqual("failed", result.outcome)
        self.assertEqual("unknown", result.delivery)
        self.assertEqual("spawn_error", result.error_code)

        unsupported = self.execute_plan(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: (_ for _ in ()).throw(
                DescendantContainmentUnsupportedError("unsupported")
            ),
        )
        self.assertEqual("failed", unsupported.outcome)
        self.assertEqual("unknown", unsupported.delivery)
        self.assertEqual("containment_unsupported", unsupported.error_code)

        self.executable.unlink()
        calls = []
        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                plan,
                allow_write=True,
                runner=lambda *args, **kwargs: calls.append(args),
            )
        self.assertEqual("executable_not_found", raised.exception.code)
        self.assertEqual([], calls)

    def test_keyboard_interrupt_propagates(self):
        plan = self.plan()

        def runner(*args, **kwargs):
            del args, kwargs
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.execute_plan(plan, allow_write=True, runner=runner)

    def test_timeout_range_is_finite_inclusive_and_capped_at_900(self):
        plan = self.plan()
        calls = []

        def runner(*args, **kwargs):
            calls.append(kwargs["timeout"])
            return self.completed(plan)

        for timeout in (1, 1.5, 900):
            self.execute_plan(
                plan,
                allow_write=True,
                timeout=timeout,
                runner=runner,
            )
        self.assertEqual([1.0, 1.5, 900.0], calls)

        for timeout in (True, 0.999, 900.001, float("nan"), float("inf")):
            before = len(calls)
            with self.subTest(timeout=timeout):
                with self.assertRaises(SendError) as raised:
                    self.execute_plan(
                        plan,
                        allow_write=True,
                        timeout=timeout,
                        runner=runner,
                    )
                self.assertEqual("invalid_timeout", raised.exception.code)
                self.assertEqual(before, len(calls))

    def test_execution_requires_a_fresh_revalidator(self):
        plan = self.plan()
        calls = []

        with self.assertRaises(SendError) as raised:
            execute_send(
                plan,
                allow_write=True,
                runtime_dir=self.runtime,
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )

        self.assertEqual("revalidation_required", raised.exception.code)
        self.assertEqual([], calls)

    def test_source_mutation_between_plan_and_refresh_prevents_spawn(self):
        plan = self.plan(message="mutation secret")
        self.transcript.write_text('{"changed":true}\n', encoding="utf-8")
        calls = []

        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                plan,
                allow_write=True,
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )

        self.assertEqual("session_changed", raised.exception.code)
        self.assertEqual([], calls)
        self.assertNotIn("mutation secret", str(raised.exception))

    def test_source_mutation_at_spawn_boundary_prevents_spawn(self):
        plan = self.plan()
        self.assert_spawn_boundary_mutation_blocked(
            plan,
            lambda: self.transcript.write_text(
                '{"spawn_boundary":true}\n',
                encoding="utf-8",
            ),
        )

    def test_cwd_replacement_at_spawn_boundary_prevents_spawn(self):
        project = self.root / "spawn-project"
        project.mkdir()
        session = self.session(project=project)
        plan = build_send_plan(
            session,
            "private",
            executable_resolver=lambda _name: str(self.executable),
        )

        def replace_project():
            project.rename(self.root / "old-spawn-project")
            project.mkdir()

        self.assert_spawn_boundary_mutation_blocked(
            plan,
            replace_project,
            refresher=lambda: [self.session(project=project)],
        )

    def test_executable_replacement_at_spawn_boundary_prevents_spawn(self):
        plan = self.plan()

        def replace_executable():
            self.executable.rename(self.root / "old-spawn-executable")
            self.executable.write_text(
                "#!/bin/sh\nprintf replaced\n",
                encoding="utf-8",
            )
            self.executable.chmod(0o700)

        self.assert_spawn_boundary_mutation_blocked(
            plan,
            replace_executable,
        )

    def test_cursor_wal_creation_at_spawn_boundary_prevents_spawn(self):
        wal = Path(str(self.transcript) + "-wal")
        extra = {
            "source": "cli",
            "store": str(self.transcript),
            "wal": str(wal),
            "transcript_kind": "cursor-chat-sqlite",
            "directory_session_id": "session-123",
        }

        def cursor_session():
            return self.session("cursor-cli", extra=dict(extra))

        plan = build_send_plan(
            cursor_session(),
            "private",
            executable_resolver=lambda _name: str(self.executable),
        )
        self.assert_spawn_boundary_mutation_blocked(
            plan,
            lambda: wal.write_bytes(b"spawn boundary"),
            refresher=lambda: [cursor_session()],
        )

    def test_runner_without_pre_exec_contract_fails_closed(self):
        plan = self.plan()
        calls = []

        def legacy_runner(
            argv,
            input_data,
            *,
            input_limit,
            stdout_limit,
            stderr_limit,
            timeout,
            env,
            cwd,
            monotonic,
        ):
            calls.append((argv, input_data))
            return self.completed(plan)

        with self.assertRaises(TypeError):
            execute_send(
                plan,
                allow_write=True,
                refresher=lambda: [self.session()],
                executable_resolver=lambda _name: str(self.executable),
                runtime_dir=self.runtime,
                runner=legacy_runner,
            )

        self.assertEqual([], calls)

    def test_refresh_rejects_executable_switch_replacement_and_mode_change(self):
        cases = ("switch", "replacement", "rewrite", "mode")
        for case in cases:
            with self.subTest(case=case):
                self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                self.executable.chmod(0o700)
                plan = self.plan()
                resolver_path = self.executable
                if case == "switch":
                    resolver_path = self.root / "other-executable"
                    resolver_path.write_text(
                        "#!/bin/sh\nexit 0\n",
                        encoding="utf-8",
                    )
                    resolver_path.chmod(0o700)
                elif case == "replacement":
                    self.executable.rename(self.root / "old-executable")
                    self.executable.write_text(
                        "#!/bin/sh\nexit 0\n",
                        encoding="utf-8",
                    )
                    self.executable.chmod(0o700)
                elif case == "rewrite":
                    self.executable.write_text(
                        "#!/bin/sh\nprintf changed\n",
                        encoding="utf-8",
                    )
                else:
                    self.executable.chmod(0o755)
                calls = []

                with self.assertRaises(SendError) as raised:
                    self.execute_plan(
                        plan,
                        allow_write=True,
                        executable_resolver=lambda _name, path=resolver_path: str(
                            path
                        ),
                        runner=lambda *args, **kwargs: calls.append(
                            (args, kwargs)
                        ),
                    )

                self.assertEqual("session_changed", raised.exception.code)
                self.assertEqual([], calls)

    def test_refresh_rejects_invalid_replacement_executable(self):
        plan = self.plan()
        self.executable.chmod(0o600)
        calls = []

        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                plan,
                allow_write=True,
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )

        self.assertEqual("invalid_executable", raised.exception.code)
        self.assertEqual([], calls)

    def test_refresh_rejects_working_directory_inode_replacement(self):
        project = self.root / "replaceable-project"
        project.mkdir()
        session = self.session(project=project)
        plan = build_send_plan(
            session,
            "private",
            executable_resolver=lambda _name: str(self.executable),
        )
        project.rename(self.root / "old-project")
        project.mkdir()
        calls = []

        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                plan,
                allow_write=True,
                refresher=lambda: [self.session(project=project)],
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )

        self.assertEqual("session_changed", raised.exception.code)
        self.assertEqual([], calls)

    def test_cursor_wal_creation_and_removal_change_source_fingerprint(self):
        wal = Path(str(self.transcript) + "-wal")
        extra = {
            "source": "cli",
            "store": str(self.transcript),
            "wal": str(wal),
            "transcript_kind": "cursor-chat-sqlite",
            "directory_session_id": "session-123",
        }

        def cursor_session():
            return self.session("cursor-cli", extra=dict(extra))

        create_plan = build_send_plan(
            cursor_session(),
            "private",
            executable_resolver=lambda _name: str(self.executable),
        )
        wal.write_bytes(b"created")
        calls = []
        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                create_plan,
                allow_write=True,
                refresher=lambda: [cursor_session()],
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
        self.assertEqual("session_changed", raised.exception.code)

        remove_plan = build_send_plan(
            cursor_session(),
            "private",
            executable_resolver=lambda _name: str(self.executable),
        )
        wal.unlink()
        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                remove_plan,
                allow_write=True,
                refresher=lambda: [cursor_session()],
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
        self.assertEqual("session_changed", raised.exception.code)
        self.assertEqual([], calls)

    def test_refresh_resolves_exact_agent_and_full_session_id(self):
        plan = self.plan()
        calls = []
        cases = (
            ([], "session_unavailable"),
            (
                [
                    self.session(),
                    self.session(),
                ],
                "session_changed",
            ),
            (
                [
                    self.session(agent="codex"),
                    self.session(session_id="session-123-extra"),
                ],
                "session_unavailable",
            ),
        )
        for sessions, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(SendError) as raised:
                    self.execute_plan(
                        plan,
                        allow_write=True,
                        refresher=lambda sessions=sessions: sessions,
                        runner=lambda *args, **kwargs: calls.append((args, kwargs)),
                    )
                self.assertEqual(code, raised.exception.code)
        self.assertEqual([], calls)

    def test_refresh_rejects_changed_identity_and_ineligible_state(self):
        plan = self.plan()
        other_project = self.root / "other-project"
        other_project.mkdir()
        other_transcript = self.root / "other-transcript"
        other_transcript.write_text("{}\n", encoding="utf-8")
        cases = (
            (
                self.session(project=other_project),
                "session_changed",
            ),
            (
                Session(
                    agent="claude",
                    session_id="session-123",
                    project=str(self.root),
                    transcript=str(other_transcript),
                    updated_at=1.0,
                    status=Status.WAITING,
                    extra={},
                ),
                "session_changed",
            ),
            (
                self.session(extra={"source": "changed"}),
                "session_changed",
            ),
            (
                self.session(status=Status.WORKING),
                "working_session",
            ),
            (
                self.session(parent_id="parent"),
                "child_session",
            ),
            (
                self.session(extra={"remote": True}),
                "remote_session",
            ),
        )
        for refreshed, code in cases:
            with self.subTest(code=code):
                calls = []
                with self.assertRaises(SendError) as raised:
                    self.execute_plan(
                        plan,
                        allow_write=True,
                        refresher=lambda refreshed=refreshed: [refreshed],
                        runner=lambda *args, **kwargs: calls.append((args, kwargs)),
                    )
                self.assertEqual(code, raised.exception.code)
                self.assertEqual([], calls)

    def test_concurrent_resumes_start_exactly_one_runner(self):
        plan = self.plan(message="concurrency secret")
        started = threading.Event()
        release = threading.Event()
        outcomes = []
        runner_calls = []

        def runner(*args, **kwargs):
            runner_calls.append((args, kwargs))
            started.set()
            self.assertTrue(release.wait(2.0))
            return self.completed(plan)

        def first_send():
            try:
                outcomes.append(
                    self.execute_plan(
                        plan,
                        allow_write=True,
                        runner=runner,
                    ).outcome
                )
            except BaseException as error:
                outcomes.append(error)

        worker = threading.Thread(target=first_send)
        worker.start()
        self.assertTrue(started.wait(2.0))
        try:
            with self.assertRaises(SendError) as raised:
                self.execute_plan(
                    plan,
                    allow_write=True,
                    runner=runner,
                )
            self.assertEqual("session_busy", raised.exception.code)
        finally:
            release.set()
            worker.join(2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual(["completed"], outcomes)
        self.assertEqual(1, len(runner_calls))

    def test_lock_releases_after_all_runner_exit_paths(self):
        plan = self.plan()

        def timeout_runner(argv, input_data, **kwargs):
            del input_data, kwargs
            raise subprocess.TimeoutExpired(argv, 1)

        def interrupt_runner(*args, **kwargs):
            del args, kwargs
            raise KeyboardInterrupt

        cases = (
            lambda *args, **kwargs: self.completed(plan),
            lambda *args, **kwargs: self.completed(plan, returncode=8),
            timeout_runner,
            interrupt_runner,
        )
        for runner in cases:
            with self.subTest(runner=runner):
                try:
                    self.execute_plan(
                        plan,
                        allow_write=True,
                        runner=runner,
                    )
                except KeyboardInterrupt:
                    pass
                follow_up = self.execute_plan(
                    plan,
                    allow_write=True,
                    runner=lambda *args, **kwargs: self.completed(plan),
                )
                self.assertEqual("completed", follow_up.outcome)

    def test_lock_paths_are_private_hashed_and_reject_unsafe_entries(self):
        message = "LOCK-PATH-MESSAGE"
        session_id = "lock-path-session"
        plan = build_send_plan(
            self.session(session_id=session_id),
            message,
            executable_resolver=lambda _name: str(self.executable),
        )
        self.execute_plan(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: self.completed(plan),
        )
        lock_root = self.runtime / "send-locks"
        lock_files = list(lock_root.iterdir())

        self.assertEqual(0o700, stat_mode(self.runtime))
        self.assertEqual(0o700, stat_mode(lock_root))
        self.assertEqual(1, len(lock_files))
        self.assertEqual(0o600, stat_mode(lock_files[0]))
        self.assertNotIn(session_id, lock_files[0].name)
        self.assertNotIn(message, lock_files[0].name)

        lock_files[0].chmod(0o644)
        with self.assertRaises(SendError) as raised:
            self.execute_plan(plan, allow_write=True)
        self.assertEqual("unsafe_lock", raised.exception.code)

    def test_default_lock_root_uses_runtime_environment(self):
        plan = self.plan()
        configured = self.root / "configured-runtime"

        with mock.patch.dict(
            os.environ,
            {"AGENT_SIDECAR_RUNTIME_DIR": str(configured)},
        ):
            result = execute_send(
                plan,
                allow_write=True,
                refresher=lambda: [self.session()],
                executable_resolver=lambda _name: str(self.executable),
                runner=honoring_runner(
                    lambda *args, **kwargs: self.completed(plan)
                ),
            )

        self.assertEqual("completed", result.outcome)
        self.assertEqual(1, len(list((configured / "send-locks").iterdir())))

    def test_default_home_lock_root_remains_usable(self):
        plan = self.plan()
        home = self.root / "home"
        home.mkdir(mode=0o700, exist_ok=True)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_SIDECAR_RUNTIME_DIR", None)
            with mock.patch("sidecar.inject.Path.home", return_value=home):
                result = execute_send(
                    plan,
                    allow_write=True,
                    refresher=lambda: [self.session()],
                    executable_resolver=lambda _name: str(self.executable),
                    runner=honoring_runner(
                        lambda *args, **kwargs: self.completed(plan)
                    ),
                )

        self.assertEqual("completed", result.outcome)
        lock_root = home / ".agent_sidecar" / "send-locks"
        self.assertEqual(1, len(list(lock_root.iterdir())))

    def test_rejects_unsafe_lock_directories_and_symlink_file(self):
        plan = self.plan()

        unsafe_runtime = self.root / "unsafe-runtime"
        unsafe_runtime.mkdir(mode=0o700)
        unsafe_runtime.chmod(0o755)
        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                plan,
                allow_write=True,
                runtime_dir=unsafe_runtime,
            )
        self.assertEqual("unsafe_lock", raised.exception.code)

        symlink_runtime = self.root / "symlink-runtime"
        symlink_runtime.mkdir(mode=0o700)
        target_directory = self.root / "target-lock-directory"
        target_directory.mkdir(mode=0o700)
        (symlink_runtime / "send-locks").symlink_to(
            target_directory,
            target_is_directory=True,
        )
        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                plan,
                allow_write=True,
                runtime_dir=symlink_runtime,
            )
        self.assertEqual("unsafe_lock", raised.exception.code)

        file_runtime = self.root / "file-runtime"
        self.execute_plan(
            plan,
            allow_write=True,
            runtime_dir=file_runtime,
            runner=lambda *args, **kwargs: self.completed(plan),
        )
        lock_file = next((file_runtime / "send-locks").iterdir())
        lock_file.unlink()
        victim = self.root / "victim"
        victim.write_text("", encoding="utf-8")
        victim.chmod(0o600)
        lock_file.symlink_to(victim)
        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                plan,
                allow_write=True,
                runtime_dir=file_runtime,
            )
        self.assertEqual("unsafe_lock", raised.exception.code)

    def test_rejects_symlinked_runtime_ancestor(self):
        plan = self.plan()
        real_ancestor = self.root / "real-ancestor"
        real_ancestor.mkdir(mode=0o700)
        linked_ancestor = self.root / "linked-ancestor"
        linked_ancestor.symlink_to(real_ancestor, target_is_directory=True)
        calls = []

        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                plan,
                allow_write=True,
                runtime_dir=linked_ancestor / "runtime",
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )

        self.assertEqual("unsafe_lock", raised.exception.code)
        self.assertEqual([], calls)

    def test_rejects_foreign_owned_ancestor_and_allows_root_sticky(self):
        plan = self.plan()
        foreign_ancestor = self.root / "foreign-ancestor"
        foreign_ancestor.mkdir(mode=0o755)
        real_entry_stat = inject_module._entry_stat
        calls = []

        def foreign_entry_stat(parent_fd, name):
            details = real_entry_stat(parent_fd, name)
            if name != "foreign-ancestor":
                return details
            values = list(details)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)

        with mock.patch(
            "sidecar.inject._entry_stat",
            side_effect=foreign_entry_stat,
        ):
            with self.assertRaises(SendError) as raised:
                self.execute_plan(
                    plan,
                    allow_write=True,
                    runtime_dir=foreign_ancestor / "runtime",
                    runner=lambda *args, **kwargs: calls.append((args, kwargs)),
                )

        self.assertEqual("unsafe_lock", raised.exception.code)
        self.assertEqual([], calls)

        values = list(os.stat(foreign_ancestor))
        values[0] = stat.S_IFDIR | stat.S_ISVTX | 0o777
        values[4] = 0
        inject_module._validate_directory(
            os.stat_result(values),
            private=False,
        )

    def test_mkdir_eexist_race_reopens_once_and_validates(self):
        plan = self.plan()
        parent = self.root / "mkdir-race-parent"
        parent.mkdir(mode=0o700)
        runtime = parent / "race-runtime"
        real_mkdir = os.mkdir
        target_mkdir_calls = []
        runner_calls = []

        def racing_mkdir(name, mode=0o777, *, dir_fd=None):
            if name == "race-runtime":
                target_mkdir_calls.append((name, mode, dir_fd))
                real_mkdir(name, mode, dir_fd=dir_fd)
                raise FileExistsError("competing creator")
            return real_mkdir(name, mode, dir_fd=dir_fd)

        with mock.patch(
            "sidecar.inject._require_anchored_lock_support",
        ), mock.patch("sidecar.inject.os.mkdir", side_effect=racing_mkdir):
            result = self.execute_plan(
                plan,
                allow_write=True,
                runtime_dir=runtime,
                runner=lambda *args, **kwargs: (
                    runner_calls.append((args, kwargs))
                    or self.completed(plan)
                ),
            )

        self.assertEqual("completed", result.outcome)
        self.assertEqual(1, len(target_mkdir_calls))
        self.assertEqual(1, len(runner_calls))
        self.assertEqual(0o700, stat_mode(runtime))

    def test_detects_runtime_ancestor_and_lock_directory_swaps(self):
        plan = self.plan()
        calls = []

        ancestor = self.root / "swap-ancestor"
        ancestor.mkdir(mode=0o700)
        nested = ancestor / "nested"
        nested.mkdir(mode=0o700)
        runtime = nested / "runtime"

        def swap_ancestor():
            moved = ancestor / "moved-nested"
            nested.rename(moved)
            nested.symlink_to(moved, target_is_directory=True)
            return [self.session()]

        result = self.execute_plan(
            plan,
            allow_write=True,
            runtime_dir=runtime,
            refresher=swap_ancestor,
            runner=lambda *args, **kwargs: (
                calls.append((args, kwargs)) or self.completed(plan)
            ),
        )
        self.assertEqual("completed", result.outcome)
        self.assertEqual(1, len(calls))

        lock_runtime = self.root / "lock-directory-swap"

        def swap_lock_directory():
            lock_directory = lock_runtime / "send-locks"
            lock_directory.rename(lock_runtime / "old-send-locks")
            lock_directory.mkdir(mode=0o700)
            return [self.session()]

        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                plan,
                allow_write=True,
                runtime_dir=lock_runtime,
                refresher=swap_lock_directory,
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
        self.assertEqual("unsafe_lock", raised.exception.code)
        self.assertEqual(1, len(calls))

    def test_request_id_replays_success_without_response_or_second_spawn(self):
        plan = self.plan(message="REPLAY-SECRET")
        calls = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return self.completed(
                plan,
                stdout=b'{"result":"stored response"}',
            )

        first = self.execute_plan(
            plan,
            allow_write=True,
            request_id="request-success",
            runner=runner,
        )
        replay = self.execute_plan(
            plan,
            allow_write=True,
            request_id="request-success",
            runner=lambda *args, **kwargs: self.fail("must not spawn"),
        )

        self.assertEqual("completed", first.outcome)
        self.assertFalse(first.replayed)
        self.assertEqual("request-success", first.request_id)
        self.assertEqual("completed", replay.outcome)
        self.assertTrue(replay.replayed)
        self.assertEqual("", replay.response)
        self.assertEqual("", replay.stderr)
        self.assertEqual(1, len(calls))

    def test_replay_identity_ignores_transcript_executable_and_wal_changes(self):
        message = "stable replay message"
        plan = self.plan(message=message)
        calls = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return self.completed(plan)

        first = self.execute_plan(
            plan,
            allow_write=True,
            request_id="request-stable-source",
            runner=runner,
        )
        self.transcript.write_text('{"mutated":true}\n', encoding="utf-8")
        transcript_plan = self.plan(message=message)
        transcript_replay = self.execute_plan(
            transcript_plan,
            allow_write=True,
            request_id="request-stable-source",
            runner=lambda *args, **kwargs: self.fail("must not spawn"),
        )
        self.executable.write_text(
            "#!/bin/sh\nprintf changed\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o700)
        executable_plan = self.plan(message=message)
        executable_replay = self.execute_plan(
            executable_plan,
            allow_write=True,
            request_id="request-stable-source",
            runner=lambda *args, **kwargs: self.fail("must not spawn"),
        )

        self.assertEqual("completed", first.outcome)
        self.assertTrue(transcript_replay.replayed)
        self.assertTrue(executable_replay.replayed)
        self.assertEqual(1, len(calls))

        wal = Path(str(self.transcript) + "-wal")
        extra = {
            "source": "cli",
            "store": str(self.transcript),
            "wal": str(wal),
            "transcript_kind": "cursor-chat-sqlite",
            "directory_session_id": "session-123",
        }

        def cursor_session():
            return self.session("cursor-cli", extra=dict(extra))

        cursor_plan = build_send_plan(
            cursor_session(),
            message,
            executable_resolver=lambda _name: str(self.executable),
        )
        cursor_calls = []
        self.execute_plan(
            cursor_plan,
            allow_write=True,
            request_id="request-stable-wal",
            refresher=lambda: [cursor_session()],
            runner=lambda *args, **kwargs: (
                cursor_calls.append((args, kwargs))
                or self.completed(cursor_plan)
            ),
        )
        wal.write_bytes(b"changed")
        changed_wal_plan = build_send_plan(
            cursor_session(),
            message,
            executable_resolver=lambda _name: str(self.executable),
        )
        wal_replay = self.execute_plan(
            changed_wal_plan,
            allow_write=True,
            request_id="request-stable-wal",
            refresher=lambda: [cursor_session()],
            runner=lambda *args, **kwargs: self.fail("must not spawn"),
        )
        self.assertTrue(wal_replay.replayed)
        self.assertEqual(1, len(cursor_calls))

    def test_same_request_id_with_changed_message_conflicts(self):
        first = self.plan(message="first message")
        changed = self.plan(message="changed message")
        self.execute_plan(
            first,
            allow_write=True,
            request_id="request-changed-message",
            runner=lambda *args, **kwargs: self.completed(first),
        )
        calls = []
        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                changed,
                allow_write=True,
                request_id="request-changed-message",
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
        self.assertEqual("request_conflict", raised.exception.code)
        self.assertEqual([], calls)

    def test_request_id_replays_every_non_success_outcome(self):
        cases = (
            (
                "failed",
                lambda plan: self.completed(plan, returncode=9),
                "native_exit",
            ),
            (
                "timed_out",
                lambda plan: (_ for _ in ()).throw(
                    subprocess.TimeoutExpired(plan.argv, 1)
                ),
                "timeout",
            ),
            (
                "overflow",
                lambda plan: self.completed(plan, overflow="stdout"),
                "stdout_overflow",
            ),
        )
        for outcome, completed, error_code in cases:
            with self.subTest(outcome=outcome):
                plan = self.plan()
                request_id = "request-" + outcome
                first = self.execute_plan(
                    plan,
                    allow_write=True,
                    request_id=request_id,
                    runner=lambda *args, completed=completed, **kwargs: completed(
                        plan
                    ),
                )
                replay = self.execute_plan(
                    plan,
                    allow_write=True,
                    request_id=request_id,
                    runner=lambda *args, **kwargs: self.fail("must not spawn"),
                )

                self.assertEqual(outcome, first.outcome)
                self.assertEqual(outcome, replay.outcome)
                self.assertEqual(error_code, replay.error_code)
                self.assertTrue(replay.replayed)
                self.assertEqual("", replay.response)
                self.assertEqual("", replay.stderr)

    def test_same_request_id_for_different_target_is_preflight_conflict(self):
        first = self.plan(message="private")
        other = build_send_plan(
            self.session(session_id="different-session"),
            "private",
            executable_resolver=lambda _name: str(self.executable),
        )
        self.execute_plan(
            first,
            allow_write=True,
            request_id="request-conflict",
            runner=lambda *args, **kwargs: self.completed(first),
        )
        calls = []

        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                other,
                allow_write=True,
                request_id="request-conflict",
                refresher=lambda: [
                    self.session(session_id="different-session")
                ],
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )

        self.assertEqual("request_conflict", raised.exception.code)
        self.assertEqual([], calls)

    def test_same_session_in_different_project_is_request_conflict(self):
        first = self.plan(message="private")
        other_project = self.root / "other-request-project"
        other_project.mkdir()
        other = build_send_plan(
            self.session(project=other_project),
            "private",
            executable_resolver=lambda _name: str(self.executable),
        )
        self.execute_plan(
            first,
            allow_write=True,
            request_id="request-project-conflict",
            runner=lambda *args, **kwargs: self.completed(first),
        )
        calls = []
        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                other,
                allow_write=True,
                request_id="request-project-conflict",
                refresher=lambda: [self.session(project=other_project)],
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
        self.assertEqual("request_conflict", raised.exception.code)
        self.assertEqual([], calls)

    def test_pending_request_replays_unknown_without_spawn(self):
        plan = self.plan()
        identity = inject_module._send_audit_identity(
            plan,
            "private prompt",
        )
        store = inject_module.SendAuditStore(self.runtime)
        self.assertIsNone(store.reserve("request-pending", identity))
        calls = []

        replay = self.execute_plan(
            plan,
            allow_write=True,
            request_id="request-pending",
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

        self.assertEqual("request_pending", replay.outcome)
        self.assertEqual("unknown", replay.delivery)
        self.assertEqual("request_pending", replay.error_code)
        self.assertTrue(replay.replayed)
        self.assertEqual([], calls)

    def test_same_request_id_concurrently_reserves_and_spawns_once(self):
        plan = self.plan()
        started = threading.Event()
        release = threading.Event()
        results = []
        calls = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            started.set()
            self.assertTrue(release.wait(2.0))
            return self.completed(plan)

        def execute():
            try:
                results.append(
                    self.execute_plan(
                        plan,
                        allow_write=True,
                        request_id="request-thread",
                        runner=runner,
                    )
                )
            except BaseException as error:
                results.append(error)

        worker = threading.Thread(target=execute)
        worker.start()
        self.assertTrue(started.wait(2.0))
        replay = self.execute_plan(
            plan,
            allow_write=True,
            request_id="request-thread",
            runner=lambda *args, **kwargs: self.fail("must not spawn"),
        )
        release.set()
        worker.join(2.0)

        self.assertFalse(worker.is_alive())
        self.assertEqual("request_pending", replay.outcome)
        self.assertTrue(replay.replayed)
        self.assertEqual(1, len(calls))
        self.assertEqual("completed", results[0].outcome)

    def test_auto_request_id_and_terminal_audit_failure_semantics(self):
        plan = self.plan()
        automatic = self.execute_plan(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: self.completed(plan),
        )
        self.assertTrue(automatic.request_id)
        self.assertFalse(automatic.replayed)

        with mock.patch(
            "sidecar.inject.AuditNamespace.append_terminal",
            side_effect=OSError("audit failed"),
        ):
            failed = self.execute_plan(
                plan,
                allow_write=True,
                request_id="request-audit-failure",
                runner=lambda *args, **kwargs: self.completed(plan),
            )
        self.assertEqual("audit_error", failed.outcome)
        self.assertEqual("unknown", failed.delivery)
        self.assertEqual("audit_error", failed.error_code)

    def test_key_mutation_after_spawn_makes_delivery_unknown(self):
        plan = self.plan()

        def runner(*args, **kwargs):
            key_path = self.runtime / "audit.key"
            key_path.write_bytes(b"z" * 32)
            key_path.chmod(0o600)
            return self.completed(plan)

        result = self.execute_plan(
            plan,
            allow_write=True,
            request_id="request-key-post-spawn",
            runner=runner,
        )
        self.assertEqual("audit_error", result.outcome)
        self.assertEqual("unknown", result.delivery)
        self.assertEqual("audit_error", result.error_code)

    def test_send_lock_parent_fsync_failure_prevents_spawn(self):
        plan = self.plan()
        runtime = self.root / "send-lock-parent-fsync"
        send_locks = runtime / "send-locks"
        calls = []
        real_fsync = os.fsync

        def fail_after_send_lock_creation(descriptor):
            details = os.fstat(descriptor)
            if send_locks.exists():
                runtime_details = os.stat(runtime)
                if (details.st_dev, details.st_ino) == (
                    runtime_details.st_dev,
                    runtime_details.st_ino,
                ):
                    raise OSError("send lock parent fsync failed")
            return real_fsync(descriptor)

        with mock.patch(
            "sidecar.inject.os.fsync",
            side_effect=fail_after_send_lock_creation,
        ):
            with self.assertRaises(SendError) as raised:
                self.execute_plan(
                    plan,
                    allow_write=True,
                    runtime_dir=runtime,
                    request_id="request-send-lock-fsync",
                    runner=lambda *args, **kwargs: calls.append(
                        (args, kwargs)
                    ),
                )
        self.assertEqual("unsafe_lock", raised.exception.code)
        self.assertEqual([], calls)

    def test_audit_never_contains_message_response_or_native_stderr(self):
        message = "AUDIT-MESSAGE-SECRET"
        plan = self.plan(message=message)
        response = "AUDIT-RESPONSE-SECRET"
        native_error = "AUDIT-STDERR-SECRET"

        self.execute_plan(
            plan,
            allow_write=True,
            request_id="request-hygiene",
            runner=lambda *args, **kwargs: self.completed(
                plan,
                stdout=json.dumps({"result": response}).encode("utf-8"),
                stderr=native_error.encode("utf-8"),
            ),
        )
        payload = (self.runtime / "audit.jsonl").read_bytes()

        for sensitive in (message, response, native_error):
            self.assertNotIn(sensitive.encode("utf-8"), payload)
        self.assertNotIn(
            hashlib.sha256(message.encode("utf-8")).hexdigest().encode("ascii"),
            payload,
        )


class KimiSendIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.kimi_home = self.home / ".kimi-code"
        self.project = self.root / "project"
        self.session_id = "kimi_native_53"
        self.session_dir = (
            self.kimi_home / "sessions" / "workspace-1" / self.session_id
        )
        self.main_dir = self.session_dir / "agents" / "main"
        for directory in (
            self.home,
            self.kimi_home,
            self.project,
            self.main_dir,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        self.index_path = self.kimi_home / "session_index.jsonl"
        self.workspace_path = self.kimi_home / "workspaces.json"
        self.state_path = self.session_dir / "state.json"
        self.wire_path = self.main_dir / "wire.jsonl"
        self.index_row = {
            "sessionId": self.session_id,
            "sessionDir": str(self.session_dir),
            "workDir": str(self.project),
        }
        self.workspace = {
            "workspaces": {"workspace-1": {"root": str(self.project)}}
        }
        self.state = {
            "id": self.session_id,
            "cwd": str(self.project),
            "agents": {"main": {"homedir": str(self.main_dir)}},
            "lastTurnReason": "completed",
            "updatedAt": 1700000000000,
        }
        self._write_fixture()
        self.executable = self.root / "kimi"
        self.executable.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then\n'
            "  printf '0.38.0\\n'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o700)
        self.runtime = self.root / "runtime"
        environment = mock.patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "KIMI_CODE_HOME": str(self.kimi_home),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        account_home = mock.patch(
            "sidecar.send_audit.pwd.getpwuid",
            return_value=mock.Mock(pw_dir=str(self.home)),
        )
        account_home.start()
        self.addCleanup(account_home.stop)

    def _write_json(self, path, value):
        path.write_text(
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _write_jsonl(self, path, values):
        path.write_text(
            "".join(
                json.dumps(
                    value,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
                for value in values
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _write_fixture(self):
        self._write_jsonl(self.index_path, [self.index_row])
        self._write_json(self.workspace_path, self.workspace)
        self._write_json(self.state_path, self.state)
        self._write_jsonl(
            self.wire_path,
            [
                {
                    "type": "turn.ended",
                    "agentId": "main",
                    "turnId": 1,
                    "reason": "completed",
                    "time": 1700000000000,
                }
            ],
        )

    def session(self, **changes):
        values = {
            "agent": "kimi",
            "session_id": self.session_id,
            "project": str(self.project),
            "transcript": str(self.wire_path),
            "updated_at": 1700000000.0,
            "status": Status.WAITING,
            "extra": {
                "source": "session_index",
                "agent_id": "main",
            },
        }
        values.update(changes)
        return Session(**values)

    def plan(self, message="KIMI-PRIVATE-PROMPT", **kwargs):
        return build_send_plan(
            self.session(**kwargs),
            message,
            executable_resolver=lambda name: (
                str(self.executable) if name == "kimi" else None
            ),
        )

    def _next_turn_id(self):
        records = [
            json.loads(line)
            for line in self.wire_path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        turn_ids = [
            record["turnId"]
            for record in records
            if record.get("type") == "turn.ended"
            and type(record.get("turnId")) is int
        ]
        return max(turn_ids, default=0) + 1

    def _append_wire(self, records):
        with self.wire_path.open("ab") as stream:
            for record in records:
                stream.write(
                    json.dumps(
                        record,
                        ensure_ascii=True,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                    + b"\n"
                )
            stream.flush()
            os.fsync(stream.fileno())

    def _acp_result(
        self,
        *,
        phase=AcpPhase.PROMPT_SETTLED,
        prompt_write=PromptWriteBoundary.COMPLETE,
        stop_reason="end_turn",
        returncode=0,
        clean_exit=True,
        cleanup_complete=True,
        error_code=None,
        definitive_busy_rejection=False,
        response_text="synthetic response",
    ):
        return KimiAcpResult(
            phase=phase,
            prompt_write=prompt_write,
            stop_reason=stop_reason,
            response_text=response_text,
            response_digest=hashlib.sha256(
                response_text.encode("utf-8")
            ).hexdigest(),
            returncode=returncode,
            clean_exit=clean_exit,
            cleanup_complete=cleanup_complete,
            error_code=error_code,
            own_turn_started=prompt_write is not PromptWriteBoundary.NONE,
            definitive_busy_rejection=definitive_busy_rejection,
            stdout_bytes=0,
            stderr_bytes=0,
        )

    def acp_runner(
        self,
        *,
        reason="completed",
        state_reason=None,
        duplicate_prompt=False,
        omit_end=False,
        replace_wire=False,
        update_state=True,
        result=None,
        calls=None,
    ):
        def run(request, **kwargs):
            if calls is not None:
                calls.append(request)
            kwargs["before_prompt"](
                AcpProcessIdentity(pid=4321, process_group_id=4321)
            )
            turn_id = self._next_turn_id()
            prompt = {
                "type": "turn.prompt",
                "agentId": "main",
                "input": [
                    {
                        "type": "text",
                        "text": request.message.decode("utf-8"),
                    }
                ],
                "origin": {"type": "acp"},
                "time": 1700000001000,
            }
            records = [prompt]
            if duplicate_prompt:
                records.append(dict(prompt))
            if not omit_end:
                records.append(
                    {
                        "type": "turn.ended",
                        "agentId": "main",
                        "turnId": turn_id,
                        "reason": reason,
                        "time": 1700000002000,
                    }
                )
            if replace_wire:
                moved = self.main_dir / "wire.before"
                self.wire_path.rename(moved)
                self._write_jsonl(self.wire_path, records)
            else:
                self._append_wire(records)
            if update_state:
                updated = dict(self.state)
                updated["lastTurnReason"] = (
                    state_reason
                    if state_reason is not None
                    else "failed"
                    if reason == "blocked"
                    else reason
                )
                self._write_json(self.state_path, updated)
            return self._acp_result() if result is None else result

        return run

    def execute(self, plan, kimi_runner, **kwargs):
        kwargs.setdefault("request_id", "request-kimi")
        kwargs.setdefault("process_guard", lambda *args, **options: None)
        return execute_send(
            plan,
            allow_write=True,
            refresher=lambda: [
                self.session(session_id=plan.session_id)
            ],
            executable_resolver=lambda name: (
                str(self.executable) if name == "kimi" else None
            ),
            runtime_dir=self.runtime,
            kimi_runner=kimi_runner,
            **kwargs,
        )

    def test_kimi_plan_is_exact_repr_hidden_ndjson_transport(self):
        message = "KIMI-PLAN-SECRET"
        plan = self.plan(message)

        self.assertEqual("kimi_acp", plan.transport)
        self.assertEqual("ndjson", plan.prompt_transport)
        self.assertEqual((str(self.executable), "acp"), plan.argv)
        self.assertEqual(message.encode("utf-8"), plan.input_data)
        self.assertNotIn(message, repr(plan))
        self.assertNotIn(message, " ".join(plan.argv))
        self.assertEqual("main", plan.target.kimi_identity.agent_id)

    def test_kimi_wire_proof_helpers_fail_closed(self):
        with self.assertRaisesRegex(SendError, "session changed"):
            inject_module._read_kimi_file(-1, 1, 1)
        closed_descriptor = os.open(self.wire_path, os.O_RDONLY)
        os.close(closed_descriptor)
        with self.assertRaises(SendError):
            inject_module._read_kimi_file(closed_descriptor, 1, 1)
        for payload in (b"{}", b"\n", b"{\n", b"1\n"):
            with self.subTest(payload=payload), self.assertRaises(SendError):
                inject_module._parse_kimi_jsonl(payload)

        self.assertEqual(
            -1,
            inject_module._previous_main_turn_id(
                [{"type": "turn.ended", "agentId": "child", "turnId": 1}]
            ),
        )
        with self.assertRaises(SendError):
            inject_module._previous_main_turn_id(
                [{"type": "turn.ended", "agentId": "main", "turnId": "1"}]
            )
        self.assertFalse(
            inject_module._kimi_prompt_matches(
                {"type": "turn.ended", "agentId": "main"},
                "private",
            )
        )
        self.assertFalse(
            inject_module._kimi_prompt_matches(
                {"type": "turn.prompt", "agentId": "main", "input": []},
                "private",
            )
        )

        prompt = {
            "type": "turn.prompt",
            "agentId": "main",
            "input": [{"type": "text", "text": "private"}],
        }
        completed = {
            "type": "turn.ended",
            "agentId": "main",
            "turnId": 1,
            "reason": "completed",
        }
        self.assertFalse(
            inject_module._kimi_durable_completed([prompt, completed], "other", 0)
        )
        self.assertTrue(
            inject_module._kimi_durable_completed(
                [prompt, {"agentId": "child"}, completed],
                "private",
                0,
            )
        )
        self.assertFalse(
            inject_module._kimi_durable_completed(
                [prompt, {"type": "turn.steer", "agentId": "main"}, completed],
                "private",
                0,
            )
        )
        self.assertFalse(
            inject_module._kimi_durable_completed([prompt], "private", 0)
        )

        evidence = inject_module.capture_kimi_identity(self.session())
        evidence.close()
        self.assertIsNone(inject_module._kimi_state_reason(evidence))

    def test_kimi_file_proof_bounds_and_state_reason_fail_closed(self):
        evidence = inject_module.capture_kimi_identity(self.session())
        try:
            descriptor = evidence._anchors.descriptor("wire")
            wire_size = evidence.root_wire_generation.size
            self.assertEqual(
                self.wire_path.read_bytes(),
                inject_module._read_kimi_file(
                    descriptor,
                    wire_size,
                    inject_module.KIMI_PROOF_BYTES,
                ),
            )
            for args in (
                (descriptor, -1, inject_module.KIMI_PROOF_BYTES),
                (descriptor, inject_module.KIMI_PROOF_BYTES + 1, inject_module.KIMI_PROOF_BYTES),
                (True, 1, inject_module.KIMI_PROOF_BYTES),
            ):
                with self.subTest(args=args):
                    with self.assertRaises(SendError):
                        inject_module._read_kimi_file(*args)
            with mock.patch.object(inject_module.os, "pread", return_value=b""):
                with self.assertRaises(SendError):
                    inject_module._read_kimi_file(descriptor, 1, 10)
            self.assertEqual("completed", inject_module._kimi_state_reason(evidence))
            self._write_json(self.state_path, [])
            self.assertIsNone(inject_module._kimi_state_reason(evidence))
            invalid_reason = dict(self.state, lastTurnReason=42)
            self._write_json(self.state_path, invalid_reason)
            self.assertIsNone(inject_module._kimi_state_reason(evidence))
            failed_state = dict(self.state, lastTurnReason="failed")
            self._write_json(self.state_path, failed_state)
            refreshed = inject_module.capture_kimi_identity(self.session())
            try:
                self.assertEqual("failed", inject_module._kimi_state_reason(refreshed))
            finally:
                refreshed.close()
        finally:
            evidence.close()

    def test_kimi_version_is_exact_and_rechecked_before_audit(self):
        calls = []

        def version_runner(argv, input_data, **kwargs):
            calls.append((tuple(argv), input_data, kwargs))
            return BoundedProcessResult(
                args=argv,
                returncode=0,
                stdout=(KIMI_SUPPORTED_VERSION + "\n").encode("ascii"),
                stderr=b"",
            )

        plan = build_send_plan(
            self.session(),
            "private",
            executable_resolver=lambda _name: str(self.executable),
            version_runner=version_runner,
        )
        result = self.execute(
            plan,
            self.acp_runner(),
            version_runner=version_runner,
        )

        self.assertEqual(("completed", "unknown"), (result.outcome, result.delivery))
        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual((str(self.executable), "--version"), calls[0][0])
        self.assertTrue(all(call[0][1] == "--version" for call in calls))
        self.assertTrue(any(call[0][0] != str(self.executable) for call in calls))
        self.assertTrue(all(call[1] is None for call in calls))

        stale = self.plan("version drift")

        def wrong_version(argv, input_data, **kwargs):
            del input_data, kwargs
            return BoundedProcessResult(
                args=argv,
                returncode=0,
                stdout=b"0.39.0\n",
                stderr=b"",
            )

        no_spawn = []
        with self.assertRaises(SendError) as raised:
            self.execute(
                stale,
                lambda *args, **kwargs: no_spawn.append((args, kwargs)),
                request_id="request-version-drift",
                version_runner=wrong_version,
            )
        self.assertEqual("unsupported_kimi", raised.exception.code)
        self.assertEqual([], no_spawn)
        self.assertNotIn(
            b"request-version-drift",
            (self.runtime / "audit.jsonl").read_bytes(),
        )

    def test_kimi_bound_snapshot_survives_spawn_path_swap_and_cleans_up(self):
        message = "bound snapshot private"
        plan = self.plan(message)
        original = self.executable.read_bytes()
        captured_paths = []
        guard_calls = []

        def guard(project, executable, *, excluded_process_group=None):
            del project, executable
            guard_calls.append(excluded_process_group)
            if excluded_process_group is None:
                return
            backup = self.root / "kimi.verified"
            replacement = self.root / "kimi.replacement"
            replacement.write_text(
                "#!/bin/sh\nprintf '0.38.0\\n'\n# malicious replacement\n",
                encoding="utf-8",
            )
            replacement.chmod(0o700)
            self.executable.rename(backup)
            replacement.rename(self.executable)
            self.executable.unlink()
            backup.rename(self.executable)

        native_runner = self.acp_runner()

        def inspect_bound(request, **kwargs):
            captured = Path(request.executable)
            captured_paths.append(captured)
            self.assertNotEqual(self.executable, captured)
            self.assertEqual(0o500, captured.stat().st_mode & 0o777)
            result = native_runner(request, **kwargs)
            self.assertEqual(original, self.executable.read_bytes())
            self.assertNotIn(b"malicious replacement", captured.read_bytes())
            return result

        result = self.execute(
            plan,
            inspect_bound,
            request_id="request-bound-snapshot",
            process_guard=guard,
        )
        self.assertEqual(("completed", "unknown"), (result.outcome, result.delivery))
        self.assertEqual([None, 4321], guard_calls)
        self.assertEqual(1, len(captured_paths))
        self.assertFalse(captured_paths[0].exists())

    def test_kimi_private_node_snapshot_versions_and_initializes_acp(self):
        candidates = (shutil.which("node"), "/usr/bin/node", "/usr/local/bin/node")
        seen = set()
        real_node = None
        for value in candidates:
            if not value:
                continue
            try:
                candidate = Path(value).resolve(strict=True)
                details = candidate.stat()
            except (OSError, RuntimeError):
                continue
            identity = (int(details.st_dev), int(details.st_ino))
            if identity in seen:
                continue
            seen.add(identity)
            if (
                stat.S_ISREG(details.st_mode)
                and os.access(str(candidate), os.X_OK)
                and details.st_size <= inject_module.KIMI_RUNTIME_FILE_BYTES
            ):
                real_node = candidate
                break
        if real_node is None:
            self.skipTest("No bounded executable Node candidate is available")
        private_node_directory = tempfile.TemporaryDirectory(
            prefix="private-node-",
            dir=str(self.root),
        )
        private_node = Path(private_node_directory.name) / "node"
        copy_private_executable(real_node, private_node)
        package_root = self.root / "kimi-package"
        dist = package_root / "dist"
        dist.mkdir(parents=True, mode=0o700)
        package = package_root / "package.json"
        package.write_text(
            '{"name":"synthetic-kimi","version":"0.38.0","type":"module"}\n',
            encoding="utf-8",
        )
        package.chmod(0o600)
        worker = dist / "search-worker.mjs"
        worker.write_text("export {};\n", encoding="utf-8")
        worker.chmod(0o600)
        main = dist / "main.mjs"
        main.write_text(
            "#!/usr/bin/env node\n"
            "import readline from 'node:readline';\n"
            "if (process.argv[2] === '--version') {\n"
            "  console.log('0.38.0');\n"
            "} else {\n"
            "  const lines = readline.createInterface({input: process.stdin});\n"
            "  lines.once('line', (line) => {\n"
            "    const request = JSON.parse(line);\n"
            "    console.log(JSON.stringify({jsonrpc:'2.0',id:request.id,result:{\n"
            "      protocolVersion:1,agentCapabilities:{loadSession:true,\n"
            "      sessionCapabilities:{list:{},resume:{}}}}}));\n"
            "  });\n"
            "}\n",
            encoding="utf-8",
        )
        main.chmod(0o700)
        bound = None
        bound_path = None
        process = None
        cleanup_complete = None
        try:
            identity = inject_module._executable_identity(str(main))
            capture_runtime = inject_module._capture_macho_closure

            def capture_private_runtime(node):
                if sys.platform != "darwin":
                    return capture_runtime(node)
                self.assertEqual(str(private_node), node)
                _node_asset, dylibs, systems = capture_runtime(str(real_node))
                return inject_module._runtime_asset(node, "bin/node"), dylibs, systems

            with mock.patch.object(
                inject_module,
                "_capture_macho_closure",
                side_effect=capture_private_runtime,
            ), mock.patch.dict(
                os.environ,
                {"PATH": private_node_directory.name},
                clear=False,
            ):
                manifest = inject_module._capture_kimi_runtime_manifest(
                    str(main),
                    identity,
                )
            self.assertEqual(str(private_node), manifest.node.identity[0])
            bound = inject_module._bind_kimi_executable(manifest)
            bound_path = Path(bound.executable)
            with mock.patch.dict(
                os.environ,
                {
                    "NODE_OPTIONS": "--require=/definitely/missing.js",
                    "NODE_PATH": "/definitely/missing",
                    "DYLD_INSERT_LIBRARIES": "/definitely/missing.dylib",
                    "DYLD_FRAMEWORK_PATH": "/definitely/missing-frameworks",
                    "DYLD_FALLBACK_FRAMEWORK_PATH": "/definitely/fallback-frameworks",
                    "DYLD_FALLBACK_LIBRARY_PATH": "/definitely/fallback-libraries",
                    "DYLD_LIBRARY_PATH": "/definitely/missing-libraries",
                },
                clear=False,
            ):
                inject_module._probe_bound_kimi_version(bound, run_bounded)
                inject_module._probe_bound_kimi_initialize(bound)
            self.assertEqual(0o500, bound_path.stat().st_mode & 0o777)
            with mock.patch.dict(
                os.environ,
                {
                    "NODE_OPTIONS": "--require=/definitely/missing.js",
                    "DYLD_INSERT_LIBRARIES": "/definitely/missing.dylib",
                },
                clear=False,
            ):
                process = BoundedDuplexLineProcess(
                    (bound.executable, "acp"),
                    line_limit=256 * 1024,
                    stdout_limit=1024 * 1024,
                    stderr_limit=64 * 1024,
                    env=build_kimi_child_env(),
                    require_descendant_containment=False,
                )
            deadline = time.monotonic() + 30
            process.write_line(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": 1,
                            "clientCapabilities": {},
                            "clientInfo": {
                                "name": "agent-sidecar",
                                "version": "test",
                            },
                        },
                    },
                    separators=(",", ":"),
                ).encode("utf-8"),
                deadline=deadline,
            )
            response = json.loads(process.read_line(deadline=deadline))
            self.assertEqual(1, response["id"])
            self.assertTrue(
                response["result"]["agentCapabilities"]["loadSession"]
            )
        finally:
            try:
                if process is not None:
                    observed = process.terminate_tree(deadline=time.monotonic() + 5)
                    process.close()
                    cleanup_complete = observed.cleanup_complete
            finally:
                try:
                    if bound is not None:
                        bound.close()
                finally:
                    private_node_directory.cleanup()
        if cleanup_complete is not None:
            self.assertTrue(cleanup_complete)
        self.assertFalse(private_node.exists())
        self.assertIsNotNone(bound_path)
        self.assertFalse(bound_path.exists())

    def test_kimi_snapshot_failures_are_pre_audit_and_sanitized(self):
        unsafe = self.root / "unsafe-image"
        unsafe.write_bytes(b"unsafe")
        unsafe.chmod(0o722)
        with self.assertRaises(SendError):
            inject_module._owner_safe_file_identity(str(unsafe))
        with self.assertRaises(SendError):
            inject_module._owner_safe_file_identity(str(self.root / "missing"))

        identity = inject_module._executable_identity(str(self.executable))
        with self.assertRaises(SendError):
            inject_module._snapshot_verified_file(
                str(self.executable),
                self.root / "wrong-source",
                expected=(str(unsafe), *identity[1:]),
                limit=1024,
                mode=0o500,
            )
        with self.assertRaises(SendError):
            inject_module._snapshot_verified_file(
                str(self.executable),
                self.root / "too-large",
                expected=identity,
                limit=1,
                mode=0o500,
            )
        with mock.patch.object(inject_module.os, "O_NOFOLLOW", 0):
            with self.assertRaises(SendError):
                inject_module._snapshot_verified_file(
                    str(self.executable),
                    self.root / "no-nofollow",
                    expected=identity,
                    limit=1024,
                    mode=0o500,
                )
        copied = self.root / "copied-image"
        inject_module._snapshot_verified_file(
            str(self.executable),
            copied,
            expected=None,
            limit=1024,
            mode=0o500,
        )
        self.assertTrue(copied.is_file())

        wrong_node = self.root / "wrong-main.mjs"
        wrong_node.write_text(
            "#!/usr/bin/env node\nconsole.log('0.38.0');\n",
            encoding="utf-8",
        )
        wrong_node.chmod(0o700)
        with self.assertRaises(SendError):
            inject_module._capture_kimi_runtime_manifest(
                str(wrong_node),
                inject_module._executable_identity(str(wrong_node)),
            )

        missing_assets = self.root / "missing-assets" / "dist" / "main.mjs"
        missing_assets.parent.mkdir(parents=True, mode=0o700)
        missing_assets.write_text(
            "#!/usr/bin/env node\nconsole.log('0.38.0');\n",
            encoding="utf-8",
        )
        missing_assets.chmod(0o700)
        with self.assertRaises(SendError):
            inject_module._capture_kimi_runtime_manifest(
                str(missing_assets),
                inject_module._executable_identity(str(missing_assets)),
            )
        (missing_assets.parent.parent / "package.json").write_text(
            '{"version":"0.38.0","type":"module"}\n',
            encoding="utf-8",
        )
        (missing_assets.parent / "search-worker.mjs").write_text(
            "export {};\n",
            encoding="utf-8",
        )
        with mock.patch("sidecar.inject.shutil.which", return_value=None):
            with self.assertRaises(SendError):
                inject_module._capture_kimi_runtime_manifest(
                    str(missing_assets),
                    inject_module._executable_identity(str(missing_assets)),
                )

        bound = inject_module._bind_kimi_executable(
            inject_module._capture_kimi_runtime_manifest(
                str(self.executable),
                identity,
            )
        )
        try:
            with self.assertRaises(SendError):
                inject_module._probe_bound_kimi_version(
                    bound,
                    mock.Mock(side_effect=RuntimeError("synthetic")),
                )
        finally:
            bound.close()

    def test_kimi_transitive_runtime_replacements_are_rejected(self):
        names = (
            "package.json",
            "dist/search-worker.mjs",
            "bin/node",
            "lib/libnode.dylib",
            "lib/libuv.dylib",
            "lib/libssl.dylib",
            "lib/libcrypto.dylib",
            "lib/libicui18n.dylib",
            "lib/libicuuc.dylib",
        )
        for index, replaced in enumerate(names):
            with self.subTest(replaced=replaced):
                root = self.root / "runtime-table-{}".format(index)
                assets = {}
                for name in names:
                    path = root / name
                    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    path.write_bytes(("trusted-" + name).encode("utf-8"))
                    path.chmod(0o500 if name == "bin/node" else 0o400)
                    assets[name] = inject_module._runtime_asset(str(path), name)
                manifest = inject_module._KimiRuntimeManifest(
                    package_root=str(root),
                    package_assets=(
                        assets["package.json"],
                        assets["dist/search-worker.mjs"],
                    ),
                    node=assets["bin/node"],
                    dylibs=tuple(
                        assets[name] for name in names if name.startswith("lib/")
                    ),
                    system_libraries=tuple(sorted((
                        "/System/Library/Frameworks/Security.framework/Security",
                        "/usr/lib/libSystem.B.dylib",
                    ))),
                )
                inject_module._validate_runtime_manifest(manifest)
                target = root / replaced
                target.chmod(0o700)
                target.write_bytes(b"replacement")
                target.chmod(0o500 if replaced == "bin/node" else 0o400)
                with self.assertRaises(SendError) as raised:
                    inject_module._validate_runtime_manifest(manifest)
                self.assertEqual("session_changed", raised.exception.code)

    def test_kimi_runtime_manifest_limits_and_parsers_fail_closed(self):
        oversized_identity = (
            "/private/oversized",
            1,
            2,
            stat.S_IFREG | 0o500,
            inject_module.KIMI_RUNTIME_FILE_BYTES + 1,
            3,
            4,
            "0" * 64,
        )
        with mock.patch.object(
            inject_module,
            "_owner_safe_file_identity",
            return_value=oversized_identity,
        ):
            with self.assertRaises(SendError):
                inject_module._runtime_asset("/private/oversized", "asset")

        with self.assertRaises(SendError):
            inject_module._capture_package_assets(self.root / "absent-package")
        with mock.patch.object(
            inject_module.os,
            "scandir",
            side_effect=PermissionError("synthetic"),
        ):
            with self.assertRaises(SendError):
                inject_module._capture_package_assets(self.root)
        unreadable_entry = mock.Mock()
        unreadable_entry.name = "entry"
        unreadable_entry.stat.side_effect = PermissionError("synthetic")
        with mock.patch.object(
            inject_module.os,
            "scandir",
            return_value=[unreadable_entry],
        ):
            with self.assertRaises(SendError):
                inject_module._capture_package_assets(self.root)
        unsafe_directory = self.root / "unsafe-package"
        unsafe_directory.mkdir(mode=0o700)
        unsafe_directory.chmod(0o722)
        with self.assertRaises(SendError):
            inject_module._capture_package_assets(unsafe_directory)
        fifo_directory = self.root / "fifo-package"
        fifo_directory.mkdir(mode=0o700)
        os.mkfifo(str(fifo_directory / "asset"))
        with self.assertRaises(SendError):
            inject_module._capture_package_assets(fifo_directory)
        limited_directory = self.root / "limited-package"
        limited_directory.mkdir(mode=0o700)
        (limited_directory / "asset").write_bytes(b"x")
        with mock.patch.object(inject_module, "KIMI_RUNTIME_ASSET_COUNT", 0):
            with self.assertRaises(SendError):
                inject_module._capture_package_assets(limited_directory)

        failed_otool = BoundedProcessResult(
            args=("/usr/bin/otool", "-L", "/private/image"),
            returncode=1,
            stdout=b"",
            stderr=b"",
        )
        with mock.patch.object(inject_module, "run_bounded", return_value=failed_otool):
            with self.assertRaises(SendError):
                inject_module._otool_dependencies("/private/image")
        with mock.patch.object(
            inject_module,
            "run_bounded",
            side_effect=RuntimeError("synthetic"),
        ):
            with self.assertRaises(SendError):
                inject_module._otool_dependencies("/private/image")
        malformed_otool = dataclasses.replace(
            failed_otool,
            returncode=0,
            stdout=b"/private/image:\nmalformed\n",
        )
        with mock.patch.object(
            inject_module,
            "run_bounded",
            return_value=malformed_otool,
        ):
            with self.assertRaises(SendError):
                inject_module._otool_dependencies("/private/image")

        for dependency in (
            "@rpath/missing.dylib",
            "relative.dylib",
            "/definitely/missing.dylib",
        ):
            with self.subTest(dependency=dependency), self.assertRaises(SendError):
                inject_module._resolve_macho_dependency(
                    self.root / "loader",
                    dependency,
                )
        loader = self.root / "loader"
        loader.write_bytes(b"loader")
        loader_dependency = self.root / "loader-dependency.dylib"
        loader_dependency.write_bytes(b"dependency")
        self.assertEqual(
            str(loader_dependency),
            inject_module._resolve_macho_dependency(
                loader,
                "@loader_path/loader-dependency.dylib",
            ),
        )
        with mock.patch.object(inject_module.sys, "platform", "linux"):
            linux_node, linux_dylibs, linux_systems = (
                inject_module._capture_macho_closure(str(loader))
            )
        self.assertEqual(str(loader), linux_node.identity[0])
        self.assertEqual(((), ()), (linux_dylibs, linux_systems))

        with self.assertRaises(SendError):
            inject_module._capture_kimi_runtime_manifest(
                str(self.root / "missing-main"),
                oversized_identity,
            )
        identity_package = self.root / "identity-package"
        (identity_package / "dist").mkdir(mode=0o700, parents=True)
        (identity_package / "package.json").write_bytes(b"{}")
        (identity_package / "dist" / "search-worker.mjs").write_bytes(b"export {};")
        identity_main = identity_package / "dist" / "main.mjs"
        identity_main.write_bytes(b"#!/usr/bin/env node\n")
        identity_main.chmod(0o500)
        wrong_identity = inject_module._executable_identity(str(identity_main))
        wrong_identity = (
            wrong_identity[0],
            *wrong_identity[1:4],
            wrong_identity[4] + 1,
            *wrong_identity[5:],
        )
        with self.assertRaises(SendError):
            inject_module._capture_kimi_runtime_manifest(
                str(identity_main),
                wrong_identity,
            )

        asset_path = self.root / "manifest-asset"
        asset_path.write_bytes(b"asset")
        asset_path.chmod(0o400)
        asset = inject_module._runtime_asset(str(asset_path), "asset")
        valid = inject_module._KimiRuntimeManifest(
            package_root=str(self.root),
            package_assets=(asset,),
        )
        inject_module._validate_runtime_manifest(valid)
        invalid_manifests = (
            object(),
            dataclasses.replace(valid, package_assets=()),
            dataclasses.replace(valid, system_libraries=("/private/library",)),
            dataclasses.replace(valid, package_assets=(asset, asset)),
            dataclasses.replace(
                valid,
                package_root=object(),
            ),
            dataclasses.replace(
                valid,
                dylibs=(dataclasses.replace(asset, relative_path="bad/library"),),
            ),
            dataclasses.replace(
                valid,
                node=dataclasses.replace(asset, relative_path="wrong-node"),
            ),
        )
        for manifest in invalid_manifests:
            with self.subTest(manifest=repr(manifest)), self.assertRaises(SendError):
                inject_module._validate_runtime_manifest(manifest)

    def test_kimi_otool_reads_fixed_node_bytes_across_source_swap(self):
        node = self.root / "node-image"
        original = b"original-node-image"
        replacement_bytes = b"replacement-node-image"
        node.write_bytes(original)
        node.chmod(0o500)
        replacement = self.root / "node-replacement"
        replacement.write_bytes(replacement_bytes)
        replacement.chmod(0o500)
        analysis_paths = []

        def inspect_fixed_bytes(path):
            analysis = Path(path)
            analysis_paths.append(analysis)
            self.assertNotEqual(node, analysis)
            self.assertEqual(original, analysis.read_bytes())
            backup = self.root / "node-backup"
            node.rename(backup)
            replacement.rename(node)
            node.rename(replacement)
            backup.rename(node)
            return ("/usr/lib/libSystem.B.dylib",)

        with mock.patch.object(inject_module.sys, "platform", "darwin"):
            with mock.patch.object(
                inject_module,
                "_otool_dependencies",
                side_effect=inspect_fixed_bytes,
            ):
                node_asset, dylibs, systems = (
                    inject_module._capture_macho_closure(str(node))
                )

        self.assertEqual(hashlib.sha256(original).hexdigest(), node_asset.identity[-1])
        self.assertEqual((), dylibs)
        self.assertEqual(("/usr/lib/libSystem.B.dylib",), systems)
        self.assertEqual(original, node.read_bytes())
        self.assertTrue(analysis_paths)
        self.assertTrue(all(not path.parent.exists() for path in analysis_paths))
        failed_paths = []

        def fail_analysis(path):
            failed_paths.append(Path(path))
            raise SendError("unsupported_kimi")

        with mock.patch.object(inject_module.sys, "platform", "darwin"):
            with mock.patch.object(
                inject_module,
                "_otool_dependencies",
                side_effect=fail_analysis,
            ):
                with self.assertRaises(SendError):
                    inject_module._capture_macho_closure(str(node))
        self.assertTrue(failed_paths)
        self.assertTrue(all(not path.parent.exists() for path in failed_paths))

    def test_kimi_otool_reads_fixed_recursive_asset_across_source_swap(self):
        node = self.root / "recursive-node"
        child = self.root / "libchild.dylib"
        node_bytes = b"recursive-node-image"
        child_bytes = b"original-child-image"
        node.write_bytes(node_bytes)
        node.chmod(0o500)
        child.write_bytes(child_bytes)
        child.chmod(0o400)
        replacement = self.root / "child-replacement"
        replacement.write_bytes(b"replacement-child-image")
        replacement.chmod(0o400)
        analysis_paths = []

        def inspect_fixed_bytes(path):
            analysis = Path(path)
            analysis_paths.append(analysis)
            self.assertNotIn(analysis, (node, child))
            if len(analysis_paths) == 1:
                self.assertEqual(node_bytes, analysis.read_bytes())
                return ("@loader_path/libchild.dylib",)
            self.assertEqual(child_bytes, analysis.read_bytes())
            backup = self.root / "child-backup"
            child.rename(backup)
            replacement.rename(child)
            child.rename(replacement)
            backup.rename(child)
            return ("/System/Library/Frameworks/Security.framework/Security",)

        with mock.patch.object(inject_module.sys, "platform", "darwin"):
            with mock.patch.object(
                inject_module,
                "_otool_dependencies",
                side_effect=inspect_fixed_bytes,
            ):
                node_asset, dylibs, systems = (
                    inject_module._capture_macho_closure(str(node))
                )

        self.assertEqual(hashlib.sha256(node_bytes).hexdigest(), node_asset.identity[-1])
        self.assertEqual(1, len(dylibs))
        self.assertEqual("lib/libchild.dylib", dylibs[0].relative_path)
        self.assertEqual(
            hashlib.sha256(child_bytes).hexdigest(),
            dylibs[0].identity[-1],
        )
        self.assertEqual(child_bytes, child.read_bytes())
        self.assertEqual(
            ("/System/Library/Frameworks/Security.framework/Security",),
            systems,
        )
        self.assertEqual(2, len(analysis_paths))
        self.assertTrue(all(not path.parent.exists() for path in analysis_paths))

    def test_kimi_manifest_analysis_cleanup_failures_fail_closed(self):
        node = self.root / "cleanup-node"
        node.write_bytes(b"synthetic-node-image")
        node.chmod(0o500)

        with mock.patch.object(inject_module.sys, "platform", "darwin"):
            with mock.patch.object(
                inject_module.tempfile,
                "mkdtemp",
                side_effect=OSError("synthetic mkdtemp failure"),
            ):
                with self.assertRaises(SendError) as creation:
                    inject_module._capture_macho_closure(str(node))
        self.assertEqual("unsupported_kimi", creation.exception.code)

        for name, cleanup_error in (
            ("rmtree-error", OSError("synthetic cleanup failure")),
            ("residual-directory", None),
        ):
            with self.subTest(name=name):
                analysis_root = self.root / name
                analysis_root.mkdir(mode=0o700)
                cleanup = (
                    mock.Mock(side_effect=cleanup_error)
                    if cleanup_error is not None
                    else mock.Mock(return_value=None)
                )
                try:
                    with mock.patch.object(inject_module.sys, "platform", "darwin"):
                        with mock.patch.object(
                            inject_module.tempfile,
                            "mkdtemp",
                            return_value=str(analysis_root),
                        ), mock.patch.object(
                            inject_module.shutil,
                            "rmtree",
                            cleanup,
                        ), mock.patch.object(
                            inject_module,
                            "_otool_dependencies",
                            return_value=(),
                        ):
                            with self.assertRaises(SendError) as raised:
                                inject_module._capture_macho_closure(str(node))
                    self.assertEqual("unsupported_kimi", raised.exception.code)
                    cleanup.assert_called_once_with(str(analysis_root))
                    self.assertTrue(analysis_root.exists())
                finally:
                    if analysis_root.exists():
                        shutil.rmtree(analysis_root)

    def test_kimi_initialize_probe_rejects_incomplete_cleanup_and_closes(self):
        manifest = inject_module._capture_kimi_runtime_manifest(
            str(self.executable),
            inject_module._executable_identity(str(self.executable)),
        )
        manifest = dataclasses.replace(
            manifest,
            node=dataclasses.replace(
                manifest.package_assets[0],
                relative_path="bin/node",
            ),
        )
        bound = mock.Mock(manifest=manifest, executable=str(self.executable))
        process = mock.Mock()
        process.read_line.return_value = json.dumps(
            {
                "id": 1,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "sessionCapabilities": {"list": {}, "resume": {}},
                    },
                },
            }
        ).encode("utf-8")
        process.terminate_tree.return_value = mock.Mock(cleanup_complete=False)

        with mock.patch.object(
            inject_module,
            "BoundedDuplexLineProcess",
            return_value=process,
        ):
            with self.assertRaises(SendError) as raised:
                inject_module._probe_bound_kimi_initialize(bound)

        self.assertEqual("unsupported_kimi", raised.exception.code)
        process.terminate_tree.assert_called_once()
        process.close.assert_called_once_with()

    def test_kimi_probes_receive_sanitized_preserving_environments(self):
        manifest = inject_module._capture_kimi_runtime_manifest(
            str(self.executable),
            inject_module._executable_identity(str(self.executable)),
        )
        manifest = dataclasses.replace(
            manifest,
            node=dataclasses.replace(
                manifest.package_assets[0],
                relative_path="bin/node",
            ),
        )
        bound = mock.Mock(manifest=manifest, executable=str(self.executable))
        version_runner = mock.Mock(
            return_value=BoundedProcessResult(
                args=(str(self.executable), "--version"),
                returncode=0,
                stdout=(KIMI_SUPPORTED_VERSION + "\n").encode("ascii"),
                stderr=b"",
            )
        )
        initialize_process = mock.Mock()
        initialize_process.read_line.return_value = json.dumps(
            {
                "id": 1,
                "result": {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": True,
                        "sessionCapabilities": {"list": {}, "resume": {}},
                    },
                },
            }
        ).encode("utf-8")
        initialize_process.terminate_tree.return_value = mock.Mock(
            cleanup_complete=True
        )
        hostile = {
            "NODE_OPTIONS": "--require=/hostile.js",
            "NODE_PATH": "/hostile/node",
            "NODE_ENV": "hostile",
            "DYLD_INSERT_LIBRARIES": "/hostile/insert.dylib",
            "DYLD_FRAMEWORK_PATH": "/hostile/frameworks",
            "DYLD_FALLBACK_FRAMEWORK_PATH": "/hostile/fallback-frameworks",
            "DYLD_FALLBACK_LIBRARY_PATH": "/hostile/fallback-libraries",
            "DYLD_LIBRARY_PATH": "/hostile/libraries",
            "DYLD_PRINT_LIBRARIES": "1",
        }
        preserved = {
            "HOME": "/safe/home",
            "KIMI_CODE_HOME": "/safe/kimi",
            "LANG": "C",
            "BENIGN_MARKER": "preserved",
        }

        with mock.patch.dict(
            os.environ,
            {**hostile, **preserved},
            clear=True,
        ), mock.patch.object(
            inject_module,
            "BoundedDuplexLineProcess",
            return_value=initialize_process,
        ) as initialize_factory:
            inject_module._probe_bound_kimi_version(bound, version_runner)
            inject_module._probe_bound_kimi_initialize(bound)

        version_env = version_runner.call_args.kwargs["env"]
        initialize_env = initialize_factory.call_args.kwargs["env"]
        self.assertEqual(version_env, initialize_env)
        self.assertIsNot(version_env, initialize_env)
        for child_env in (version_env, initialize_env):
            self.assertFalse(
                any(
                    name.startswith(("NODE_", "DYLD_"))
                    for name in child_env
                )
            )
            for name, value in preserved.items():
                self.assertEqual(value, child_env[name])
        initialize_process.terminate_tree.assert_called_once()
        initialize_process.close.assert_called_once_with()

    def test_kimi_completed_is_unknown_even_with_durable_unique_turn(self):
        plan = self.plan("durable private")
        guard_calls = []

        def guard(project, executable, *, excluded_process_group=None):
            guard_calls.append(
                (
                    (project.dev, project.ino),
                    executable.canonical_path,
                    excluded_process_group,
                )
            )

        result = self.execute(
            plan,
            self.acp_runner(),
            process_guard=guard,
        )

        self.assertEqual(("completed", "unknown"), (result.outcome, result.delivery))
        self.assertEqual(0, result.returncode)
        self.assertEqual("synthetic response", result.response)
        self.assertEqual([None, 4321], [call[2] for call in guard_calls])

    def test_kimi_cleanup_incomplete_proof_survives_containment_uncertainty(self):
        for index, scenario in enumerate(("darwin_fork_activity", "escaped_child")):
            with self.subTest(scenario=scenario):
                self._write_fixture()
                plan = self.plan("containment proof {}".format(index))
                result = self.execute(
                    plan,
                    self.acp_runner(
                        result=self._acp_result(
                            clean_exit=False,
                            cleanup_complete=False,
                            error_code="cleanup_incomplete",
                        )
                    ),
                    request_id="request-containment-proof-{}".format(index),
                )

                self.assertEqual(
                    ("completed", "unknown"),
                    (result.outcome, result.delivery),
                )
                self.assertEqual(0, result.returncode)
                self.assertIsNone(result.error_code)

    def test_kimi_error_free_path_still_requires_clean_cleanup(self):
        plan = self.plan("normal cleanup requirement")
        result = self.execute(
            plan,
            self.acp_runner(
                result=self._acp_result(
                    cleanup_complete=False,
                    error_code=None,
                )
            ),
            request_id="request-normal-cleanup-requirement",
        )

        self.assertEqual(("failed", "unknown"), (
            result.outcome,
            result.delivery,
        ))
        self.assertEqual("cleanup_incomplete", result.error_code)

    def test_kimi_protocol_timeout_overflow_and_nonzero_are_not_upgraded(self):
        cases = (
            (
                "timeout",
                self._acp_result(error_code="timeout"),
                "timed_out",
                "timeout",
            ),
            (
                "overflow",
                self._acp_result(error_code="output_overflow"),
                "overflow",
                "output_overflow",
            ),
            (
                "protocol",
                self._acp_result(error_code="protocol_error"),
                "failed",
                "protocol_error",
            ),
            (
                "nonzero",
                self._acp_result(
                    returncode=9,
                    clean_exit=False,
                    error_code="native_exit",
                ),
                "failed",
                "native_exit",
            ),
        )
        for index, (name, acp_result, outcome, error_code) in enumerate(cases):
            with self.subTest(name=name):
                self._write_fixture()
                plan = self.plan("non-completion signal " + name)
                result = self.execute(
                    plan,
                    self.acp_runner(result=acp_result),
                    request_id="request-non-completion-{}".format(index),
                )

                self.assertEqual((outcome, "unknown"), (
                    result.outcome,
                    result.delivery,
                ))
                self.assertEqual(error_code, result.error_code)

    def test_kimi_completed_unknown_keeps_cli_exit_one(self):
        from sidecar import cli as cli_module

        result = SendResult(
            agent="kimi",
            session_id=self.session_id,
            outcome="completed",
            delivery="unknown",
            returncode=0,
            request_id="request-cli-unknown",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = cli_module._write_send_result(
            result,
            as_json=True,
            message="private",
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(1, exit_code)
        self.assertEqual("completed", json.loads(stdout.getvalue())["outcome"])
        self.assertIn("do not retry", stderr.getvalue())

    def test_kimi_fake_acp_and_synthetic_home_complete_end_to_end(self):
        fake_acp = (
            Path(__file__).resolve().parent / "fixtures" / "fake_kimi_acp.py"
        )
        self.executable.write_text(
            "#!{}\n"
            "import os\n"
            "import sys\n"
            "if sys.argv[1:] == ['--version']:\n"
            "    print('0.38.0')\n"
            "    raise SystemExit(0)\n"
            "os.execv({python!r}, [{python!r}, {fake!r}] + sys.argv[1:])\n".format(
                sys.executable,
                python=sys.executable,
                fake=str(fake_acp),
            ),
            encoding="utf-8",
        )
        self.executable.chmod(0o700)
        message = "FAKE-ACP-PRIVATE"
        plan = self.plan(message)

        def process_factory(argv, **kwargs):
            kwargs["require_descendant_containment"] = False
            return BoundedDuplexLineProcess(argv, **kwargs)

        def fake_runner(request, **kwargs):
            result = run_kimi_acp(
                request,
                process_factory=process_factory,
                **kwargs,
            )
            turn_id = self._next_turn_id()
            self._append_wire(
                [
                    {
                        "type": "turn.prompt",
                        "agentId": "main",
                        "input": [{"type": "text", "text": message}],
                        "origin": {"type": "acp"},
                        "time": 1700000001000,
                    },
                    {
                        "type": "turn.ended",
                        "agentId": "main",
                        "turnId": turn_id,
                        "reason": "completed",
                        "time": 1700000002000,
                    },
                ]
            )
            self._write_json(self.state_path, self.state)
            return result

        with mock.patch.dict(
            os.environ,
            {
                "FAKE_KIMI_ACP_SCENARIO": "valid",
                "FAKE_KIMI_ACP_SESSION_ID": self.session_id,
                "FAKE_KIMI_ACP_EXPECTED_DIGEST": hashlib.sha256(
                    message.encode("utf-8")
                ).hexdigest(),
            },
            clear=False,
        ):
            result = self.execute(
                plan,
                fake_runner,
                request_id="request-fake-acp",
                timeout=5,
            )

        self.assertEqual(("completed", "unknown"), (result.outcome, result.delivery))

    def test_kimi_failed_and_wire_proof_races_are_unknown(self):
        cases = (
            ("failed", {"reason": "failed"}, "protocol_error"),
            ("blocked", {"reason": "blocked"}, "protocol_error"),
            ("cancelled", {"reason": "cancelled"}, "protocol_error"),
            ("missing_end", {"omit_end": True}, "protocol_error"),
            ("duplicate_prompt", {"duplicate_prompt": True}, "protocol_error"),
            ("stale_completed_state", {"update_state": False}, "protocol_error"),
            ("wire_replacement", {"replace_wire": True}, "protocol_error"),
            (
                "cleanup_incomplete_without_proof",
                {
                    "omit_end": True,
                    "result": self._acp_result(
                        clean_exit=False,
                        cleanup_complete=False,
                        error_code="cleanup_incomplete",
                    )
                },
                "cleanup_incomplete",
            ),
        )
        for index, (name, runner_options, error_code) in enumerate(cases):
            with self.subTest(name=name):
                self._write_fixture()
                plan = self.plan("proof race " + name)
                result = self.execute(
                    plan,
                    self.acp_runner(**runner_options),
                    request_id="request-proof-{}".format(index),
                )
                self.assertEqual("unknown", result.delivery)
                self.assertNotEqual("completed", result.outcome)
                self.assertEqual(error_code, result.error_code)

    def test_kimi_pre_prompt_error_audits_then_replays_without_spawn(self):
        plan = self.plan("preprompt private")
        calls = []

        def pre_prompt_failure(request, **kwargs):
            del request, kwargs
            calls.append("spawn")
            return self._acp_result(
                phase=AcpPhase.INITIALIZED,
                prompt_write=PromptWriteBoundary.NONE,
                stop_reason=None,
                returncode=1,
                clean_exit=True,
                error_code="protocol_error",
            )

        with self.assertRaises(SendError) as raised:
            self.execute(
                plan,
                pre_prompt_failure,
                request_id="request-preprompt",
            )
        self.assertEqual("protocol_error", raised.exception.code)

        replay = self.execute(
            plan,
            lambda *args, **kwargs: self.fail("replay must not spawn"),
            request_id="request-preprompt",
        )
        self.assertEqual(("failed", "unknown"), (replay.outcome, replay.delivery))
        self.assertEqual("protocol_error", replay.error_code)
        self.assertTrue(replay.replayed)
        self.assertEqual(["spawn"], calls)

    def test_kimi_unknown_and_old_new_completed_replays_never_spawn(self):
        completed_plan = self.plan("completed replay")
        completed_calls = []
        first = self.execute(
            completed_plan,
            self.acp_runner(calls=completed_calls),
            request_id="request-kimi-completed",
        )
        replay = self.execute(
            completed_plan,
            lambda *args, **kwargs: self.fail("completed replay spawned"),
            request_id="request-kimi-completed",
        )
        self.assertEqual(("completed", "unknown"), (first.outcome, first.delivery))
        self.assertEqual(("completed", "unknown"), (replay.outcome, replay.delivery))
        self.assertTrue(replay.replayed)
        self.assertEqual(1, len(completed_calls))

        self._write_fixture()
        old_plan = self.plan("old completed replay")
        old_identity = inject_module._send_audit_identity(
            old_plan,
            "old completed replay",
        )
        old_store = inject_module.SendAuditStore(self.runtime)
        self.assertIsNone(old_store.reserve("request-kimi-old-completed", old_identity))
        old_store.append_terminal(
            "request-kimi-old-completed",
            old_identity,
            outcome="failed",
            delivery="unknown",
            error=None,
            returncode=0,
        )
        old_calls = []
        old_replay = self.execute(
            old_plan,
            lambda *args, **kwargs: old_calls.append((args, kwargs)),
            request_id="request-kimi-old-completed",
        )
        self.assertEqual(
            ("completed", "unknown"),
            (old_replay.outcome, old_replay.delivery),
        )
        self.assertTrue(old_replay.replayed)
        self.assertEqual([], old_calls)

        self._write_fixture()
        unknown_plan = self.plan("unknown replay")
        unknown_calls = []
        unknown = self.execute(
            unknown_plan,
            self.acp_runner(reason="failed", calls=unknown_calls),
            request_id="request-kimi-unknown",
        )
        unknown_replay = self.execute(
            unknown_plan,
            lambda *args, **kwargs: self.fail("unknown replay spawned"),
            request_id="request-kimi-unknown",
        )
        self.assertEqual("unknown", unknown.delivery)
        self.assertEqual("unknown", unknown_replay.delivery)
        self.assertTrue(unknown_replay.replayed)
        self.assertEqual(1, len(unknown_calls))

    def test_kimi_request_conflict_and_live_owner_block_before_acp(self):
        first = self.plan("first request message")
        self.execute(
            first,
            self.acp_runner(),
            request_id="request-kimi-conflict",
        )
        changed = self.plan("changed request message")
        calls = []
        with self.assertRaises(SendError) as conflict:
            self.execute(
                changed,
                lambda *args, **kwargs: calls.append((args, kwargs)),
                request_id="request-kimi-conflict",
            )
        self.assertEqual("request_conflict", conflict.exception.code)
        self.assertEqual([], calls)

        self._write_fixture()
        busy = self.plan("busy request")
        with self.assertRaises(SendError) as raised:
            self.execute(
                busy,
                lambda *args, **kwargs: self.fail("busy session spawned ACP"),
                request_id="request-kimi-busy",
                process_guard=mock.Mock(side_effect=RuntimeError("live owner")),
            )
        self.assertEqual("session_busy", raised.exception.code)

    def test_kimi_structured_busy_is_always_unknown_after_prompt(self):
        plan = self.plan("busy proof")
        calls = []

        def busy(request, **kwargs):
            kwargs["before_prompt"](
                AcpProcessIdentity(pid=4321, process_group_id=4321)
            )
            self.assertNotIn("confirm_no_own_prompt", kwargs)
            calls.append(request)
            return self._acp_result(
                prompt_write=PromptWriteBoundary.COMPLETE,
                phase=AcpPhase.PROMPT_WRITTEN,
                stop_reason=None,
                returncode=0,
                error_code="session_busy",
                definitive_busy_rejection=True,
            )

        result = self.execute(plan, busy, request_id="request-kimi-turn-busy")
        self.assertEqual(("failed", "unknown"), (result.outcome, result.delivery))
        self.assertEqual("session_busy", result.error_code)
        replay = self.execute(
            plan,
            lambda *args, **kwargs: self.fail("unknown replay spawned"),
            request_id="request-kimi-turn-busy",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual("unknown", replay.delivery)
        self.assertEqual(1, len(calls))

        self._write_fixture()
        delayed_plan = self.plan("delayed busy append")
        delayed_writes = []

        def delayed_busy(request, **kwargs):
            kwargs["before_prompt"](
                AcpProcessIdentity(pid=4322, process_group_id=4322)
            )

            def append_after_result():
                time.sleep(0.05)
                self._append_wire(
                    [
                        {
                            "type": "turn.prompt",
                            "agentId": "main",
                            "input": [
                                {
                                    "type": "text",
                                    "text": request.message.decode("utf-8"),
                                }
                            ],
                        }
                    ]
                )

            delayed = threading.Thread(target=append_after_result)
            delayed.start()
            delayed_writes.append(delayed)
            return self._acp_result(
                prompt_write=PromptWriteBoundary.COMPLETE,
                phase=AcpPhase.PROMPT_WRITTEN,
                stop_reason=None,
                error_code="session_busy",
                definitive_busy_rejection=False,
            )

        delayed = self.execute(
            delayed_plan,
            delayed_busy,
            request_id="request-kimi-delayed-busy",
        )
        self.assertEqual(("failed", "unknown"), (delayed.outcome, delayed.delivery))
        for delayed_write in delayed_writes:
            delayed_write.join(timeout=2)
            self.assertFalse(delayed_write.is_alive())
        self.assertIn(b"delayed busy append", self.wire_path.read_bytes())

    def test_kimi_build_rejects_ineligible_and_malformed_identity(self):
        cases = (
            (self.session(status=Status.WORKING), "working_session"),
            (self.session(status=Status.DEAD), "dead_session"),
            (self.session(parent_id="parent"), "child_session"),
            (
                self.session(
                    session_id="{}:child".format(self.session_id),
                    extra={"source": "session_index", "agent_id": "child"},
                ),
                "child_session",
            ),
            (
                self.session(
                    extra={
                        "source": "session_index",
                        "agent_id": "main",
                        "host": "remote",
                    }
                ),
                "remote_session",
            ),
        )
        for session, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(SendError) as raised:
                    build_send_plan(
                        session,
                        "private",
                        executable_resolver=lambda _name: str(self.executable),
                    )
                self.assertEqual(code, raised.exception.code)

        changed_state = dict(self.state)
        changed_state["id"] = "other-session"
        self._write_json(self.state_path, changed_state)
        with self.assertRaises(SendError) as raised:
            self.plan("malformed identity")
        self.assertEqual("invalid_session", raised.exception.code)

    def test_kimi_message_is_absent_from_audit_argv_errors_and_repr(self):
        message = "KIMI-AUDIT-SECRET"
        plan = self.plan(message)
        result = self.execute(
            plan,
            self.acp_runner(reason="failed"),
            request_id="request-kimi-hygiene",
        )
        audit = (self.runtime / "audit.jsonl").read_bytes()

        self.assertEqual("unknown", result.delivery)
        self.assertNotIn(message, repr(plan))
        self.assertNotIn(message, repr(result))
        self.assertNotIn(message, " ".join(plan.argv))
        self.assertNotIn(message.encode("utf-8"), audit)
        self.assertNotIn(
            hashlib.sha256(message.encode("utf-8")).hexdigest().encode("ascii"),
            audit,
        )


class NativeOutputTests(InjectionTestCase):
    def completed(self, plan, stdout, stderr=b"", returncode=0):
        return BoundedProcessResult(
            args=plan.argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def execute_output(self, agent, stdout, *, message="private prompt"):
        plan = self.plan(agent, message)
        return self.execute_plan(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: self.completed(plan, stdout),
        )

    def assert_machine_output_safe(self, result):
        payload = result.to_dict()
        for value in payload.values():
            if isinstance(value, str):
                value.encode("utf-8")

        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        serialized.encode("utf-8")
        json_buffer = io.StringIO()
        json.dump(payload, json_buffer, ensure_ascii=False, allow_nan=False)
        json_buffer.getvalue().encode("utf-8")

        human_buffer = io.StringIO()
        print(
            "{}\n{}".format(result.response, result.stderr),
            file=human_buffer,
        )
        human_buffer.getvalue().encode("utf-8")

    def test_claude_result_and_cursor_assistant_content(self):
        claude = self.execute_output(
            "claude",
            json.dumps({"type": "result", "result": "Claude answer"}).encode(),
        )
        cursor = self.execute_output(
            "cursor-cli",
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "first"},
                            {"type": "text", "text": "second"},
                        ],
                    },
                }
            ).encode(),
        )

        self.assertEqual("Claude answer", claude.response)
        self.assertEqual("first\nsecond", cursor.response)

    def test_codex_jsonl_composes_agent_messages_and_prefers_final(self):
        composed = self.execute_output(
            "codex",
            b"\n".join(
                (
                    b'{"type":"thread.started","thread_id":"one"}',
                    b'{"type":"item.completed","item":'
                    b'{"type":"agent_message","text":"first"}}',
                    b"not json",
                    b'{"type":"agent_message","text":"second"}',
                )
            ),
        )
        final = self.execute_output(
            "codex",
            b"\n".join(
                (
                    b'{"type":"agent_message","text":"intermediate"}',
                    b'{"type":"turn.completed","last_agent_message":"final"}',
                )
            ),
        )

        self.assertEqual("first\nsecond", composed.response)
        self.assertEqual("final", final.response)

    def test_malformed_and_invalid_utf8_fall_back_without_crashing(self):
        malformed = self.execute_output("claude", b'{"result":')
        invalid_utf8 = self.execute_output("cursor-cli", b"\xffbroken")
        malformed_codex = self.execute_output("codex", b"{bad}\nnot-json")

        self.assertEqual('{"result":', malformed.response)
        self.assertEqual("\ufffdbroken", invalid_utf8.response)
        self.assertEqual("{bad}\nnot-json", malformed_codex.response)

    def test_all_execute_paths_normalize_unicode_for_machine_output(self):
        success = self.execute_output(
            "claude",
            b'{"result":{"content":[{"text":"nested \\ud800 value"}]}}',
        )
        codex = self.execute_output(
            "codex",
            b'{"type":"agent_message","text":"codex \\udfff value"}',
        )
        malformed = self.execute_output(
            "cursor-cli",
            b'{"result":"\\ud800"\xff',
        )

        timeout_plan = self.plan("claude")

        def timeout_runner(argv, input_data, **kwargs):
            del input_data, kwargs
            raise subprocess.TimeoutExpired(
                argv,
                1,
                output=b'{"result":"timeout \\ud800 value"}',
                stderr="timeout stderr \udfff",
            )

        timed_out = self.execute_plan(
            timeout_plan,
            allow_write=True,
            runner=timeout_runner,
        )

        failed_plan = self.plan("cursor-cli")
        failed = self.execute_plan(
            failed_plan,
            allow_write=True,
            runner=lambda *args, **kwargs: self.completed(
                failed_plan,
                b'{"result":"failure \\ud800 value"}',
                stderr="failure stderr \udfff",
                returncode=2,
            ),
        )

        self.assertEqual("nested \ufffd value", success.response)
        self.assertEqual("codex \ufffd value", codex.response)
        self.assertEqual("timeout \ufffd value", timed_out.response)
        self.assertEqual("timeout stderr \ufffd", timed_out.stderr)
        self.assertEqual("failure \ufffd value", failed.response)
        self.assertEqual("failure stderr \ufffd", failed.stderr)
        for result in (success, codex, malformed, timed_out, failed):
            self.assert_machine_output_safe(result)

    def test_valid_unicode_is_not_changed_during_normalization(self):
        valid = "中文 😀\n\t\x1b[31m ordinary text"
        result = self.execute_output(
            "claude",
            json.dumps({"result": valid}, ensure_ascii=False).encode("utf-8"),
        )

        self.assertEqual(valid, result.response)
        self.assert_machine_output_safe(result)

    def test_output_hygiene_redacts_json_forms_and_preserves_controls(self):
        message = 'secret "line"\n雪'
        escaped = json.dumps(message, ensure_ascii=False)[1:-1]
        ascii_escaped = json.dumps(message, ensure_ascii=True)[1:-1]
        response = " | ".join(
            (
                message,
                escaped,
                ascii_escaped,
                "controls \x1b[31m\tremain",
                "surrogate \ud800 replaced",
            )
        )

        result = self.execute_output(
            "claude",
            json.dumps({"result": response}).encode("utf-8"),
            message=message,
        )

        self.assertEqual(3, result.response.count("[message redacted]"))
        self.assertNotIn(message, result.response)
        self.assertNotIn(escaped, result.response)
        self.assertNotIn(ascii_escaped, result.response)
        self.assertIn("controls \x1b[31m\tremain", result.response)
        self.assertIn("surrogate \ufffd replaced", result.response)
        self.assert_machine_output_safe(result)

    def test_large_message_is_redacted_from_large_bounded_output(self):
        message = "s" * MAX_MESSAGE_BYTES
        prefix = "x" * (256 * 1024)
        result = self.execute_output(
            "claude",
            json.dumps({"result": prefix + message}).encode("utf-8"),
            message=message,
        )

        self.assertTrue(result.response.startswith(prefix))
        self.assertTrue(result.response.endswith("[message redacted]"))
        self.assertNotIn(message, result.response)
        self.assert_machine_output_safe(result)

    def test_result_repr_dict_and_error_never_retain_submitted_message(self):
        message = "DO-NOT-RETURN-THIS"
        plan = self.plan("claude", message)
        output = json.dumps({"result": "echo " + message}).encode("utf-8")
        result = self.execute_plan(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: self.completed(
                plan,
                output,
                stderr=("stderr " + message).encode("utf-8"),
            ),
        )

        self.assertIsInstance(result, SendResult)
        self.assertNotIn(message, repr(plan))
        self.assertNotIn(message, repr(result))
        self.assertNotIn(message, json.dumps(result.to_dict()))
        self.assertNotIn("message", result.to_dict())
        with self.assertRaises(SendError) as raised:
            validate_message(message + "\x00")
        self.assertNotIn(message, str(raised.exception))
        self.assertNotIn(message, repr(raised.exception))
        self.assertEqual({"code": "message_nul"}, raised.exception.to_dict())

    def test_result_is_frozen_and_machine_safe(self):
        result = self.execute_output("claude", b'{"result":"done"}')

        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.response = "changed"
        encoded = json.dumps(result.to_dict(), allow_nan=False)
        self.assertIn('"outcome": "completed"', encoded)
        self.assert_machine_output_safe(result)

    def test_execute_rejects_tampered_plan_before_runner(self):
        original = self.plan()
        tampered = SendPlan(
            agent=original.agent,
            session_id=original.session_id,
            executable=original.executable,
            argv=original.argv + ("--dangerously-skip-permissions",),
            cwd=original.cwd,
            target=original.target,
            input_data=original.input_data,
            prompt_transport=original.prompt_transport,
        )
        calls = []

        with self.assertRaises(SendError) as raised:
            self.execute_plan(
                tampered,
                allow_write=True,
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )

        self.assertEqual("invalid_plan", raised.exception.code)
        self.assertEqual([], calls)

    def test_output_helpers_cover_nested_and_malformed_native_protocols(self):
        self.assertEqual((b"text", False), inject_module._output_bytes("text", 8))
        self.assertEqual((b"byt", True), inject_module._output_bytes(b"bytes", 3))
        self.assertEqual((b"arr", True), inject_module._output_bytes(bytearray(b"array"), 3))
        self.assertEqual((b"", False), inject_module._output_bytes(object(), 3))

        nested = {
            "ignored": "value",
            "content": [
                {"text": "first"},
                {"message": {"output": "second"}},
                b"ignored",
            ],
        }
        self.assertEqual("first\nsecond", inject_module._content_text(nested))
        self.assertEqual("", inject_module._content_text([[[[[[[[[["deep"]]]]]]]]]]))

        self.assertEqual([{"one": 1}], inject_module._json_values('{"one":1}'))
        self.assertEqual(
            [{"one": 1}, {"two": 2}],
            inject_module._json_values(
                'invalid\n\n{"one":1}\nnot-json\n{"two":2}'
            ),
        )
        self.assertEqual(
            "",
            inject_module._assistant_record_text("not-a-record"),
        )
        self.assertEqual(
            "",
            inject_module._assistant_record_text(
                {"type": "user", "content": "not assistant"}
            ),
        )
        self.assertEqual(
            "final",
            inject_module._assistant_record_text({"result": {"text": "final"}}),
        )
        self.assertEqual(
            "nested",
            inject_module._parse_claude_or_cursor(
                json.dumps(
                    [
                        {"type": "user", "content": "skip"},
                        {
                            "type": "message",
                            "message": {
                                "role": "assistant",
                                "content": {"text": "nested"},
                            },
                        },
                    ]
                )
            ),
        )

        self.assertEqual(("", ""), inject_module._codex_record_text("invalid"))
        message, final = inject_module._codex_record_text(
            {
                "payload": {
                    "item": {"type": "agent-message", "message": "step"},
                    "last_agent_message": "final",
                }
            }
        )
        self.assertEqual(("step", "final"), (message, final))
        self.assertEqual(
            "final",
            inject_module._parse_codex(
                '{"type":"agent_message","text":"step"}\n'
                '{"last_agent_message":"final"}'
            ),
        )
        self.assertEqual("raw payload", inject_module._parse_codex("raw payload"))

    def test_result_helpers_enforce_utf8_bounds_and_integer_returncodes(self):
        self.assertEqual("ab", inject_module._bounded_result_text("ab雪", 2))
        self.assertEqual(7, inject_module._result_returncode(mock.Mock(returncode=7)))
        self.assertIsNone(
            inject_module._result_returncode(mock.Mock(returncode=True))
        )
        self.assertIsNone(inject_module._result_returncode(object()))

    def test_plan_and_result_value_objects_reject_inconsistent_states(self):
        with self.assertRaises(ValueError):
            SendError("not-a-public-code")
        self.assertEqual(
            {"code": "invalid_plan"},
            SendError("invalid_plan").to_dict(),
        )

        original = self.plan()
        bytearray_plan = SendPlan(
            agent=original.agent,
            session_id=original.session_id,
            executable=original.executable,
            argv=list(original.argv),
            cwd=original.cwd,
            target=original.target,
            input_data=bytearray(b"message"),
        )
        self.assertIsInstance(bytearray_plan.argv, tuple)
        self.assertEqual(b"message", bytearray_plan.input_data)

        invalid_results = (
            {"request_id": "-invalid"},
            {"replayed": 1},
            {"returncode": True},
            {"outcome": "unknown-outcome"},
            {"delivery": "unknown-state"},
            {"outcome": "failed", "delivery": "delivered", "returncode": 0},
        )
        base = {
            "agent": "claude",
            "session_id": "session",
            "outcome": "failed",
            "delivery": "unknown",
        }
        for update in invalid_results:
            with self.subTest(update=update):
                with self.assertRaises(ValueError):
                    SendResult(**{**base, **update})
        completed_unknown = SendResult(
            agent="kimi",
            session_id="session",
            outcome="completed",
            delivery="unknown",
            returncode=0,
        )
        self.assertEqual(("completed", "unknown"), (
            completed_unknown.outcome,
            completed_unknown.delivery,
        ))

    def test_identity_boundary_helpers_map_filesystem_failures_to_stable_codes(self):
        with self.assertRaises(SendError) as raised:
            inject_module._validate_session_id("\ud800")
        self.assertEqual("invalid_session_id", raised.exception.code)

        for invalid in (object(), "", "relative", "nul\x00path"):
            with self.subTest(project=invalid):
                with self.assertRaises(SendError) as raised:
                    inject_module._resolve_project(invalid)
                self.assertEqual("invalid_project", raised.exception.code)

        with self.assertRaises(SendError) as raised:
            inject_module._directory_identity(str(self.root / "missing"))
        self.assertEqual("invalid_project", raised.exception.code)
        regular = self.root / "regular"
        regular.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(SendError) as raised:
            inject_module._directory_identity(str(regular))
        self.assertEqual("invalid_project", raised.exception.code)

        with self.assertRaises(SendError) as raised:
            inject_module._transcript_path("relative")
        self.assertEqual("session_unavailable", raised.exception.code)
        optional = inject_module._stat_source(
            str(self.root / "optional-missing"),
            required=False,
        )
        self.assertFalse(optional[1])
        with self.assertRaises(SendError) as raised:
            inject_module._stat_source(str(self.root), required=True)
        self.assertEqual("session_unavailable", raised.exception.code)

        invalid_session = self.session(extra={"host": object()})
        with self.assertRaises(SendError) as raised:
            inject_module._critical_identity(invalid_session)
        self.assertEqual("invalid_session", raised.exception.code)

        with self.assertRaises(SendError) as raised:
            inject_module._resolve_executable(
                "claude",
                mock.Mock(side_effect=OSError("resolver failed")),
            )
        self.assertEqual("executable_not_found", raised.exception.code)
        with self.assertRaises(SendError) as raised:
            inject_module._resolve_executable("claude", lambda _name: None)
        self.assertEqual("executable_not_found", raised.exception.code)
        with self.assertRaises(SendError) as raised:
            inject_module._session_agent(mock.Mock(agent=42))
        self.assertEqual("unsupported_agent", raised.exception.code)
        with self.assertRaises(SendError) as raised:
            inject_module._validate_session(object())
        self.assertEqual("invalid_session", raised.exception.code)

    def test_preflight_rejects_each_tampered_plan_field_before_filesystem_use(self):
        original = self.plan()
        tampered = (
            object(),
            dataclasses.replace(original, agent="unsupported"),
            dataclasses.replace(original, target=object()),
            dataclasses.replace(original, prompt_transport="argv"),
            dataclasses.replace(original, cwd="relative"),
            dataclasses.replace(original, executable="relative"),
            dataclasses.replace(original, input_data=None),
            dataclasses.replace(original, input_data=b"\xff"),
            dataclasses.replace(
                original,
                target=dataclasses.replace(original.target, session_id="changed"),
            ),
        )
        for plan in tampered:
            with self.subTest(plan_type=type(plan).__name__):
                with self.assertRaises(SendError) as raised:
                    inject_module._preflight_plan(plan, filesystem=False)
                self.assertEqual("invalid_plan", raised.exception.code)
        with self.assertRaises(SendError) as raised:
            inject_module._preflight_plan(
                dataclasses.replace(original, input_data=b"   "),
                filesystem=False,
            )
        self.assertEqual("blank_message", raised.exception.code)

        cursor = self.plan("cursor-cli")
        with self.assertRaises(SendError) as raised:
            inject_module._plan_message(
                dataclasses.replace(cursor, input_data=b"unexpected")
            )
        self.assertEqual("invalid_plan", raised.exception.code)
        with self.assertRaises(SendError) as raised:
            inject_module._plan_message(dataclasses.replace(original, agent="other"))
        self.assertEqual("invalid_plan", raised.exception.code)

    def test_runtime_directory_validation_rejects_unsafe_platform_boundaries(self):
        for value in (object(), "relative", "/tmp/../unsafe"):
            with self.subTest(value=value):
                with self.assertRaises(SendError) as raised:
                    inject_module._runtime_root(value)
                self.assertEqual("unsafe_lock", raised.exception.code)

        regular_details = os.stat(str(self.executable))
        with self.assertRaises(SendError):
            inject_module._validate_directory(regular_details, private=True)
        os.chmod(str(self.root), 0o755)
        try:
            private_details = os.stat(str(self.root))
            with self.assertRaises(SendError):
                inject_module._validate_directory(private_details, private=True)
        finally:
            os.chmod(str(self.root), 0o700)
        with mock.patch(
            "sidecar.inject.os.stat",
            side_effect=OSError("stat failed"),
        ):
            with self.assertRaises(SendError) as raised:
                inject_module._entry_stat(1, "missing")
            self.assertEqual("unsafe_lock", raised.exception.code)

    def test_descriptor_boundary_failures_close_or_reject_before_mutation(self):
        with mock.patch(
            "sidecar.inject.os.open",
            side_effect=FileNotFoundError(),
        ):
            with self.assertRaises(SendError):
                inject_module._open_directory_at(
                    1,
                    "missing",
                    create=False,
                    private=True,
                )

        with mock.patch(
            "sidecar.inject.os.open",
            side_effect=FileNotFoundError(),
        ), mock.patch(
            "sidecar.inject.os.mkdir",
            side_effect=OSError("mkdir failed"),
        ):
            with self.assertRaises(SendError):
                inject_module._open_directory_at(
                    1,
                    "missing",
                    create=True,
                    private=True,
                )

        with mock.patch(
            "sidecar.inject._open_runtime_parent",
            return_value=([3, 4], [], 4, "runtime", "/runtime"),
        ), mock.patch(
            "sidecar.inject._open_directory_at",
            side_effect=SendError("unsafe_lock"),
        ), mock.patch("sidecar.inject.os.close") as close:
            with self.assertRaises(SendError):
                inject_module._open_runtime_directories("/runtime")
        self.assertEqual([mock.call(4), mock.call(3)], close.call_args_list)

        with self.assertRaises(SendError):
            inject_module._open_runtime_parent("/")
        with mock.patch(
            "sidecar.inject.os.open",
            side_effect=OSError("root open failed"),
        ):
            with self.assertRaises(SendError):
                inject_module._open_runtime_parent("/tmp/runtime")

        with mock.patch(
            "sidecar.inject.os.fstat",
            side_effect=OSError("fstat failed"),
        ):
            with self.assertRaises(SendError):
                inject_module._validate_lock_file(1, "lock", 2)
        with mock.patch(
            "sidecar.inject.os.open",
            side_effect=OSError("lock open failed"),
        ):
            with self.assertRaises(SendError):
                inject_module._open_lock_file(1, "lock")

        with mock.patch("sidecar.inject.fcntl", None):
            with self.assertRaises(SendError):
                with inject_module._session_lock("claude", "session", self.runtime):
                    self.fail("unsafe lock unexpectedly opened")

        with self.assertRaises(SendError) as raised:
            inject_module._refreshed_send_plan(
                self.plan(),
                "message",
                mock.Mock(side_effect=RuntimeError("refresh failed")),
                lambda _name: str(self.executable),
            )
        self.assertEqual("session_unavailable", raised.exception.code)

    def test_codex_and_kimi_completion_helpers_accept_terminal_contracts(self):
        text, final = inject_module._codex_record_text(
            {"last_agent_message": "already-final", "item": {}}
        )
        self.assertEqual(("", "already-final"), (text, final))
        records = [
            {
                "type": "turn.prompt",
                "agentId": "main",
                "input": [{"type": "text", "text": "hello"}],
            },
            {"type": "turn.ended", "agentId": "worker", "turnId": 1},
            {"type": "turn.started", "agentId": "main"},
            {
                "type": "turn.ended",
                "agentId": "main",
                "turnId": 2,
                "reason": "completed",
            },
        ]
        self.assertTrue(inject_module._kimi_durable_completed(records, "hello", 1))

    def test_kimi_plan_close_is_safe_for_non_kimi_targets(self):
        inject_module._close_kimi_plan(self.plan())

    def test_audit_failure_recording_does_not_mask_the_original_failure(self):
        namespace = mock.Mock()
        namespace.append_terminal.side_effect = RuntimeError("audit unavailable")
        inject_module._append_audit_failure_terminal(
            namespace,
            "functional-request",
            mock.Mock(),
            "protocol_error",
        )
        namespace.append_terminal.assert_called_once()

    def test_native_send_rejects_kimi_without_a_bound_executable(self):
        plan = dataclasses.replace(self.plan(), transport="kimi_acp")
        with self.assertRaises(SendError) as raised:
            inject_module._run_native_send(
                plan,
                "message",
                bound_kimi_executable=None,
                bounded_timeout=1.0,
                refresher=lambda: (),
                executable_resolver=lambda _path: None,
                runtime_dir=self.runtime,
                runtime_namespace=mock.Mock(),
                runner=mock.Mock(),
                monotonic=time.monotonic,
                request_id="functional-request",
                version_runner=mock.Mock(),
                kimi_runner=mock.Mock(),
                process_guard=mock.Mock(),
            )
        self.assertEqual("unsupported_kimi", raised.exception.code)

    def test_kimi_send_closes_refreshed_plan_when_identity_is_invalid(self):
        plan = self.plan()
        with mock.patch.object(
            inject_module,
            "_refreshed_send_plan",
            return_value=plan,
        ), mock.patch.object(inject_module, "_session_lock") as session_lock:
            session_lock.return_value.__enter__.return_value = mock.Mock()
            with self.assertRaises(SendError) as raised:
                inject_module._run_kimi_send(
                    plan,
                    "message",
                    bound_executable=mock.Mock(),
                    bounded_timeout=1.0,
                    refresher=mock.Mock(),
                    executable_resolver=mock.Mock(),
                    runtime_dir=self.runtime,
                    runtime_namespace=mock.Mock(),
                    monotonic=time.monotonic,
                    request_id="functional-request",
                    version_runner=mock.Mock(),
                    kimi_runner=mock.Mock(),
                    process_guard=mock.Mock(),
                )
        self.assertEqual("invalid_plan", raised.exception.code)

    def test_kimi_suffix_reader_rejects_generation_and_prefix_mutations(self):
        evidence = mock.Mock()
        boundary = mock.Mock(wire_offset=3)
        evidence.root_wire_generation.size = 2
        with self.assertRaises(SendError):
            inject_module._kimi_suffix_records(evidence, boundary)

        evidence.root_wire_generation.size = 6
        evidence._anchors.descriptor.return_value = 9
        boundary.evidence = evidence
        with mock.patch.object(
            inject_module.os,
            "pread",
            side_effect=[b"bad", b"xyz"],
        ), mock.patch.object(inject_module, "_read_kimi_file", return_value=b"abc"):
            with self.assertRaises(SendError):
                inject_module._kimi_suffix_records(evidence, boundary)

        with mock.patch.object(
            inject_module.os,
            "pread",
            side_effect=[b"abc", b""],
        ), mock.patch.object(inject_module, "_read_kimi_file", return_value=b"abc"):
            with self.assertRaises(SendError):
                inject_module._kimi_suffix_records(evidence, boundary)

    def test_kimi_version_and_runtime_snapshot_guards_fail_closed(self):
        identity = ("x", 1, 2, 3, 4, 5, 6, "digest")
        with mock.patch.object(
            inject_module,
            "_executable_identity",
            return_value=identity,
        ):
            with self.assertRaises(SendError):
                inject_module._probe_kimi_version(
                    "kimi",
                    identity,
                    mock.Mock(side_effect=RuntimeError("runner failed")),
                )

        before = mock.Mock(
            st_mode=inject_module.stat.S_IFDIR,
            st_uid=os.geteuid(),
            st_size=0,
        )
        with mock.patch.object(inject_module.os, "open", return_value=41), mock.patch.object(
            inject_module.os,
            "fstat",
            return_value=before,
        ), mock.patch.object(inject_module.os, "close"):
            with self.assertRaises(SendError):
                inject_module._snapshot_runtime_asset_for_analysis(
                    "/tmp/kimi",
                    self.runtime / "snapshot",
                    "snapshot",
                )

        regular = mock.Mock(
            st_mode=inject_module.stat.S_IFREG | 0o600,
            st_uid=os.geteuid(),
            st_size=0,
        )
        with mock.patch.object(
            inject_module,
            "KIMI_RUNTIME_FILE_BYTES",
            1,
        ), mock.patch.object(
            inject_module.os,
            "open",
            side_effect=[41, 42],
        ), mock.patch.object(
            inject_module.os,
            "fstat",
            return_value=regular,
        ), mock.patch.object(
            inject_module.os,
            "read",
            return_value=b"xx",
        ), mock.patch.object(inject_module.os, "close"):
            with self.assertRaises(SendError):
                inject_module._snapshot_runtime_asset_for_analysis(
                    "/tmp/kimi",
                    self.runtime / "snapshot-overflow",
                    "snapshot",
                )

        safe_source = mock.Mock(
            st_mode=inject_module.stat.S_IFREG | 0o600,
            st_uid=os.geteuid(),
            st_size=1,
        )
        unsafe_snapshot = mock.Mock(
            st_mode=inject_module.stat.S_IFREG | 0o600,
            st_uid=os.geteuid(),
            st_size=1,
        )
        with mock.patch.object(
            inject_module.os,
            "open",
            side_effect=[41, 42],
        ), mock.patch.object(
            inject_module.os,
            "fstat",
            side_effect=[safe_source, unsafe_snapshot],
        ), mock.patch.object(
            inject_module.os,
            "read",
            side_effect=[b"x", b""],
        ), mock.patch.object(
            inject_module,
            "_write_all",
        ), mock.patch.object(inject_module.os, "fsync"), mock.patch.object(
            inject_module.os,
            "fchmod",
        ), mock.patch.object(inject_module.os, "close"):
            with self.assertRaises(SendError):
                inject_module._snapshot_runtime_asset_for_analysis(
                    "/tmp/kimi",
                    self.runtime / "snapshot-mode",
                    "snapshot",
                )

    def test_otool_dependency_probe_translates_unexpected_failures(self):
        with mock.patch.object(
            inject_module,
            "run_bounded",
            side_effect=RuntimeError("otool unavailable"),
        ):
            with self.assertRaises(SendError) as raised:
                inject_module._otool_dependencies("/tmp/node")
        self.assertEqual("unsupported_kimi", raised.exception.code)
        with mock.patch.object(
            inject_module,
            "run_bounded",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                inject_module._otool_dependencies("/tmp/node")


if __name__ == "__main__":
    unittest.main()
