import dataclasses
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

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
from sidecar.process_runner import BoundedProcessResult


class InjectionTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.executable = self.root / "agent-executable"
        self.executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.executable.chmod(0o700)

    def tearDown(self):
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
            transcript=str(self.root / "transcript"),
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
    def completed(self, plan, *, returncode=0, stdout=b"", stderr=b"", overflow=None):
        return BoundedProcessResult(
            args=plan.argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            overflow=overflow,
        )

    def test_hard_gate_is_identity_true_and_never_calls_runner(self):
        plan = self.plan()
        for allow_write in (False, None, 0, 1, "true"):
            calls = []

            def runner(*args, **kwargs):
                calls.append((args, kwargs))
                return self.completed(plan)

            with self.subTest(allow_write=allow_write):
                with self.assertRaises(SendError) as raised:
                    execute_send(
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

        result = execute_send(
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
                "monotonic": clock,
            },
            kwargs,
        )
        self.assertEqual("completed", result.outcome)
        self.assertEqual("delivered", result.delivery)
        self.assertEqual(0, result.returncode)
        self.assertEqual("native response", result.response)
        self.assertEqual("warning", result.stderr)

    def test_cursor_runner_gets_no_stdin(self):
        plan = self.plan("cursor-cli", "argv prompt")
        calls = []

        def runner(argv, input_data, **kwargs):
            calls.append((argv, input_data, kwargs))
            return self.completed(plan, stdout=b'{"result":"done"}')

        result = execute_send(plan, allow_write=True, runner=runner)

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

        result = execute_send(plan, allow_write=True, runner=runner)

        self.assertEqual("timed_out", result.outcome)
        self.assertEqual("unknown", result.delivery)
        self.assertEqual("timeout", result.error_code)
        self.assertNotIn(message, result.response)
        self.assertNotIn(message, result.stderr)

    def test_overflow_and_nonzero_are_unknown(self):
        plan = self.plan()
        overflow = execute_send(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: self.completed(
                plan,
                returncode=-9,
                stdout=b"partial",
                overflow="stdout",
            ),
        )
        failed = execute_send(
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

        result = execute_send(
            plan,
            allow_write=True,
            runner=lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("spawn failed")
            ),
        )

        self.assertEqual("failed", result.outcome)
        self.assertEqual("unknown", result.delivery)
        self.assertEqual("spawn_error", result.error_code)

        self.executable.unlink()
        calls = []
        with self.assertRaises(SendError) as raised:
            execute_send(
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
            execute_send(plan, allow_write=True, runner=runner)

    def test_timeout_range_is_finite_inclusive_and_capped_at_900(self):
        plan = self.plan()
        calls = []

        def runner(*args, **kwargs):
            calls.append(kwargs["timeout"])
            return self.completed(plan)

        for timeout in (1, 1.5, 900):
            execute_send(
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
                    execute_send(
                        plan,
                        allow_write=True,
                        timeout=timeout,
                        runner=runner,
                    )
                self.assertEqual("invalid_timeout", raised.exception.code)
                self.assertEqual(before, len(calls))


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
        return execute_send(
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

        timed_out = execute_send(
            timeout_plan,
            allow_write=True,
            runner=timeout_runner,
        )

        failed_plan = self.plan("cursor-cli")
        failed = execute_send(
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

    def test_result_repr_dict_and_error_never_retain_submitted_message(self):
        message = "DO-NOT-RETURN-THIS"
        plan = self.plan("claude", message)
        output = json.dumps({"result": "echo " + message}).encode("utf-8")
        result = execute_send(
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
            input_data=original.input_data,
            prompt_transport=original.prompt_transport,
        )
        calls = []

        with self.assertRaises(SendError) as raised:
            execute_send(
                tampered,
                allow_write=True,
                runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )

        self.assertEqual("invalid_plan", raised.exception.code)
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
