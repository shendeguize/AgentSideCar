import dataclasses
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sidecar.kimi_acp import (
    ACP_LINE_BYTES,
    AcpPhase,
    KimiAcpRequest,
    PromptWriteBoundary,
    run_kimi_acp,
)
from sidecar.process_runner import (
    AcpProcessIdentity,
    BoundedDuplexLineProcess,
    BoundedDuplexLineProcessCancelledError,
    BoundedDuplexLineProcessError,
    BoundedDuplexLineProcessTimeoutError,
    DescendantContainmentUnsupportedError,
    DuplexProcessResult,
    DuplexWriteBoundary,
    DuplexWriteResult,
    MAX_TIMEOUT_SECONDS,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = (REPO_ROOT / "tests" / "fixtures" / "fake_kimi_acp.py").resolve()
SECRET_MESSAGE = "PRIVATE-PROMPT-DO-NOT-PERSIST"


class SignalOnBoundary:
    def __init__(self, error):
        self.error = error

    @property
    def boundary(self):
        raise self.error


def uncontained_process_factory(argv, **kwargs):
    if kwargs.get("require_descendant_containment") is not True:
        raise AssertionError("driver did not request descendant containment")
    kwargs["require_descendant_containment"] = False
    return BoundedDuplexLineProcess(argv, **kwargs)


class ScriptedProcess:
    def __init__(
        self,
        argv,
        responses,
        *,
        partial_prompt=False,
        prompt_error_boundary=None,
        prompt_base_exception=None,
        prompt_base_boundary=DuplexWriteBoundary.PARTIAL,
        prompt_signal_window="after_record",
        cancel_base_exception=None,
        **kwargs,
    ):
        self.argv = tuple(argv)
        self.kwargs = kwargs
        self.responses = list(responses)
        self.partial_prompt = partial_prompt
        self.prompt_error_boundary = prompt_error_boundary
        self.prompt_base_exception = prompt_base_exception
        self.prompt_base_boundary = prompt_base_boundary
        self.prompt_signal_window = prompt_signal_window
        self.cancel_base_exception = cancel_base_exception
        self.writes = []
        self.deadlines = []
        self.stderr = b""
        self.result = None
        self._write_result = None
        self.boundary_before_cancel = None
        self.closed = False
        self.stdin_closed = False
        self.identity = AcpProcessIdentity(pid=4321, process_group_id=4321)

    def write_line(self, payload, *, deadline):
        self.deadlines.append(deadline)
        value = json.loads(payload.decode("utf-8"))
        self.writes.append(value)
        total = len(payload) + 1
        if value.get("method") == "session/cancel":
            self.boundary_before_cancel = (
                None if self._write_result is None else self._write_result.boundary
            )
        if (
            self.cancel_base_exception is not None
            and value.get("method") == "session/cancel"
        ):
            error = self.cancel_base_exception
            self.cancel_base_exception = None
            raise error
        if (
            self.prompt_base_exception is not None
            and self.prompt_signal_window == "before_record"
            and value.get("method") == "session/prompt"
        ):
            error = self.prompt_base_exception
            self.prompt_base_exception = None
            raise error
        if (
            self.prompt_base_exception is not None
            and self.prompt_signal_window == "after_record"
            and value.get("method") == "session/prompt"
        ):
            error = self.prompt_base_exception
            self.prompt_base_exception = None
            boundary = self.prompt_base_boundary
            written = total if boundary is DuplexWriteBoundary.COMPLETE else 2
            self._write_result = DuplexWriteResult(boundary, written, total)
            raise error
        if (
            self.prompt_error_boundary is not None
            and value.get("method") == "session/prompt"
        ):
            boundary = self.prompt_error_boundary
            self.prompt_error_boundary = None
            written = total if boundary is DuplexWriteBoundary.COMPLETE else 2
            self._write_result = DuplexWriteResult(boundary, written, total)
            raise BoundedDuplexLineProcessTimeoutError(
                "synthetic typed write failure",
                write_result=self._write_result,
            )
        if self.partial_prompt and value.get("method") == "session/prompt":
            self._write_result = DuplexWriteResult(
                DuplexWriteBoundary.PARTIAL,
                2,
                total,
            )
            return self._write_result
        if (
            self.prompt_base_exception is not None
            and self.prompt_signal_window == "after_return"
            and value.get("method") == "session/prompt"
        ):
            error = self.prompt_base_exception
            self.prompt_base_exception = None
            boundary = self.prompt_base_boundary
            written = total if boundary is DuplexWriteBoundary.COMPLETE else 2
            self._write_result = DuplexWriteResult(boundary, written, total)
            return SignalOnBoundary(error)
        self._write_result = DuplexWriteResult(
            DuplexWriteBoundary.COMPLETE,
            total,
            total,
        )
        return self._write_result

    @property
    def write_result(self):
        return self._write_result

    def read_line(self, *, deadline):
        del deadline
        if not self.responses:
            return None
        value = self.responses.pop(0)
        if isinstance(value, bytes):
            return value
        return json.dumps(value, separators=(",", ":")).encode("utf-8")

    def close_stdin(self):
        self.stdin_closed = True

    def wait_clean(self, *, deadline):
        del deadline
        if self.result is None:
            self.result = DuplexProcessResult(
                args=self.argv,
                returncode=0,
                stderr=b"",
                stdout_bytes_read=0,
                cleanup_complete=True,
                clean_exit=True,
            )
        return self.result

    def terminate_tree(self, *, deadline):
        return self.wait_clean(deadline=deadline)

    def close(self):
        self.closed = True


def lifecycle_responses(cwd, session_id="session-1", prompt_result=None):
    if prompt_result is None:
        prompt_result = {"stopReason": "end_turn"}
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "sessionCapabilities": {"list": {}, "resume": {}},
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "sessions": [{"sessionId": session_id, "cwd": cwd}]
            },
        },
        {"jsonrpc": "2.0", "id": 3, "result": {}},
        {"jsonrpc": "2.0", "id": 4, "result": {}},
        {"jsonrpc": "2.0", "id": 5, "result": prompt_result},
    ]


class KimiAcpTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cwd = os.path.realpath(self.temporary.name)
        self.request = KimiAcpRequest(
            executable=str(FAKE_ACP),
            cwd=self.cwd,
            session_id="session-1",
            request_id="request-1",
            message=SECRET_MESSAGE.encode("utf-8"),
            deadline=time.monotonic() + 3,
        )

    def run_scenario(
        self,
        scenario,
        *,
        timeout=3,
        process_factory=uncontained_process_factory,
        confirm_no_own_prompt=None,
        artifact_dir=None,
    ):
        request = dataclasses.replace(
            self.request,
            deadline=time.monotonic() + timeout,
        )
        environment = {
            "FAKE_KIMI_ACP_SCENARIO": scenario,
            "FAKE_KIMI_ACP_SESSION_ID": request.session_id,
            "FAKE_KIMI_ACP_EXPECTED_DIGEST": hashlib.sha256(
                request.message
            ).hexdigest(),
        }
        if artifact_dir is not None:
            environment["FAKE_KIMI_ACP_ARTIFACT_DIR"] = str(artifact_dir)
        with mock.patch.dict(os.environ, environment, clear=False):
            return run_kimi_acp(
                request,
                before_prompt=lambda _identity: None,
                validate_session_cwd=lambda value: os.path.samefile(
                    value,
                    request.cwd,
                ),
                confirm_no_own_prompt=confirm_no_own_prompt,
                process_factory=process_factory,
            )

    def test_request_repr_hides_message_and_exact_sequence(self):
        events = []
        holder = {}

        def factory(argv, **kwargs):
            events.append(("spawn", tuple(argv)))
            process = ScriptedProcess(
                argv,
                lifecycle_responses(self.cwd),
                **kwargs,
            )
            holder["process"] = process
            return process

        def validate_cwd(value):
            events.append(("cwd", value))
            return value == self.cwd

        def before_prompt(identity):
            events.append(("before_prompt", identity.pid))

        result = run_kimi_acp(
            self.request,
            before_prompt=before_prompt,
            validate_session_cwd=validate_cwd,
            process_factory=factory,
        )
        process = holder["process"]
        methods = [value.get("method") for value in process.writes]

        self.assertEqual((str(FAKE_ACP), "acp"), process.argv)
        self.assertEqual(
            [
                "initialize",
                "session/list",
                "session/resume",
                "session/set_mode",
                "session/prompt",
            ],
            methods,
        )
        self.assertEqual(
            [
                ("spawn", (str(FAKE_ACP), "acp")),
                ("cwd", self.cwd),
                ("before_prompt", 4321),
            ],
            events,
        )
        self.assertEqual(
            {},
            process.writes[0]["params"]["clientCapabilities"],
        )
        self.assertEqual(
            [],
            process.writes[2]["params"]["mcpServers"],
        )
        self.assertEqual(
            {"sessionId": "session-1", "modeId": "default"},
            process.writes[3]["params"],
        )
        self.assertEqual(
            "request-1",
            process.writes[4]["params"]["messageId"],
        )
        self.assertEqual(
            [{"type": "text", "text": SECRET_MESSAGE}],
            process.writes[4]["params"]["prompt"],
        )
        for value in process.writes[:4]:
            self.assertNotIn(SECRET_MESSAGE, json.dumps(value))
        self.assertNotIn(SECRET_MESSAGE, repr(self.request))
        self.assertNotIn(SECRET_MESSAGE, repr(result))
        self.assertEqual(PromptWriteBoundary.COMPLETE, result.prompt_write)
        self.assertEqual(AcpPhase.PROMPT_SETTLED, result.phase)

    def test_valid_split_coalesced_and_interleaved_updates(self):
        for scenario in ("valid", "split", "coalesced"):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario)
                self.assertIsNone(result.error_code)
                self.assertEqual(0, result.returncode)
                self.assertTrue(result.clean_exit)
                self.assertTrue(result.cleanup_complete)
                self.assertEqual("end_turn", result.stop_reason)
                self.assertEqual("", result.response_text)

        result = self.run_scenario("interleaved_update")
        self.assertIsNone(result.error_code)
        self.assertEqual("synthetic reply", result.response_text)
        self.assertEqual(
            hashlib.sha256(b"synthetic reply").hexdigest(),
            result.response_digest,
        )

    def test_permission_and_question_reverse_requests_are_cancelled(self):
        for scenario in ("permission", "question"):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario)
                self.assertIsNone(result.error_code)
                self.assertEqual("end_turn", result.stop_reason)
                self.assertEqual(PromptWriteBoundary.COMPLETE, result.prompt_write)

    def test_initialize_capability_and_protocol_failures_are_pre_prompt(self):
        cases = (
            ("missing_capability", "protocol_error"),
            ("protocol_mismatch", "protocol_error"),
            ("malformed_before", "protocol_error"),
            ("bad_utf8", "protocol_error"),
            ("duplicate_key", "protocol_error"),
            ("nonfinite", "protocol_error"),
            ("excess_depth", "protocol_error"),
            ("excess_items", "protocol_error"),
            ("oversized_before", "output_overflow"),
            ("eof", "protocol_error"),
            ("unterminated", "protocol_error"),
        )
        for scenario, code in cases:
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario, timeout=0.6)
                self.assertEqual(code, result.error_code)
                self.assertEqual(PromptWriteBoundary.NONE, result.prompt_write)
                self.assertEqual(AcpPhase.NEW, result.phase)
                self.assertNotIn(SECRET_MESSAGE, repr(result))

    def test_initialize_acp_023_full_optional_schema_is_strict(self):
        responses = lifecycle_responses(self.cwd)
        responses[0]["result"] = {
            "protocolVersion": 1,
            "agentInfo": {
                "name": "kimi",
                "version": "0.38.0",
                "title": None,
            },
            "authMethods": [
                {"id": "agent", "name": "Agent", "description": None},
                {
                    "type": "env_var",
                    "id": "env",
                    "name": "Environment",
                    "link": None,
                    "vars": [
                        {
                            "name": "TOKEN",
                            "label": None,
                            "optional": False,
                            "secret": True,
                        }
                    ],
                },
                {
                    "type": "terminal",
                    "id": "terminal",
                    "name": "Terminal",
                    "args": ["login"],
                    "env": {"TERM": "xterm"},
                },
            ],
            "agentCapabilities": {
                "loadSession": True,
                "auth": {"logout": {}},
                "mcpCapabilities": {"acp": False, "http": False, "sse": False},
                "nes": {
                    "context": {
                        "diagnostics": {},
                        "editHistory": {"maxCount": 10},
                        "openFiles": {},
                        "recentFiles": {"maxCount": None},
                        "relatedSnippets": {},
                        "userActions": {"maxCount": 5},
                    },
                    "events": {
                        "document": {
                            "didChange": {"syncKind": "incremental"},
                            "didClose": {},
                            "didFocus": {},
                            "didOpen": {},
                            "didSave": {},
                        }
                    },
                },
                "positionEncoding": "utf-8",
                "promptCapabilities": {
                    "audio": False,
                    "embeddedContext": False,
                    "image": False,
                },
                "providers": {},
                "sessionCapabilities": {
                    "additionalDirectories": {},
                    "close": {},
                    "delete": {},
                    "fork": {},
                    "list": {},
                    "resume": {},
                },
            },
        }
        result = run_kimi_acp(
            self.request,
            before_prompt=lambda _identity: None,
            process_factory=lambda argv, **kwargs: ScriptedProcess(
                argv,
                responses,
                **kwargs,
            ),
        )
        self.assertIsNone(result.error_code)

        invalid_mutations = (
            lambda value: value.update(protocolVersion=1.0),
            lambda value: value.update(protocolVersion=True),
            lambda value: value.update(authMethods=7),
            lambda value: value.update(
                authMethods=[
                    {
                        "type": "env_var",
                        "id": "env",
                        "name": "Environment",
                        "vars": {},
                    }
                ]
            ),
            lambda value: value.update(
                agentInfo={"name": "kimi", "version": 38}
            ),
            lambda value: value["agentCapabilities"].update(
                mcpCapabilities={"http": "yes"}
            ),
            lambda value: value["agentCapabilities"][
                "sessionCapabilities"
            ].update(close={"unexpected": True}),
        )
        for mutate in invalid_mutations:
            with self.subTest(mutation=repr(mutate)):
                invalid = lifecycle_responses(self.cwd)
                invalid[0]["result"] = json.loads(
                    json.dumps(responses[0]["result"])
                )
                mutate(invalid[0]["result"])
                outcome = run_kimi_acp(
                    self.request,
                    before_prompt=lambda _identity: None,
                    process_factory=lambda argv, **kwargs: ScriptedProcess(
                        argv,
                        invalid,
                        **kwargs,
                    ),
                )
                self.assertEqual("protocol_error", outcome.error_code)
                self.assertEqual(PromptWriteBoundary.NONE, outcome.prompt_write)

    def test_list_exact_id_and_filesystem_identity_precede_resume(self):
        result = self.run_scenario("list_absent")
        self.assertEqual("session_unavailable", result.error_code)
        self.assertEqual(AcpPhase.INITIALIZED, result.phase)
        self.assertEqual(PromptWriteBoundary.NONE, result.prompt_write)

        for scenario in ("list_duplicate", "list_cwd_mismatch"):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario)
                self.assertEqual("invalid_session", result.error_code)
                self.assertEqual(AcpPhase.INITIALIZED, result.phase)
                self.assertEqual(PromptWriteBoundary.NONE, result.prompt_write)

    def test_cwd_callback_failure_is_invalid_session(self):
        holder = {}

        def factory(argv, **kwargs):
            process = ScriptedProcess(
                argv,
                lifecycle_responses(self.cwd),
                **kwargs,
            )
            holder["process"] = process
            return process

        result = run_kimi_acp(
            self.request,
            before_prompt=lambda _identity: None,
            validate_session_cwd=lambda _value: False,
            process_factory=factory,
        )
        self.assertEqual("invalid_session", result.error_code)
        self.assertEqual(
            ["initialize", "session/list"],
            [value["method"] for value in holder["process"].writes],
        )

    def test_resume_and_mode_errors_never_write_prompt(self):
        for scenario, phase in (
            ("resume_error", AcpPhase.LISTED),
            ("set_mode_error", AcpPhase.RESUMED),
            ("set_mode_nonempty", AcpPhase.RESUMED),
        ):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario)
                self.assertEqual("protocol_error", result.error_code)
                self.assertEqual(phase, result.phase)
                self.assertEqual(PromptWriteBoundary.NONE, result.prompt_write)

    def test_before_prompt_revalidation_is_last_pre_write_gate(self):
        class Changed(Exception):
            code = "session_changed"

        holder = {}

        def factory(argv, **kwargs):
            process = ScriptedProcess(
                argv,
                lifecycle_responses(self.cwd),
                **kwargs,
            )
            holder["process"] = process
            return process

        result = run_kimi_acp(
            self.request,
            before_prompt=lambda _identity: (_ for _ in ()).throw(Changed()),
            process_factory=factory,
        )
        self.assertEqual("session_changed", result.error_code)
        self.assertEqual(AcpPhase.MODE_SET, result.phase)
        self.assertEqual(PromptWriteBoundary.NONE, result.prompt_write)
        self.assertNotIn(
            "session/prompt",
            [value.get("method") for value in holder["process"].writes],
        )

    def test_interrupts_from_revalidation_and_busy_proof_propagate(self):
        responses = lifecycle_responses(self.cwd)
        for exception in (KeyboardInterrupt(), SystemExit(19)):
            with self.subTest(source="before_prompt", exception=type(exception).__name__):
                process = ScriptedProcess(("/synthetic/kimi", "acp"), responses)
                with self.assertRaises(type(exception)) as raised:
                    run_kimi_acp(
                        self.request,
                        before_prompt=lambda _identity, error=exception: (
                            _ for _ in ()
                        ).throw(error),
                        process_factory=lambda _argv, **_kwargs: process,
                    )
                if isinstance(exception, SystemExit):
                    self.assertEqual(19, raised.exception.code)
                self.assertTrue(process.closed)

        busy = lifecycle_responses(self.cwd)
        busy[-1] = {
            "jsonrpc": "2.0",
            "id": 5,
            "error": {
                "code": -32000,
                "message": "busy",
                "data": {"code": "turn.agent_busy"},
            },
        }
        for exception in (KeyboardInterrupt(), SystemExit(23)):
            with self.subTest(source="busy_proof", exception=type(exception).__name__):
                process = ScriptedProcess(("/synthetic/kimi", "acp"), busy)
                with self.assertRaises(type(exception)) as raised:
                    run_kimi_acp(
                        self.request,
                        before_prompt=lambda _identity: None,
                        confirm_no_own_prompt=lambda error=exception: (
                            _ for _ in ()
                        ).throw(error),
                        process_factory=lambda _argv, **_kwargs: process,
                    )
                if isinstance(exception, SystemExit):
                    self.assertEqual(23, raised.exception.code)
                self.assertTrue(process.closed)

    def test_process_start_failures_map_to_public_error_codes(self):
        cases = (
            (BoundedDuplexLineProcessCancelledError("cancelled"), "cancelled"),
            (
                DescendantContainmentUnsupportedError("unsupported"),
                "containment_unsupported",
            ),
            (BoundedDuplexLineProcessError("transport"), "protocol_error"),
            (OSError("spawn"), "spawn_error"),
        )
        for failure, code in cases:
            with self.subTest(code=code):
                result = run_kimi_acp(
                    self.request,
                    before_prompt=lambda _identity: None,
                    process_factory=mock.Mock(side_effect=failure),
                )
                self.assertEqual(code, result.error_code)
                self.assertEqual(AcpPhase.NEW, result.phase)
                self.assertEqual(PromptWriteBoundary.NONE, result.prompt_write)
                self.assertFalse(result.cleanup_complete)

    def test_unknown_reverse_method_gets_method_not_found_then_fails_closed(self):
        result = self.run_scenario("unknown_reverse", timeout=0.8)
        self.assertEqual("protocol_error", result.error_code)
        self.assertEqual(PromptWriteBoundary.COMPLETE, result.prompt_write)
        self.assertFalse(result.definitive_busy_rejection)

    def test_malformed_oversized_and_output_overflow_after_prompt_are_unknown(self):
        cases = (
            ("malformed_after", "protocol_error"),
            ("oversized_after", "output_overflow"),
            ("aggregate_overflow", "output_overflow"),
            ("stderr_overflow", "output_overflow"),
        )
        for scenario, code in cases:
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario, timeout=1.0)
                self.assertEqual(code, result.error_code)
                self.assertEqual(PromptWriteBoundary.COMPLETE, result.prompt_write)
                self.assertNotIn(SECRET_MESSAGE, repr(result))

    def test_absolute_deadline_before_and_after_prompt(self):
        before = self.run_scenario("timeout_before", timeout=0.2)
        self.assertEqual("timeout", before.error_code)
        self.assertEqual(PromptWriteBoundary.NONE, before.prompt_write)

        started = time.monotonic()
        after = self.run_scenario("timeout_after_full", timeout=0.6)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual("timeout", after.error_code)
        self.assertEqual(PromptWriteBoundary.COMPLETE, after.prompt_write)
        self.assertTrue(after.cleanup_complete)

        late = self.run_scenario("late_settlement", timeout=0.6)
        self.assertEqual(PromptWriteBoundary.COMPLETE, late.prompt_write)
        self.assertIn(late.error_code, ("timeout", "native_exit"))

    def test_partial_prompt_write_sends_cancel_and_is_conservative(self):
        holder = {}

        def factory(argv, **kwargs):
            process = ScriptedProcess(
                argv,
                lifecycle_responses(self.cwd)[:-1],
                partial_prompt=True,
                **kwargs,
            )
            holder["process"] = process
            return process

        result = run_kimi_acp(
            self.request,
            before_prompt=lambda _identity: None,
            process_factory=factory,
        )
        writes = holder["process"].writes
        self.assertEqual(PromptWriteBoundary.PARTIAL, result.prompt_write)
        self.assertEqual("timeout", result.error_code)
        self.assertEqual("session/cancel", writes[-1]["method"])
        self.assertEqual(
            {"sessionId": self.request.session_id},
            writes[-1]["params"],
        )

    def test_typed_partial_prompt_exception_preserves_boundary_and_cancels(self):
        holder = {}

        def factory(argv, **kwargs):
            process = ScriptedProcess(
                argv,
                lifecycle_responses(self.cwd)[:-1],
                prompt_error_boundary=DuplexWriteBoundary.PARTIAL,
                **kwargs,
            )
            holder["process"] = process
            return process

        result = run_kimi_acp(
            self.request,
            before_prompt=lambda _identity: None,
            process_factory=factory,
        )
        self.assertEqual(PromptWriteBoundary.PARTIAL, result.prompt_write)
        self.assertEqual("timeout", result.error_code)
        self.assertEqual(AcpPhase.MODE_SET, result.phase)
        self.assertEqual("session/cancel", holder["process"].writes[-1]["method"])

    def test_typed_complete_prompt_exception_preserves_boundary_and_cancels(self):
        holder = {}

        def factory(argv, **kwargs):
            process = ScriptedProcess(
                argv,
                lifecycle_responses(self.cwd)[:-1],
                prompt_error_boundary=DuplexWriteBoundary.COMPLETE,
                **kwargs,
            )
            holder["process"] = process
            return process

        result = run_kimi_acp(
            self.request,
            before_prompt=lambda _identity: None,
            process_factory=factory,
        )
        self.assertEqual(PromptWriteBoundary.COMPLETE, result.prompt_write)
        self.assertEqual("timeout", result.error_code)
        self.assertEqual(AcpPhase.PROMPT_WRITTEN, result.phase)
        self.assertEqual("session/cancel", holder["process"].writes[-1]["method"])

    def test_base_signal_prompt_exception_preserves_boundary_cancels_and_reraises(self):
        cases = (
            (KeyboardInterrupt(), DuplexWriteBoundary.PARTIAL, None),
            (SystemExit(42), DuplexWriteBoundary.COMPLETE, KeyboardInterrupt()),
        )
        for exception, boundary, cleanup_exception in cases:
            with self.subTest(exception=type(exception).__name__, boundary=boundary):
                holder = {}

                def factory(argv, **kwargs):
                    process = ScriptedProcess(
                        argv,
                        lifecycle_responses(self.cwd)[:-1],
                        prompt_base_exception=exception,
                        prompt_base_boundary=boundary,
                        cancel_base_exception=cleanup_exception,
                        **kwargs,
                    )
                    holder["process"] = process
                    return process

                with self.assertRaises(type(exception)) as raised:
                    run_kimi_acp(
                        self.request,
                        before_prompt=lambda _identity: None,
                        process_factory=factory,
                    )
                if isinstance(exception, SystemExit):
                    self.assertEqual(42, raised.exception.code)
                writes = holder["process"].writes
                self.assertEqual("session/prompt", writes[-2]["method"])
                self.assertEqual("session/cancel", writes[-1]["method"])
                self.assertEqual(
                    {"sessionId": self.request.session_id},
                    writes[-1]["params"],
                )

    def test_signal_windows_compare_prompt_write_snapshot_in_finally(self):
        cases = (
            (
                "before_record",
                KeyboardInterrupt(),
                DuplexWriteBoundary.PARTIAL,
                PromptWriteBoundary.NONE,
                False,
            ),
            (
                "after_record",
                SystemExit(23),
                DuplexWriteBoundary.PARTIAL,
                PromptWriteBoundary.PARTIAL,
                True,
            ),
            (
                "after_return",
                KeyboardInterrupt(),
                DuplexWriteBoundary.COMPLETE,
                PromptWriteBoundary.COMPLETE,
                True,
            ),
        )
        for window, exception, write_boundary, expected, should_cancel in cases:
            with self.subTest(window=window):
                holder = {}

                def factory(argv, **kwargs):
                    process = ScriptedProcess(
                        argv,
                        lifecycle_responses(self.cwd)[:-1],
                        prompt_base_exception=exception,
                        prompt_base_boundary=write_boundary,
                        prompt_signal_window=window,
                        **kwargs,
                    )
                    holder["process"] = process
                    return process

                with self.assertRaises(type(exception)):
                    run_kimi_acp(
                        self.request,
                        before_prompt=lambda _identity: None,
                        process_factory=factory,
                    )
                methods = [
                    write.get("method") for write in holder["process"].writes
                ]
                self.assertEqual(
                    should_cancel,
                    "session/cancel" in methods,
                )
                last_result = holder["process"].write_result
                if should_cancel:
                    self.assertEqual("session/cancel", methods[-1])
                    self.assertEqual(
                        expected.value,
                        holder["process"].boundary_before_cancel.value,
                    )
                else:
                    self.assertIsNotNone(last_result)
                    self.assertEqual(
                        DuplexWriteBoundary.COMPLETE,
                        last_result.boundary,
                    )
                if expected is PromptWriteBoundary.NONE:
                    self.assertNotIn("session/cancel", methods)

    def test_overlong_deadline_is_rejected_before_process_factory(self):
        request = dataclasses.replace(
            self.request,
            deadline=time.monotonic() + MAX_TIMEOUT_SECONDS + 10,
        )
        factory = mock.Mock()
        with self.assertRaisesRegex(ValueError, "maximum operation interval"):
            run_kimi_acp(
                request,
                before_prompt=lambda _identity: None,
                process_factory=factory,
            )
        factory.assert_not_called()

        overflow = dataclasses.replace(self.request, deadline=10**10000)
        overflow_factory = mock.Mock()
        with self.assertRaisesRegex(ValueError, "finite"):
            run_kimi_acp(
                overflow,
                before_prompt=lambda _identity: None,
                process_factory=overflow_factory,
            )
        overflow_factory.assert_not_called()

    def test_deadline_is_strictly_typed_normalized_and_reused(self):
        holder = {}

        def factory(argv, **kwargs):
            process = ScriptedProcess(
                argv,
                lifecycle_responses(self.cwd),
                **kwargs,
            )
            holder["process"] = process
            return process

        request = dataclasses.replace(self.request, deadline=20)
        result = run_kimi_acp(
            request,
            before_prompt=lambda _identity: None,
            process_factory=factory,
            monotonic=lambda: 10.0,
        )
        self.assertIsNone(result.error_code)
        self.assertEqual([20.0] * 5, holder["process"].deadlines)
        self.assertTrue(
            all(type(deadline) is float for deadline in holder["process"].deadlines)
        )

    def test_prompt_frame_bounds_are_validated_before_spawn_or_callback(self):
        request = dataclasses.replace(
            self.request,
            message=b"x" * ACP_LINE_BYTES,
        )
        factory = mock.Mock()
        before_prompt = mock.Mock()
        with self.assertRaisesRegex(ValueError, "prompt frame"):
            run_kimi_acp(
                request,
                before_prompt=before_prompt,
                process_factory=factory,
            )
        factory.assert_not_called()
        before_prompt.assert_not_called()

    def test_all_acp_023_session_update_shapes_are_validated(self):
        updates = [
            {
                "sessionUpdate": kind,
                "content": {"type": "text", "text": text},
            }
            for kind, text in (
                ("user_message_chunk", "user"),
                ("agent_message_chunk", "answer"),
                ("agent_thought_chunk", "thought"),
            )
        ]
        updates.extend(
            [
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {
                        "type": "image",
                        "data": "aW1hZ2U=",
                        "mimeType": "image/png",
                        "uri": None,
                        "annotations": {
                            "audience": ["assistant"],
                            "lastModified": None,
                            "priority": 0.5,
                        },
                    },
                },
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {
                        "type": "audio",
                        "data": "YXVkaW8=",
                        "mimeType": "audio/wav",
                    },
                },
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {
                        "type": "resource_link",
                        "name": "source",
                        "uri": "file:///tmp/source",
                        "description": None,
                        "mimeType": "text/plain",
                        "size": 12,
                        "title": "Source",
                    },
                },
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {
                        "type": "resource",
                        "resource": {
                            "uri": "file:///tmp/text",
                            "text": "embedded",
                            "mimeType": None,
                        },
                    },
                },
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {
                        "type": "resource",
                        "resource": {
                            "uri": "file:///tmp/blob",
                            "blob": "YmxvYg==",
                        },
                    },
                },
            ]
        )
        updates.extend(
            [
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tool-1",
                    "title": "Read",
                    "kind": "read",
                    "status": "pending",
                    "locations": [{"path": "/tmp/example", "line": 0}],
                    "content": [
                        {
                            "type": "content",
                            "content": {"type": "text", "text": "progress"},
                        },
                        {
                            "type": "diff",
                            "path": "/tmp/example",
                            "oldText": None,
                            "newText": "new",
                        },
                        {"type": "terminal", "terminalId": "terminal-1"},
                    ],
                },
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tool-optional-absent",
                    "title": "Minimal",
                },
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tool-1",
                    "kind": None,
                    "status": None,
                    "title": None,
                    "content": None,
                    "locations": [{"path": "/tmp/example", "line": None}],
                },
                {
                    "sessionUpdate": "plan",
                    "entries": [
                        {
                            "content": "work",
                            "priority": "high",
                            "status": "in_progress",
                        }
                    ],
                },
                {
                    "sessionUpdate": "plan_update",
                    "plan": {
                        "type": "markdown",
                        "id": "plan-1",
                        "content": "# Plan",
                    },
                },
                {
                    "sessionUpdate": "plan_update",
                    "plan": {
                        "type": "items",
                        "id": "plan-2",
                        "entries": [],
                    },
                },
                {
                    "sessionUpdate": "plan_update",
                    "plan": {
                        "type": "file",
                        "id": "plan-3",
                        "uri": "file:///tmp/plan.md",
                    },
                },
                {"sessionUpdate": "plan_removed", "id": "plan-1"},
                {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": [
                        {
                            "name": "help",
                            "description": "Help",
                            "input": {"hint": "topic"},
                        }
                    ],
                },
                {
                    "sessionUpdate": "current_mode_update",
                    "currentModeId": "default",
                },
                {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [
                        {
                            "type": "boolean",
                            "id": "plan",
                            "name": "Plan",
                            "currentValue": False,
                            "category": "mode",
                        },
                        {
                            "type": "select",
                            "id": "model",
                            "name": "Model",
                            "currentValue": "m1",
                            "options": [{"name": "M1", "value": "m1"}],
                        },
                        {
                            "type": "select",
                            "id": "grouped",
                            "name": "Grouped",
                            "currentValue": "g1",
                            "options": [
                                {
                                    "group": "models",
                                    "name": "Models",
                                    "options": [
                                        {
                                            "name": "G1",
                                            "value": "g1",
                                            "description": None,
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
                {
                    "sessionUpdate": "session_info_update",
                    "title": None,
                    "updatedAt": "2026-08-26T00:00:00Z",
                },
                {
                    "sessionUpdate": "usage_update",
                    "size": 100,
                    "used": 10,
                    "cost": {"amount": 0.1, "currency": "USD"},
                },
            ]
        )
        responses = lifecycle_responses(
            self.cwd,
            prompt_result={
                "stopReason": "end_turn",
                "usage": {
                    "inputTokens": 1,
                    "outputTokens": 2,
                    "totalTokens": 3,
                },
                "userMessageId": "user-1",
            },
        )
        responses[1]["result"].update({"nextCursor": None})
        responses[1]["result"]["sessions"][0].update(
            {
                "additionalDirectories": [],
                "title": "Synthetic",
                "updatedAt": None,
            }
        )
        responses[2]["result"] = {
            "configOptions": [],
            "models": {
                "availableModels": [
                    {"modelId": "m1", "name": "Model 1", "description": None}
                ],
                "currentModelId": "m1",
            },
            "modes": {
                "availableModes": [{"id": "default", "name": "Default"}],
                "currentModeId": "default",
            },
        }
        responses[4:4] = [
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": self.request.session_id,
                    "update": update,
                },
            }
            for update in updates
        ]

        result = run_kimi_acp(
            self.request,
            before_prompt=lambda _identity: None,
            process_factory=lambda argv, **kwargs: ScriptedProcess(
                argv,
                responses,
                **kwargs,
            ),
        )
        self.assertIsNone(result.error_code)
        self.assertEqual("answer", result.response_text)

    def test_invalid_acp_023_schema_fields_fail_closed(self):
        invalid_updates = (
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text"}},
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-1",
                "title": "Read",
                "status": "unknown",
            },
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-null-kind",
                "title": "Read",
                "kind": None,
            },
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-null-status",
                "title": "Read",
                "status": None,
            },
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-shaped-kind",
                "title": "Read",
                "kind": ["read"],
            },
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-null-content",
                "title": "Read",
                "content": None,
            },
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-null-locations",
                "title": "Read",
                "locations": None,
            },
            {"sessionUpdate": "tool_call_update", "status": "completed"},
            {
                "sessionUpdate": "plan",
                "entries": [
                    {
                        "content": "work",
                        "priority": "urgent",
                        "status": "pending",
                    }
                ],
            },
            {
                "sessionUpdate": "config_option_update",
                "configOptions": [
                    {
                        "type": "boolean",
                        "id": "plan",
                        "name": "Plan",
                        "currentValue": "false",
                    }
                ],
            },
            {"sessionUpdate": "usage_update", "size": True, "used": 1},
            {
                "sessionUpdate": "current_mode_update",
                "currentModeId": "default",
                "unexpected": True,
            },
        )
        for update in invalid_updates:
            with self.subTest(kind=update.get("sessionUpdate")):
                responses = lifecycle_responses(self.cwd)
                responses.insert(
                    4,
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": self.request.session_id,
                            "update": update,
                        },
                    },
                )
                result = run_kimi_acp(
                    self.request,
                    before_prompt=lambda _identity: None,
                    process_factory=lambda argv, **kwargs: ScriptedProcess(
                        argv,
                        responses,
                        **kwargs,
                    ),
                )
                self.assertEqual("protocol_error", result.error_code)
                self.assertEqual(
                    PromptWriteBoundary.COMPLETE,
                    result.prompt_write,
                )

    def test_invalid_list_resume_prompt_and_permission_option_schema(self):
        cases = []

        bad_list = lifecycle_responses(self.cwd)
        bad_list[1]["result"]["nextCursor"] = 7
        cases.append((bad_list, PromptWriteBoundary.NONE, AcpPhase.INITIALIZED))

        bad_resume = lifecycle_responses(self.cwd)
        bad_resume[2]["result"] = {"modes": {"availableModes": "default"}}
        cases.append((bad_resume, PromptWriteBoundary.NONE, AcpPhase.LISTED))

        bad_prompt = lifecycle_responses(
            self.cwd,
            prompt_result={
                "stopReason": "end_turn",
                "usage": {"inputTokens": 1, "outputTokens": 2},
            },
        )
        cases.append(
            (bad_prompt, PromptWriteBoundary.COMPLETE, AcpPhase.PROMPT_WRITTEN)
        )

        bad_permission = lifecycle_responses(self.cwd)
        bad_permission.insert(
            4,
            {
                "jsonrpc": "2.0",
                "id": "permission-1",
                "method": "session/request_permission",
                "params": {
                    "sessionId": self.request.session_id,
                    "toolCall": {"toolCallId": "tool-1"},
                    "options": [
                        {
                            "optionId": "reject",
                            "name": "Reject",
                            "kind": "sometimes",
                        }
                    ],
                },
            },
        )
        cases.append(
            (bad_permission, PromptWriteBoundary.COMPLETE, AcpPhase.PROMPT_WRITTEN)
        )

        for responses, boundary, phase in cases:
            with self.subTest(boundary=boundary, phase=phase):
                result = run_kimi_acp(
                    self.request,
                    before_prompt=lambda _identity: None,
                    process_factory=lambda argv, **kwargs: ScriptedProcess(
                        argv,
                        responses,
                        **kwargs,
                    ),
                )
                self.assertEqual("protocol_error", result.error_code)
                self.assertEqual(boundary, result.prompt_write)
                self.assertEqual(phase, result.phase)

    def test_structured_busy_requires_external_no_own_prompt_proof(self):
        unknown = self.run_scenario("busy")
        self.assertEqual("session_busy", unknown.error_code)
        self.assertEqual(PromptWriteBoundary.COMPLETE, unknown.prompt_write)
        self.assertFalse(unknown.definitive_busy_rejection)
        self.assertFalse(unknown.own_turn_started)

        rejected = self.run_scenario(
            "busy",
            confirm_no_own_prompt=lambda: True,
        )
        self.assertEqual("session_busy", rejected.error_code)
        self.assertTrue(rejected.definitive_busy_rejection)

        uncertain = self.run_scenario(
            "busy",
            confirm_no_own_prompt=lambda: False,
        )
        self.assertFalse(uncertain.definitive_busy_rejection)

    def test_json_rpc_error_code_must_be_int32_before_busy_classification(self):
        for code in (-2147483649, 2147483648):
            with self.subTest(code=code):
                responses = lifecycle_responses(self.cwd)
                responses[-1] = {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "error": {
                        "code": code,
                        "message": "busy",
                        "data": {"code": "turn.agent_busy"},
                    },
                }
                proof = mock.Mock(return_value=True)
                result = run_kimi_acp(
                    self.request,
                    before_prompt=lambda _identity: None,
                    confirm_no_own_prompt=proof,
                    process_factory=lambda argv, **kwargs: ScriptedProcess(
                        argv,
                        responses,
                        **kwargs,
                    ),
                )
                self.assertEqual("protocol_error", result.error_code)
                self.assertFalse(result.definitive_busy_rejection)
                proof.assert_not_called()

    def test_unknown_and_duplicate_response_ids_fail_closed(self):
        for scenario in ("unknown_response", "duplicate_response"):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario, timeout=0.8)
                self.assertEqual("protocol_error", result.error_code)
                self.assertEqual(PromptWriteBoundary.COMPLETE, result.prompt_write)

    def test_stop_reason_is_collected_but_never_claimed_as_delivery_proof(self):
        for scenario, reason in (
            ("valid", "end_turn"),
            ("clean_cancelled", "cancelled"),
            ("clean_refusal", "refusal"),
            ("durable_failed", "end_turn"),
            ("durable_cancelled", "end_turn"),
            ("durable_blocked", "end_turn"),
            ("missing_durable_end", "end_turn"),
            ("wire_replacement", "end_turn"),
            ("wire_truncation", "end_turn"),
            ("wire_prefix_mutation", "end_turn"),
            ("state_conflict", "end_turn"),
        ):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario)
                self.assertEqual(reason, result.stop_reason)
                self.assertFalse(hasattr(result, "delivery"))
                self.assertFalse(hasattr(result, "durable_proof"))

    def test_fixture_artifacts_contain_only_digest_not_prompt(self):
        artifact_dir = Path(self.temporary.name) / "synthetic-home"
        result = self.run_scenario(
            "wire_prefix_mutation",
            artifact_dir=artifact_dir,
        )
        self.assertIsNone(result.error_code)
        contents = b"".join(
            path.read_bytes() for path in sorted(artifact_dir.iterdir())
        )
        self.assertNotIn(self.request.message, contents)
        self.assertIn(
            hashlib.sha256(self.request.message).hexdigest().encode("ascii"),
            contents,
        )

    @unittest.skipUnless(os.name == "posix", "process cleanup requires POSIX")
    def test_lingering_children_are_conservative_and_reaped(self):
        for scenario in ("child_lingers", "forks"):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(scenario, timeout=0.5)
                self.assertEqual(PromptWriteBoundary.COMPLETE, result.prompt_write)
                self.assertIsNotNone(result.error_code)

    @unittest.skipUnless(
        sys.platform == "darwin",
        "Darwin kqueue containment proof required",
    )
    def test_exited_and_escaped_children_never_become_clean_success(self):
        for scenario in ("child_exits", "child_escapes"):
            with self.subTest(scenario=scenario):
                result = self.run_scenario(
                    scenario,
                    timeout=0.5,
                    process_factory=BoundedDuplexLineProcess,
                )
                self.assertEqual(PromptWriteBoundary.COMPLETE, result.prompt_write)
                self.assertEqual("cleanup_incomplete", result.error_code)
                self.assertFalse(result.cleanup_complete)

    def test_hard_limits_and_request_validation_precede_spawn(self):
        invalid = (
            None,
            dataclasses.replace(self.request, session_id=""),
            dataclasses.replace(self.request, executable="kimi"),
            dataclasses.replace(self.request, cwd=self.cwd + "/../other"),
            dataclasses.replace(self.request, message="not-bytes"),
            dataclasses.replace(self.request, message=b"\xff"),
            dataclasses.replace(self.request, message=b" "),
            dataclasses.replace(self.request, deadline=True),
            dataclasses.replace(self.request, deadline="123"),
            dataclasses.replace(self.request, deadline=object()),
            dataclasses.replace(self.request, deadline=float("inf")),
            dataclasses.replace(self.request, deadline=time.monotonic() - 1),
        )
        for request in invalid:
            with self.subTest(request=repr(request)):
                with mock.patch(
                    "sidecar.kimi_acp.BoundedDuplexLineProcess"
                ) as factory:
                    with self.assertRaises((TypeError, ValueError)):
                        run_kimi_acp(
                            request,
                            before_prompt=lambda _identity: None,
                        )
                    factory.assert_not_called()
        self.assertEqual(256 * 1024, ACP_LINE_BYTES)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.request.cwd = self.cwd

    def test_callback_and_factory_contracts_are_validated_before_spawn(self):
        cases = (
            {"before_prompt": None},
            {
                "before_prompt": lambda _identity: None,
                "validate_session_cwd": object(),
            },
            {
                "before_prompt": lambda _identity: None,
                "confirm_no_own_prompt": object(),
            },
            {
                "before_prompt": lambda _identity: None,
                "process_factory": object(),
            },
            {
                "before_prompt": lambda _identity: None,
                "monotonic": object(),
            },
        )
        for kwargs in cases:
            with self.subTest(kwargs=tuple(kwargs)):
                with self.assertRaises(TypeError):
                    run_kimi_acp(self.request, **kwargs)

    def test_fixture_is_owner_executable_and_contains_no_model_client(self):
        mode = FAKE_ACP.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)
        source = FAKE_ACP.read_text(encoding="utf-8")
        for forbidden in ("openai", "anthropic", "moonshot", "requests", "httpx"):
            self.assertNotIn(forbidden, source.casefold())


if __name__ == "__main__":
    unittest.main()
