import dataclasses
import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import sidecar.inject as inject_module
from sidecar.inject import (
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
from sidecar.model import Session, Status
from sidecar.process_runner import (
    BoundedProcessResult,
    DescendantContainmentUnsupportedError,
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
            "kimi": "unsupported_kimi",
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

    @unittest.skipUnless(os.name == "posix", "gated supervisor requires POSIX")
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

        self.assertEqual("failed", incomplete.outcome)
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
            {"outcome": "completed", "delivery": "unknown", "returncode": 0},
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


if __name__ == "__main__":
    unittest.main()
