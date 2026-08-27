import hashlib
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import sidecar.daemon as daemon
import sidecar.bus as bus
import sidecar.index as index
import sidecar.inject as inject
import sidecar.kimi_acp as acp
import sidecar.kimi_identity as kimi_identity
import sidecar.json_limits as json_limits
import sidecar.daemon_log as daemon_log
import sidecar.process as process
import sidecar.remote_types as remote_types
import sidecar.remote as remote
import sidecar.remote_watch_types as remote_watch_types
import sidecar.send_audit as audit
import sidecar.scan as scan
import sidecar.state as state
import sidecar.tail as tail
import sidecar.tailer_pool as tailer_pool
import sidecar.text_utils as text_utils


def assert_rejected(test_case, function, *args, **kwargs):
    with test_case.assertRaises(Exception):
        function(*args, **kwargs)


class SendAuditCoverageMatrixTests(unittest.TestCase):
    def setUp(self):
        self.epoch = "e_" + ("a" * 32)
        self.hash_value = "b" * 64
        self.pending = {
            "schema_version": 1,
            "timestamp": 1.0,
            "request_id": "request-matrix",
            "namespace_epoch": self.epoch,
            "agent": "claude",
            "target_hmac": self.hash_value,
            "request_hmac": self.hash_value,
            "executable_basename": "claude",
            "confirmation_mode": "allow_write",
            "outcome": "pending",
        }

    def test_hash_and_record_validation_matrix(self):
        for value in (None, False, True, 0, -4, 1.5, "text", b"bytes", (), [1, "two"]):
            digest = hashlib.sha256()
            audit._hash_value(digest, value)
            self.assertTrue(digest.hexdigest())
        with self.assertRaises(audit.AuditError):
            audit._hash_value(hashlib.sha256(), object())
        with self.assertRaises(audit.AuditError):
            audit._hash_value(hashlib.sha256(), [[[[[[[[[1]]]]]]]]])

        self.assertEqual(self.pending, audit._validate_record(self.pending))
        for outcome in ("failed", "timed_out", "overflow"):
            terminal = dict(self.pending)
            terminal.update(
                {
                    "outcome": outcome,
                    "delivery": "unknown",
                    "error": None,
                    "returncode": None,
                }
            )
            self.assertEqual(terminal, audit._validate_record(terminal))

        malformed = (
            {"outcome": "pending"},
            dict(self.pending, schema_version=True),
            dict(self.pending, timestamp=float("inf")),
            dict(self.pending, request_id="bad id"),
            dict(self.pending, namespace_epoch="wrong"),
            dict(self.pending, agent="Claude"),
            dict(self.pending, target_hmac="wrong"),
            dict(self.pending, executable_basename=""),
            dict(self.pending, confirmation_mode="confirm"),
            dict(
                self.pending,
                outcome="completed",
                delivery="unknown",
                error=None,
                returncode=0,
            ),
            dict(
                self.pending,
                outcome="failed",
                delivery="delivered",
                error=None,
                returncode=0,
            ),
            dict(
                self.pending,
                outcome="failed",
                delivery="unknown",
                error="bad error",
                returncode=None,
            ),
            dict(
                self.pending,
                outcome="failed",
                delivery="unknown",
                error=None,
                returncode=True,
            ),
        )
        for value in malformed:
            with self.subTest(value=value):
                assert_rejected(self, audit._validate_record, value)

    def test_history_marker_and_descriptor_boundaries(self):
        terminal = dict(
            self.pending,
            outcome="failed",
            delivery="unknown",
            error=None,
            returncode=1,
        )
        assert_rejected(self, audit._validate_histories, [terminal])
        assert_rejected(
            self,
            audit._validate_histories,
            [self.pending, dict(self.pending, timestamp=2.0)],
        )

        marker = {
            "schema_version": 1,
            "namespace_epoch": self.epoch,
            "key_fingerprint": self.hash_value,
            "runtime_dev": 1,
            "runtime_ino": 2,
            "key_dev": 3,
            "key_ino": 4,
        }
        payload = (
            json.dumps(marker, separators=(",", ":"), sort_keys=True).encode("ascii")
            + b"\n"
        )
        self.assertEqual(marker, audit._parse_marker(payload))
        for bad in (b"", b"{}\n", b"not-json\n", payload[:-1]):
            with self.subTest(bad=bad):
                assert_rejected(self, audit._parse_marker, bad)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "descriptor"
            path.write_bytes(b"x")
            descriptor = os.open(str(path), os.O_RDONLY)
            try:
                details = os.fstat(descriptor)
                self.assertEqual(b"x", audit._read_descriptor(descriptor, 8)[0])
                changed = mock.Mock(
                    st_dev=details.st_dev,
                    st_ino=details.st_ino,
                    st_mode=details.st_mode,
                    st_uid=details.st_uid,
                    st_size=2,
                    st_mtime_ns=details.st_mtime_ns,
                    st_ctime_ns=details.st_ctime_ns,
                )
                with mock.patch.object(
                    audit.os, "fstat", side_effect=[changed, changed]
                ):
                    assert_rejected(self, audit._read_descriptor, descriptor, 8)
            finally:
                os.close(descriptor)

    def test_identity_and_filesystem_error_boundaries(self):
        identity = audit.make_audit_identity(
            agent="claude",
            session_id="session",
            project="/project",
            executable_basename="claude",
            confirmation_mode="allow_write",
            message=b"message",
        )
        self.assertEqual("claude", identity.agent)
        for values in (
            dict(agent="Claude"),
            dict(session_id=""),
            dict(project=""),
            dict(executable_basename="bad/name"),
            dict(confirmation_mode="no"),
            dict(message="text"),
        ):
            kwargs = {
                "agent": "claude",
                "session_id": "session",
                "project": "/project",
                "executable_basename": "claude",
                "confirmation_mode": "allow_write",
                "message": b"message",
            }
            kwargs.update(values)
            with self.subTest(values=values):
                assert_rejected(self, audit.make_audit_identity, **kwargs)
        self.assertEqual(
            "unsafe_audit",
            audit._translate_filesystem_error(
                OSError(audit.errno.ELOOP, "loop")
            ).code,
        )
        self.assertEqual(
            "audit_error",
            audit._translate_filesystem_error(OSError()).code,
        )

    def test_namespace_close_and_closed_operation_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir(mode=0o700)
            with mock.patch(
                "sidecar.send_audit.pwd.getpwuid",
                return_value=mock.Mock(pw_dir=str(home)),
            ):
                store = audit.SendAuditStore(root / "runtime")
                context = store.open_namespace()
                namespace = context.__enter__()
                try:
                    namespace.close()
                    namespace.close()
                    for operation in (
                        namespace.validate,
                        namespace.downgrade_marker,
                    ):
                        assert_rejected(self, operation)
                finally:
                    context.__exit__(None, None, None)


class KimiAcpCoverageMatrixTests(unittest.TestCase):
    def test_primitive_protocol_validators_cover_all_shapes(self):
        self.assertEqual(
            acp.PromptWriteBoundary.COMPLETE,
            acp._prompt_boundary(acp.DuplexWriteBoundary.COMPLETE),
        )
        self.assertEqual(
            acp.PromptWriteBoundary.PARTIAL,
            acp._prompt_boundary(acp.DuplexWriteBoundary.PARTIAL),
        )
        self.assertEqual(
            acp.PromptWriteBoundary.NONE,
            acp._prompt_boundary(acp.DuplexWriteBoundary.NONE),
        )
        for value in (
            {"value": float("nan")},
            {"value": "\ud800"},
            {"value": object()},
        ):
            assert_rejected(self, acp._encode_frame, value)
        assert_rejected(self, acp._mapping, [])
        assert_rejected(self, acp._rpc_id, True)
        self.assertTrue(acp._valid_meta_only({}))
        self.assertTrue(acp._valid_meta_only({"_meta": None}))
        self.assertFalse(acp._valid_meta_only({"unexpected": True}))
        self.assertTrue(acp._error_is_busy({"data": {"code": "turn.agent_busy"}}))
        self.assertTrue(
            acp._error_is_busy({"data": {"error": {"code": "turn.agent_busy"}}})
        )
        self.assertFalse(acp._error_is_busy({"data": {}}))

        for annotations in (
            {},
            {"audience": ["assistant", "user"], "priority": 1},
            {"lastModified": None},
        ):
            acp._validate_annotations(annotations)
        for bad in ([], {"audience": ["system"]}, {"priority": "high"}):
            assert_rejected(self, acp._validate_annotations, bad)

        content_values = (
            {"type": "text", "text": "text"},
            {"type": "image", "data": "data", "mimeType": "image/png"},
            {"type": "audio", "data": "data", "mimeType": "audio/wav"},
            {"type": "resource_link", "name": "name", "uri": "uri", "size": 1},
            {
                "type": "resource",
                "resource": {"uri": "uri", "text": "text", "mimeType": "text/plain"},
            },
            {"type": "resource", "resource": {"uri": "uri", "blob": "blob"}},
        )
        for value in content_values:
            acp._validate_content_block(value)
        for bad in (
            {},
            {"type": "unknown"},
            {"type": "resource", "resource": {"uri": "uri", "text": "x", "blob": "y"}},
            {"type": "text", "text": 1},
        ):
            assert_rejected(self, acp._validate_content_block, bad)

        acp._validate_tool_location({"path": "/tmp/file", "line": 0})
        for bad in ({}, {"path": "/tmp/file", "line": -1}, {"path": "/tmp/file", "line": True}):
            assert_rejected(self, acp._validate_tool_location, bad)
        for value in (
            {"type": "content", "content": {"type": "text", "text": "x"}},
            {"type": "diff", "newText": "new", "path": "file", "oldText": None},
            {"type": "terminal", "terminalId": "terminal"},
        ):
            acp._validate_tool_content(value)
        assert_rejected(self, acp._validate_tool_content, {"type": "unknown"})

    def test_plan_config_mode_model_and_update_shapes(self):
        priority = next(iter(acp._PLAN_PRIORITIES))
        status = next(iter(acp._PLAN_STATUSES))
        acp._validate_plan_entries(
            [{"content": "content", "priority": priority, "status": status}]
        )
        assert_rejected(self, acp._validate_plan_entries, {})
        for plan in (
            {"type": "items", "id": "plan", "entries": []},
            {"type": "file", "id": "plan", "uri": "file:///plan"},
            {"type": "markdown", "id": "plan", "content": "plan"},
        ):
            acp._validate_plan_update({"plan": plan})
        assert_rejected(self, acp._validate_plan_update, {"plan": {"type": "other"}})

        acp._validate_config_option(
            {"type": "boolean", "currentValue": True, "id": "flag", "name": "Flag"}
        )
        acp._validate_config_option(
            {
                "type": "select",
                "currentValue": "one",
                "id": "select",
                "name": "Select",
                "options": [{"name": "One", "value": "one"}],
            }
        )
        acp._validate_config_option(
            {
                "type": "select",
                "currentValue": "one",
                "id": "grouped",
                "name": "Grouped",
                "options": [
                    {
                        "group": "Group",
                        "name": "Group",
                        "options": [{"name": "One", "value": "one"}],
                    }
                ],
            }
        )
        for bad in (
            {"type": "other"},
            {"type": "boolean", "currentValue": "true", "id": "x", "name": "X"},
            {
                "type": "select",
                "currentValue": "x",
                "id": "x",
                "name": "X",
                "options": [{}],
            },
        ):
            assert_rejected(self, acp._validate_config_option, bad)

        acp._validate_mode_state({"availableModes": [], "currentModeId": "default"})
        acp._validate_model_state({"availableModels": [], "currentModelId": "default"})
        for bad in (
            {"availableModes": {}, "currentModeId": "default"},
            {"availableModels": {}, "currentModelId": "default"},
        ):
            assert_rejected(
                self,
                acp._validate_mode_state if "availableModes" in bad else acp._validate_model_state,
                bad,
            )

        for update in (
            {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "x"}},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "x"}},
            {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "x"}},
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call",
                "title": "title",
                "kind": "execute",
                "content": [],
                "locations": [],
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call",
                "title": None,
                "kind": None,
                "status": None,
                "content": None,
                "locations": None,
            },
            {"sessionUpdate": "plan", "entries": []},
            {
                "sessionUpdate": "plan_update",
                "plan": {"type": "items", "id": "plan", "entries": []},
            },
            {"sessionUpdate": "plan_removed", "id": "plan"},
            {"sessionUpdate": "current_mode_update", "currentModeId": "default"},
            {"sessionUpdate": "config_option_update", "configOptions": []},
            {"sessionUpdate": "session_info_update"},
            {"sessionUpdate": "session_info_update", "title": None, "updatedAt": None},
            {"sessionUpdate": "usage_update", "size": 1, "used": 1},
            {
                "sessionUpdate": "usage_update",
                "size": 1.5,
                "used": 2,
                "cost": {"amount": 0.1, "currency": "USD"},
            },
        ):
            acp._validate_update(update)
        assert_rejected(self, acp._validate_update, {"sessionUpdate": "unknown"})


class KimiIdentityCoverageMatrixTests(unittest.TestCase):
    def test_path_and_json_validation_boundaries(self):
        self.assertEqual(
            ("child", "file"),
            kimi_identity._contained_components("/root", "child/file"),
        )
        self.assertEqual(
            ("child",),
            kimi_identity._contained_components("/root", "/root/child"),
        )
        for value in ("", "\x00", "../outside", "/outside", "."):
            with self.subTest(value=value):
                assert_rejected(
                    self, kimi_identity._contained_components, "/root", value
                )
        assert_rejected(
            self,
            kimi_identity._contained_components,
            "/root",
            "/root/" + "/".join("part" + str(i) for i in range(65)),
        )
        for value in ("", "not-an-int", "9" * 300):
            assert_rejected(self, kimi_identity._parse_integer, value)
        self.assertEqual(4, kimi_identity._parse_integer("4"))
        for value in ("", "not-a-float", "nan", "inf"):
            assert_rejected(self, kimi_identity._parse_float, value)
        self.assertEqual(1.5, kimi_identity._parse_float("1.5"))

        for payload, allow_empty in ((b"", True), (b"{}\n", False)):
            if payload:
                self.assertEqual(
                    ({},),
                    kimi_identity._strict_jsonl(payload, allow_empty=allow_empty),
                )
            else:
                self.assertEqual(
                    (), kimi_identity._strict_jsonl(payload, allow_empty=allow_empty)
                )
        for payload in (b"", b"{bad}\n", b"{}\npartial"):
            assert_rejected(
                self, kimi_identity._strict_jsonl, payload, allow_empty=False
            )
        records, valid = kimi_identity._parse_index_tail(
            kimi_identity._TailRead(b"partial\n{}\n", 0, True, False)
        )
        self.assertEqual(({},), records)
        self.assertTrue(valid)
        records, valid = kimi_identity._parse_index_tail(
            kimi_identity._TailRead(b"not-json\n", 0, False, True)
        )
        self.assertEqual((), records)
        self.assertFalse(valid)

    def test_descriptor_and_regular_file_error_boundaries(self):
        capture = kimi_identity._Capture()
        parent = kimi_identity.DirectoryIdentity("/tmp", 1, 1, 0o40700, os.geteuid())
        for function, args in (
            (kimi_identity._open_directory_at, (capture, -1, parent, "")),
            (kimi_identity._open_regular_at, (capture, -1, parent, "bad/name", 10)),
            (kimi_identity._open_regular_tail_at, (capture, -1, parent, "..", 10)),
        ):
            assert_rejected(self, function, *args)
        for size, limit in ((-1, 1), (1, 0), (True, 1), (1, False)):
            assert_rejected(self, kimi_identity._read_fd_tail, -1, size, limit)
        with mock.patch.object(kimi_identity.os, "pread", return_value=b""):
            assert_rejected(self, kimi_identity._read_fd_tail, -1, 2, 1)
        with mock.patch.object(kimi_identity.os, "O_NOFOLLOW", 0):
            assert_rejected(self, kimi_identity._directory_flags)
        with mock.patch.object(kimi_identity.os, "O_DIRECTORY", 0):
            assert_rejected(self, kimi_identity._directory_flags)
        capture.close()

    def test_real_descriptor_paths_cover_race_and_owner_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "directory"
            directory.mkdir(mode=0o700)
            capture = kimi_identity._Capture()
            try:
                opened, identity = kimi_identity._open_absolute_directory(
                    capture, str(directory), reject_symlink_leaf=True
                )
                self.assertEqual(identity.canonical_path, os.path.realpath(str(directory)))
                parent = identity
                file_path = directory / "file"
                file_path.write_bytes(b"payload")
                file_path.chmod(0o600)
                assert_rejected(
                    self,
                    kimi_identity._open_directory_at,
                    capture,
                    opened,
                    parent,
                    "missing",
                )
                assert_rejected(
                    self,
                    kimi_identity._open_regular_at,
                    capture,
                    opened,
                    parent,
                    "missing",
                    100,
                )
                assert_rejected(
                    self,
                    kimi_identity._open_regular_tail_at,
                    capture,
                    opened,
                    parent,
                    "missing",
                    100,
                )
            finally:
                capture.close()

    def test_read_index_and_native_id_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing"
            self.assertEqual(
                ((), False), kimi_identity.read_kimi_index_metadata(missing)
            )
            index = Path(temporary) / "index.jsonl"
            index.write_bytes(b'{"sessionId":"session"}\n')
            rows, valid = kimi_identity.read_kimi_index_metadata(index)
            self.assertTrue(valid)
            self.assertEqual({"sessionId": "session"}, rows[0])

        for value in ([], {"host": "remote"}):
            with self.assertRaises(kimi_identity.KimiIdentityError):
                kimi_identity._native_id(value)
        with self.assertRaises(kimi_identity.KimiIdentityError) as raised:
            kimi_identity._native_id("雪")
        self.assertEqual("invalid_session", raised.exception.code)


class ProcessCoverageMatrixTests(unittest.TestCase):
    def test_process_parsing_and_platform_fallbacks(self):
        rows = process.parse_ps_output(
            b"bad\n0 1s /bin/claude\n1 2s /bin/claude --flag\n"
            b"2 3s /bin/ssh claude\n3 4s /bin/codex",
            cwd_lookup=lambda pid: "/cwd/{}".format(pid),
        )
        self.assertEqual([1, 3], [row["pid"] for row in rows])
        self.assertEqual(
            [],
            process.parse_ps_output(
                "1 1s /bin/claude",
                cwd_lookup=lambda _pid: (_ for _ in ()).throw(OSError()),
            )[0:0],
        )
        with mock.patch.object(process.os, "readlink", side_effect=OSError()):
            self.assertEqual("", process._linux_pid_cwd(1))
        with mock.patch.object(
            process.subprocess,
            "run",
            side_effect=process.subprocess.SubprocessError(),
        ):
            self.assertEqual("", process._macos_pid_cwd(1))
        with mock.patch.object(
            process.subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stdout=b""),
        ):
            self.assertEqual("", process._macos_pid_cwd(1))
        with mock.patch.object(
            process.subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout=b"n/cwd\n"),
        ):
            self.assertEqual("/cwd", process._macos_pid_cwd(1))
        with mock.patch.object(process.sys, "platform", "plan9"):
            self.assertEqual("", process._pid_cwd(1))
        with mock.patch.object(
            process.subprocess,
            "run",
            side_effect=OSError(),
        ):
            self.assertEqual([], process.running_agent_processes())

    def test_strict_process_and_node_classification_guards(self):
        with self.assertRaises(process.ProcessInspectionError):
            process._inspection_remaining(0)
        for value in (object(), mock.Mock(dev=-1, ino=1), mock.Mock(dev=1, ino=0)):
            assert_rejected(self, process._strict_identity, value)
        for value in (object(), mock.Mock(canonical_path=""), mock.Mock(canonical_path="\x00")):
            assert_rejected(self, process._strict_canonical_path, value)
        with mock.patch.object(process, "run_bounded", side_effect=OSError()):
            assert_rejected(
                self,
                process._strict_run,
                ["ps"],
                stdout_limit=1,
                timeout=1,
            )
        bad_result = mock.Mock(returncode=1, overflow=None, cleanup_incomplete=False)
        with mock.patch.object(process, "run_bounded", return_value=bad_result):
            assert_rejected(
                self,
                process._strict_run,
                ["ps"],
                stdout_limit=1,
                timeout=1,
            )
        for value in (b"kimi", b"1 2 /bin/kimi"):
            with mock.patch.object(
                process,
                "_strict_run",
                return_value=mock.Mock(stdout=value),
            ):
                if value == b"kimi":
                    assert_rejected(self, process._strict_process_rows)
                else:
                    self.assertEqual(1, len(process._strict_process_rows()))
        with mock.patch.object(
            process,
            "_strict_run",
            return_value=mock.Mock(stdout=b"1 2 /bin/node /bin/node --require /x"),
        ):
            self.assertEqual(1, len(process._strict_process_rows()))

        for command in (
            "node -e code",
            "node --eval code",
            "node --print code",
            "node -p code",
            "node -r preload.js script.js",
            "node --require=preload.js script.js",
            "node -- --require script.js",
        ):
            tokens = process._strict_command_tokens(command)
            self.assertTrue(tokens)
            self.assertIsNotNone(process._node_candidate_tokens(tokens))
        for command in ("", "node \ud800", "node 'unterminated"):
            assert_rejected(self, process._strict_command_tokens, command)
        self.assertEqual(
            process._NodeKimiClassification.ORDINARY,
            process._classify_node_kimi_candidate(
                "node -e code", (1, 2), time.monotonic() + 5
            )[0],
        )
        assert_rejected(
            self,
            process._classify_node_kimi_candidate,
            "node " + "arg " * process._STRICT_CANDIDATE_TOKEN_LIMIT,
            (1, 2),
            time.monotonic() + 5,
            [process._STRICT_TOTAL_CANDIDATE_TOKEN_LIMIT],
        )

    def test_strict_process_row_and_identity_boundaries(self):
        for value in (None, object(), mock.Mock(dev=True, ino=1)):
            assert_rejected(self, process._strict_identity, value)
        self.assertEqual((1, 2), process._strict_identity(mock.Mock(dev=1, ino=2)))
        for value in (None, "", "\x00"):
            assert_rejected(self, process._strict_canonical_path, mock.Mock(canonical_path=value))
        self.assertEqual("/tmp/project", process._strict_canonical_path(mock.Mock(canonical_path="/tmp/project")))
        assert_rejected(self, process._inspection_remaining, time.monotonic() - 1)

        output = b"\n1 2 /bin/claude\nbad row with kimi\nx 2 /bin/kimi\n-1 2 /bin/kimi\n1 -2 /bin/kimi\n1 2\n"
        with mock.patch.object(
            process,
            "_strict_run",
            return_value=mock.Mock(returncode=0, overflow=None, cleanup_incomplete=False, stdout=output),
        ):
            assert_rejected(self, process._strict_process_rows)
        with mock.patch.object(
            process,
            "_strict_run",
            return_value=mock.Mock(returncode=0, overflow=None, cleanup_incomplete=False, stdout=b"\n1 2 /bin/claude\n"),
        ):
            self.assertEqual([(1, 2, "/bin/claude", "/bin/claude")], process._strict_process_rows())

        with mock.patch.object(process.os, "readlink", return_value=""):
            assert_rejected(self, process._strict_linux_pid_cwd, 1)
        with mock.patch.object(process.os, "readlink", return_value="/cwd"):
            self.assertEqual("/cwd", process._strict_linux_pid_cwd(1))
        with mock.patch.object(process, "_strict_run", side_effect=process.ProcessInspectionError()):
            assert_rejected(self, process._strict_macos_pid_cwd, 1)
        with mock.patch.object(
            process,
            "_strict_run",
            return_value=mock.Mock(stdout=b"n/a\nn/b\n"),
        ):
            assert_rejected(self, process._strict_macos_pid_cwd, 1)
        with mock.patch.object(process.sys, "platform", "plan9"):
            assert_rejected(self, process._strict_candidate_cwd, 1, time.monotonic() + 1)
            assert_rejected(self, process._strict_pid_cwd, 1)

    def test_candidate_tokens_and_executable_binding(self):
        self.assertIsNone(process._candidate_token_identity("", -1))
        self.assertIsNone(process._candidate_token_identity("--", -1))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            regular = root / "regular"
            regular.write_text("x", encoding="utf-8")
            descriptor = os.open(str(root), os.O_RDONLY)
            try:
                identity = process._candidate_token_identity("regular", descriptor)
                self.assertIsNotNone(identity)
                self.assertIsNone(process._candidate_token_identity("missing", descriptor))
            finally:
                os.close(descriptor)
        self.assertEqual(("--require=x", "x"), process._token_probes("--require=x"))
        self.assertEqual(("plain",), process._token_probes("plain"))
        self.assertEqual(("x.js",), process._node_candidate_tokens(("node", "-r", "x.js")))
        self.assertEqual(("=x.js",), process._node_candidate_tokens(("node", "-r=x.js")))
        self.assertEqual((), process._node_candidate_tokens(("node", "-e", "code")))
        self.assertEqual((), process._node_candidate_tokens(("node", "--",)))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "agent"
            path.write_text("agent", encoding="utf-8")
            path.chmod(0o500)
            metadata = os.stat(str(path))
            identity = (metadata.st_dev, metadata.st_ino)
            self.assertEqual(identity, process._strict_executable_binding(str(path), identity)[:2])
            assert_rejected(self, process._strict_executable_binding, "relative", identity)
            assert_rejected(
                self,
                process._strict_executable_binding,
                str(path),
                (identity[0], identity[1] + 1),
            )


class DaemonLogCoverageMatrixTests(unittest.TestCase):
    def test_record_and_validation_matrix(self):
        log = daemon_log.DaemonLog(Path("/tmp/runtime"), version="version")
        record = log._record(
            "event",
            {
                "level": "not-a-level",
                "session_id": "session",
                "http_enabled": True,
                "timed_out": False,
                "http_port": 99999,
                "count": -4,
                "component": "component",
            },
        )
        encoded = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode(
            "ascii"
        )
        log._validate_record_line(encoded)
        for bad in (
            b"",
            b"not-json",
            b"[]",
            json.dumps(dict(record, pid=0)).encode("ascii"),
            json.dumps(dict(record, ts_epoch=True)).encode("ascii"),
            json.dumps(dict(record, unexpected=True)).encode("ascii"),
            json.dumps(dict(record, level=1)).encode("ascii"),
            json.dumps(dict(record, http_enabled="yes")).encode("ascii"),
            json.dumps(dict(record, count=True)).encode("ascii"),
        ):
            assert_rejected(self, log._validate_record_line, bad)
        self.assertEqual("unknown", log._session_identifier(""))
        self.assertEqual("unknown", daemon_log._safe_identifier(None, 4))

    def test_open_append_rotation_and_lifecycle_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            runtime.chmod(0o700)
            log = daemon_log.DaemonLog(
                runtime, max_bytes=300, line_bytes=300, backups=0
            )
            self.assertFalse(log.append("before-open"))
            self.assertIs(log.open(), log)
            self.assertTrue(log.append("startup", count=1))
            with mock.patch.object(
                daemon_log, "_write_all", side_effect=OSError("write")
            ):
                self.assertFalse(log.append("after"))
            self.assertTrue(log.disabled)
            log.close()
            log.close()
            with self.assertRaises(daemon_log.DaemonLogError):
                log.open()

        with tempfile.TemporaryDirectory() as temporary:
            log = daemon_log.DaemonLog(Path(temporary), max_bytes=1, line_bytes=1)
            assert_rejected(self, log._encode, "event", {})
            with mock.patch.object(daemon_log, "_fcntl", None):
                assert_rejected(self, log._open_directory)

    def test_repair_current_handles_suffix_and_io_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "log"
            log = daemon_log.DaemonLog(Path(temporary), line_bytes=1000)
            valid = (
                json.dumps(log._record("event", {}), separators=(",", ":")).encode(
                    "ascii"
                )
                + b"\n"
            )
            path.write_bytes(valid + (b"x" * 1000))
            descriptor = os.open(str(path), os.O_RDWR)
            try:
                assert_rejected(self, log._repair_current, descriptor)
            finally:
                os.close(descriptor)


class TailCoverageMatrixTests(unittest.TestCase):
    def test_jsonl_follower_partial_lines_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            follower = tail.JSONLFollower(path, from_start=False, max_line_bytes=32)
            self.assertEqual([], follower.poll())
            path.write_bytes(b'{"value":1}\npartial')
            self.assertEqual([{"value": 1}], follower.poll())
            checkpoint = follower.export_checkpoint()
            self.assertTrue(follower.restore_checkpoint(checkpoint))
            self.assertFalse(follower.restore_checkpoint({"kind": "wrong"}))
            self.assertFalse(
                follower.restore_checkpoint(dict(checkpoint, offset=True))
            )
            path.write_bytes(b'{"value":2}\n')
            self.assertEqual([{"value": 2}], follower.poll())

            from_start = tail.JSONLFollower(
                path, from_start=True, max_line_bytes=8, max_records=1
            )
            self.assertEqual([], from_start.poll())
            from_start._consume_chunk(b"x" * 9)
            from_start._consume_chunk(b"\n")
            from_start._consume_chunk(b'{"x":1}\nnot-json\n')
            self.assertEqual([{"x": 1}], from_start.poll())
            self.assertFalse(
                from_start.restore_checkpoint(
                    dict(from_start.export_checkpoint(), records=[object()])
                )
            )

    def test_dsh_tailer_replay_and_watch_lifecycle(self):
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "events.zst"
            transcript.write_bytes(b"compressed")
            calls = []

            class DshAdapter:
                name = "dsh"

                def replay(self, session, *, after_seq, max_records):
                    calls.append((session.session_id, after_seq, max_records))
                    if len(calls) == 1:
                        return [{"seq": 0, "text": "baseline"}]
                    if len(calls) == 2:
                        return [
                            {"seq": 1, "text": "first"},
                            {"seq": 2, "text": "second"},
                        ]
                    if len(calls) > 2:
                        raise RuntimeError("replay failed")

                def normalize(self, record, _session):
                    return [
                        daemon.Event(
                            "now",
                            "dsh",
                            "session",
                            "message",
                            record["text"],
                        )
                    ]

            session = daemon.Session(
                "dsh", "session", temporary, str(transcript), 1, status="idle"
            )
            tailer = tail.SessionTailer(
                session, adapter=DshAdapter(), from_start=False, max_records=2
            )
            self.assertTrue(tailer.is_dsh)
            self.assertTrue(tailer.single_poll_per_refresh)
            self.assertEqual([], tailer.poll())
            tailer._dsh_force_replay = True
            events = tailer.poll()
            self.assertEqual(["first", "second"], [event.text for event in events])
            self.assertTrue(tailer.has_pending_records)
            checkpoint = tailer.export_checkpoint()
            self.assertTrue(tailer.restore_checkpoint(checkpoint))
            self.assertFalse(
                tailer.restore_checkpoint(dict(checkpoint, signature=["bad"]))
            )
            self.assertEqual([], tailer.poll())

            failing = tail.SessionTailer(session, adapter=DshAdapter(), from_start=False)
            failing.poll()
            failing._dsh_force_replay = True
            failing.poll()
            self.assertEqual([], failing.poll())
            self.assertTrue(failing.errors)

            cancelled = threading.Event()
            cancelled.set()
            self.assertEqual(
                [],
                list(tail.watch_sessions([], cancel_event=cancelled)),
            )
            with self.assertRaises(ValueError):
                list(tail.watch_sessions([], poll_interval=0))

    def test_tailer_pool_error_and_checkpoint_boundaries(self):
        published = []
        pool = tailer_pool.TailerPool(published.append, max_event_polls=1)
        self.assertEqual(
            "tail_error",
            pool._safe_error_code("unsafe:path"),
        )
        self.assertEqual(
            "RuntimeError",
            pool._safe_error_code(RuntimeError("private")),
        )
        key = ("claude", "session")
        pool._record_tail_error(key, "RuntimeError: private")
        pool._record_tail_error(key, "RuntimeError: duplicate")
        self.assertEqual(1, len(pool.tail_errors))
        pool._consume_tailer_errors(key, mock.Mock(errors=["ValueError: bad"]))
        self.assertEqual(2, len(pool.tail_errors))
        self.assertFalse(pool._has_unread_data(object()))
        self.assertFalse(pool._drop(key))
        pool.close()
        pool.close()
        self.assertTrue(pool.state.closed)


class DaemonCoverageMatrixTests(unittest.TestCase):
    def test_constructor_and_stop_validation(self):
        with self.assertRaises(ValueError):
            daemon.SidecarDaemon(active_interval=0)
        with self.assertRaises(ValueError):
            daemon.SidecarDaemon(max_idle_interval=1, idle_interval=2)
        with self.assertRaises(ValueError):
            daemon.SidecarDaemon(idle_backoff=0.5)
        with self.assertRaises(ValueError):
            daemon.SidecarDaemon(shutdown_timeout="invalid")
        with self.assertRaises(ValueError):
            daemon.SidecarDaemon(http_port=True)
        with tempfile.TemporaryDirectory() as temporary:
            instance = daemon.SidecarDaemon(runtime_dir=Path(temporary))
            with self.assertRaises(ValueError):
                instance.stop("invalid")
            with self.assertRaises(ValueError):
                instance.stop(float("inf"))
            self.assertTrue(instance.stop(0))

    def test_runtime_paths_and_http_startup_outcomes(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            instance = daemon.SidecarDaemon(runtime_dir=runtime)
            instance._prepare_runtime_paths()
            self.assertIsNotNone(instance._runtime_lock_fd)
            instance._release_runtime_lock()
            instance._release_runtime_lock()

            class HttpServer:
                port = 43123

                def start(self):
                    return None

            instance = daemon.SidecarDaemon(
                runtime_dir=runtime,
                http_port=0,
                http_server_factory=lambda *args, **kwargs: HttpServer(),
            )
            instance._log_event = mock.Mock()
            instance._start_http_server()
            self.assertEqual(43123, instance._http_bound_port)
            no_http = daemon.SidecarDaemon(runtime_dir=runtime)
            no_http._start_http_server()

            for failure in (OSError("failure"), daemon.DaemonError("failure"), RuntimeError("failure")):
                failing = daemon.SidecarDaemon(
                    runtime_dir=runtime,
                    http_port=0,
                    http_server_factory=lambda *args, error=failure, **kwargs: (_ for _ in ()).throw(error),
                )
                failing._log_event = mock.Mock()
                with self.assertRaises(Exception):
                    failing._start_http_server()

            class InvalidHttpServer:
                port = 70000

                def start(self):
                    return None

            invalid = daemon.SidecarDaemon(
                runtime_dir=runtime,
                http_port=0,
                http_server_factory=lambda *args, **kwargs: InvalidHttpServer(),
            )
            invalid._log_event = mock.Mock()
            with self.assertRaises(daemon.DaemonError):
                invalid._start_http_server()

            pid_instance = daemon.SidecarDaemon(runtime_dir=runtime)
            pid_instance._prepare_runtime_paths()
            try:
                with mock.patch.object(daemon.os, "write", return_value=0):
                    with self.assertRaises(OSError):
                        pid_instance._write_pidfile()
            finally:
                pid_instance._release_runtime_lock()

    def test_scan_loop_cleanup_and_serve_failure(self):
        instance = daemon.SidecarDaemon(active_interval=0.01, idle_interval=0.01)
        with mock.patch.object(instance, "_stopping", side_effect=[False, True]), \
             mock.patch.object(instance, "_wait_for_stop", return_value=False), \
             mock.patch.object(instance, "_scan_once", return_value=(True, True)):
            instance._scan_loop(None, initial_working=True)
        self.assertGreaterEqual(instance.current_interval, 0)

        failing = daemon.SidecarDaemon()
        failing._prepare_runtime_paths = mock.Mock(side_effect=RuntimeError("failure"))
        failing._log_event = mock.Mock()
        failing._close_http_server = mock.Mock(return_value=True)
        failing._close_clients = mock.Mock()
        failing._join_scan_thread = mock.Mock()
        failing._close_daemon_log = mock.Mock()
        failing._remove_owned_paths = mock.Mock()
        failing._release_runtime_lock = mock.Mock()
        with self.assertRaises(RuntimeError):
            failing.serve_forever()
        self.assertFalse(failing.ready.is_set())


class BatchThreeCoverageMatrixTests(unittest.TestCase):
    def test_json_limits_bus_and_text_boundaries(self):
        with self.assertRaises(json_limits.JSONSyntaxError):
            json_limits.parse_json(b'{"a":1,"a":2}', json_limits.JSONLimits())
        with self.assertRaises(json_limits.JSONLimitError):
            json_limits.parse_json(
                b"123456",
                json_limits.JSONLimits(max_integer_bits=2),
            )
        with self.assertRaises(json_limits.JSONSyntaxError):
            json_limits.validate_json(float("nan"), json_limits.JSONLimits())
        with self.assertRaises(json_limits.JSONSyntaxError):
            json_limits.validate_json({1: "value"}, json_limits.JSONLimits())
        with self.assertRaises(ValueError):
            json_limits.JSONLimits(max_nodes=True)

        event_bus = bus.EventBus(queue_size=1)
        selected = event_bus.subscribe(["claude"])
        selected._offer({"agent": "codex", "value": 0})
        selected._offer({"agent": "claude", "value": 1})
        selected._offer({"agent": "claude", "value": 2})
        self.assertEqual({"agent": "claude", "value": 2}, selected.get())
        selected._finish()
        with self.assertRaises(bus.SubscriptionClosed):
            selected.get()
        event_bus.close()
        busy = bus.EventBus(1).subscribe()
        busy.queue.put_nowait({"existing": True})
        with mock.patch.object(
            busy.queue,
            "put_nowait",
            side_effect=queue.Full(),
        ):
            busy._offer({"agent": "claude"})
            busy._finish()
        with self.assertRaises(ValueError):
            bus.EventBus(queue_size=0)
        with self.assertRaises(ValueError):
            event_bus.subscribe([""])
        self.assertEqual("short", text_utils.snip("short", 20))
        self.assertTrue(text_utils.snip("long value", 4).endswith("…"))
        self.assertEqual("n", text_utils.extract_cursor_title("no title"))

    def test_remote_wire_validation_and_immutable_watch_items(self):
        self.assertEqual(1.5, remote_types._validate_recent_seconds(1.5))
        for value in (True, "bad", float("inf"), 0):
            assert_rejected(self, remote_types._validate_recent_seconds, value)
        completed = mock.Mock(stdout=b"out", stderr=b"err", returncode="bad")
        self.assertEqual(b"out", remote_types._completed_stdout(completed))
        self.assertEqual(b"err", remote_types._completed_stderr(completed))
        self.assertEqual(1, remote_types._completed_returncode(completed))
        self.assertIsNone(remote_types._completed_overflow(completed))
        self.assertEqual(
            ("list", "--json", "--all"),
            remote_types._command_arguments("list"),
        )
        self.assertEqual(
            ("list", "--json", "--recent-seconds", "1.5"),
            remote_types._command_arguments("list", 1.5),
        )
        self.assertEqual(("status", "--json"), remote_types._command_arguments("status"))
        for command, recent in (("bad", None), ("status", 1)):
            assert_rejected(self, remote_types._command_arguments, command, recent)
        row = {
            "agent": "claude",
            "session_id": "session",
            "project": "",
            "transcript": "",
            "title": "",
            "updated_at": 1,
            "status": "waiting",
            "extra": {},
            "parent_id": None,
        }
        row = dict(row, **{key: row.get(key, "") for key in remote_types._SESSION_KEYS})
        self.assertEqual(1, len(remote_types._validate_protocol_rows([row], "host")))
        self.assertEqual({"x": 1}, remote_types._parse_one_line_json('{"x":1}\n', max_bytes=32))
        assert_rejected(self, remote_types._parse_one_line_json, b"")
        assert_rejected(self, remote_types._parse_one_line_json, b'{"x":1} \n')
        assert_rejected(self, remote_types._parse_one_line_json, b" \n")
        assert_rejected(self, remote_types._parse_execution_response, b'{"ok":false,"code":"bad"}', "host")
        with self.assertRaises(remote_types._RemoteResponseFailure):
            remote_types._parse_execution_response(b'{"ok":false,"code":"timeout"}', "host")
        self.assertEqual(
            (),
            remote_types._parse_execution_response(b'{"ok":true,"rows":[]}', "host"),
        )

        ready = remote_watch_types.RemoteWatchReady("host")
        failure = remote_watch_types.RemoteWatchFailure("host", "timeout")
        event = remote_watch_types.validate_watch_event(
            {
                "ts": "now",
                "agent": "claude",
                "session_id": "session",
                "kind": "message",
                "text": "hello",
                "extra": {"nested": [1, 2]},
            },
            "host",
        )
        self.assertEqual("ready", ready.to_dict()["type"])
        self.assertTrue(failure.events_may_be_missed)
        self.assertEqual({"nested": [1, 2]}, event.to_dict()["extra"])
        self.assertEqual(1, remote_watch_types.validate_watch_queue_items(1))
        assert_rejected(self, remote_watch_types.validate_watch_queue_items, True)
        assert_rejected(
            self,
            remote_watch_types.validate_watch_event,
            {"bad": True},
            "host",
        )

    def test_index_and_scanner_failure_isolation(self):
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "transcript"
            transcript.write_text("data", encoding="utf-8")
            first = daemon.Session(
                "claude", "same", temporary, str(transcript), 10, status="idle"
            )
            newer = daemon.Session(
                "claude", "same", temporary, str(transcript), 20, status="waiting"
            )
            cache = index.IncrementalIndex()
            with self.assertRaises(TypeError):
                cache.update([first, newer, "bad"])
            delta = cache.update([first, newer])
            self.assertIn(("claude", "same"), delta.changed)
            self.assertEqual(newer, cache.get(("claude", "same")))
            self.assertIn(("claude", "same"), cache.clear())
            self.assertEqual(0, len(cache))

            class GoodAdapter:
                name = "good"
                agent_names = ("alias", "good")

                def discover(self, _home):
                    return iter(
                        [
                            daemon.Session(
                                "good", "one", temporary, "", 20, status="idle"
                            ),
                            "not-a-session",
                            daemon.Session(
                                "good", "old", temporary, "", 1, status="idle"
                            ),
                        ]
                    )

            class BadAdapter:
                name = "bad"
                agent_names = ()

                def discover(self, _home):
                    raise RuntimeError("discover failed")

            scanner = scan.Scanner(
                adapters={"good": GoodAdapter(), "duplicate": GoodAdapter(), "bad": BadAdapter()},
                state_engine=mock.Mock(),
                home=Path(temporary),
            )
            sessions = scanner.scan(now=20, recent_seconds=10)
            self.assertEqual(["one"], [item.session_id for item in sessions])
            self.assertTrue(scanner.errors)
            self.assertIn("bad", scanner.failed_agent_names)
            with self.assertRaises(ValueError):
                scanner.scan(recent=1, recent_seconds=2)
            with self.assertRaises(ValueError):
                scanner.scan(recent_seconds=-1)

    def test_remote_selection_and_empty_aggregate_boundaries(self):
        hosts = (remote_types.RemoteHost("Beta", "ready"), remote_types.RemoteHost("alpha", "no_dsh"))
        self.assertEqual(
            ("alpha", "Beta"),
            tuple(item.alias for item in remote._selected_hosts(hosts, None)),
        )
        self.assertEqual(
            ("Beta",),
            tuple(item.alias for item in remote._selected_hosts(hosts, ["beta"])),
        )
        for selected in (["alpha", "ALPHA"], ["missing"]):
            assert_rejected(self, remote._selected_hosts, hosts, selected)
        assert_rejected(self, remote._selected_hosts, hosts, [object()])
        assert_rejected(self, remote._selected_hosts, [object()], None)
        result = remote.aggregate_remote("list", hosts=(), artifact=b"")
        self.assertEqual((), result.rows)
        self.assertEqual(0, result.exit_code)
        with self.assertRaises(TypeError):
            list(remote.watch_remote(hosts=(), from_start=1))

    def test_send_validation_and_agent_argument_matrix(self):
        self.assertEqual(b"hello", inject.validate_message("hello"))
        for value in (None, "", " \n", "\x00message", "\ud800"):
            assert_rejected(self, inject.validate_message, value)
        with tempfile.TemporaryDirectory() as temporary:
            session = daemon.Session(
                "claude", "session", temporary, "/tmp/transcript", 1, status="idle"
            )
            self.assertEqual(
                session,
                inject._validate_session(session)[0],
            )
            for status in ("working", "dead"):
                invalid = daemon.Session(
                    "claude", "session", temporary, "/tmp/transcript", 1, status=status
                )
                assert_rejected(self, inject._validate_session, invalid)
            for extra, parent in (({"sidechain": True}, None), ({}, "parent"), ({"host": "h"}, None)):
                invalid = daemon.Session(
                    "claude", "session", temporary, "/tmp/transcript", 1,
                    status="idle", extra=extra, parent_id=parent,
                )
                assert_rejected(self, inject._validate_session, invalid)
            for agent in ("claude", "codex", "cursor-cli", "kimi"):
                self.assertTrue(inject._send_arguments(agent, "exe", "sid", "msg"))
            assert_rejected(self, inject._send_arguments, "unknown", "exe", "sid", "msg")
            self.assertEqual(
                ("exe", "--print", "--resume", "sid", "--input-format", "text", "--output-format", "json"),
                inject._send_arguments("claude", "exe", "sid", "msg"),
            )

    def test_state_metadata_and_terminal_activity_boundaries(self):
        self.assertEqual("hello_world", state._normalized_key(" Hello-world "))
        self.assertEqual({}, state.parse_terminal_metadata("no metadata"))
        metadata = state.parse_terminal_metadata(
            "---\nstatus: running\nrunning-for-ms: 10\n---\nignored: yes"
        )
        self.assertEqual("running", metadata["status"])
        for value in (
            {"status": "completed"},
            {"status": "running"},
            {"running": "yes"},
            {"running-for-ms": "1", "command": "echo"},
            {"current-command": "echo"},
            {"last-command": "echo", "last-exit-code": "null"},
        ):
            self.assertIn(state.terminal_metadata_is_active(value), (True, False))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "terminal.txt"
            path.write_text("---\nstatus: running\n---\n", encoding="utf-8")
            self.assertTrue(state.terminal_file_is_active(path, now=time.time()))
            self.assertFalse(state.terminal_file_is_active(path, now=time.time(), max_bytes=0))
            self.assertFalse(
                state.terminal_file_is_active(path, now=time.time() + 1000)
            )
            session = daemon.Session(
                "cursor", "session", temporary, "/a/agent-transcripts/x", 1,
                extra={"terminals_root": temporary, "project_slug": "slug"},
            )
            self.assertTrue(
                state.cursor_terminal_active(
                    session, now=time.time(), terminals_root=Path(temporary)
                )
            )
            self.assertFalse(
                state.cursor_terminal_active(
                    daemon.Session("claude", "session", temporary, "", 1),
                    terminals_root=Path(temporary),
                )
            )


class CoverageRatchetMatrixTests(unittest.TestCase):
    def test_json_limit_and_text_error_boundaries(self):
        limits = json_limits.JSONLimits(
            max_bytes=3,
            max_depth=1,
            max_nodes=2,
            max_string_bytes=2,
            max_integer_bits=2,
        )
        for payload in (None, b"", b"\xff", b"1234", b'{"a":1}'):
            assert_rejected(self, json_limits.parse_json, payload, limits)
        for value in (object(), {"a": [1, 2]}, {"a": 8}, "\ud800"):
            assert_rejected(self, json_limits.validate_json, value, limits)
        self.assertEqual("ok", json_limits.parse_json('"ok"', json_limits.JSONLimits()))
        self.assertEqual("x…", text_utils.snip("xyz", 2))
        self.assertEqual("", text_utils.snip("xyz", 0))
        self.assertEqual("a", text_utils.normalize_scalar_text("a"))
        self.assertEqual("�", text_utils.normalize_scalar_text("\ud800", "replace"))
        assert_rejected(self, text_utils.normalize_scalar_text, "x", "bad")

    def test_bus_queue_and_close_boundaries(self):
        event_bus = bus.EventBus(queue_size=1)
        filtered = event_bus.subscribe(["claude"])
        filtered._offer({"agent": "codex"})
        filtered._offer({"agent": "claude", "value": 1})
        filtered._offer({"agent": "claude", "value": 2})
        self.assertEqual({"agent": "claude", "value": 2}, filtered.get())
        filtered.queue.put(object())
        assert_rejected(self, filtered.get, timeout=0)
        event_bus.close()
        event_bus.close()
        closed = event_bus.subscribe()
        assert_rejected(self, closed.get, timeout=0)
        assert_rejected(self, bus.EventBus, 0)

    def test_remote_and_watch_type_validation_matrix(self):
        for value in (None, "", "host name", "-host", "local"):
            assert_rejected(self, remote_types.RemoteHost, value, "ready")
        for value in ((1, 2), (3, 4, 5, 6), (True, 2, 3), (-1, 2, 3)):
            assert_rejected(self, remote_types.ProbeResult, value, "/usr/bin/python3")
        assert_rejected(
            self,
            remote_types.ProbeResult,
            (3, 11, 0),
            "relative-python",
        )
        ready = remote_watch_types.RemoteWatchReady("edge")
        self.assertEqual("ready", ready.to_dict()["type"])
        assert_rejected(self, remote_watch_types.RemoteWatchFailure, "edge", "bad")
        event = {
            "ts": "1",
            "agent": "claude",
            "session_id": "sid",
            "kind": "text",
            "text": "",
            "extra": {"nested": [1]},
        }
        self.assertEqual("edge", remote_watch_types.validate_watch_event(event, "edge").host)
        for bad in (None, [], {"bad": True}, {"x": set()}):
            assert_rejected(
                self,
                remote_watch_types.validate_watch_event,
                bad,
                "edge",
            )

    def test_state_and_scan_edge_boundaries(self):
        for metadata in (
            {"status": "done"},
            {"status": "running"},
            {"running": "true"},
            {"running-for-ms": "not-number", "command": "echo"},
            {"current-command": "null"},
            {"last-command": "echo", "last-exit-code": "1"},
        ):
            self.assertIn(state.terminal_metadata_is_active(metadata), (True, False))
        with tempfile.TemporaryDirectory() as temporary:
            session = daemon.Session(
                "cursor", "sid", temporary, "", 1,
                extra={"terminals_root": str(Path(temporary) / "missing")},
            )
            self.assertFalse(state.cursor_terminal_active(session))
            self.assertFalse(
                state.terminal_file_is_active(Path(temporary) / "missing", now=0)
            )
        assert_rejected(self, scan.Scanner.scan, mock.Mock(), recent=1, recent_seconds=2)
        assert_rejected(self, scan.Scanner.scan, mock.Mock(), recent_seconds=-1)

    def test_additional_json_remote_and_title_boundaries(self):
        assert_rejected(
            self,
            json_limits._raw_payload,
            "\ud800",
        )
        self.assertEqual(b"abc", json_limits._raw_payload(bytearray(b"abc")))
        assert_rejected(self, json_limits._raw_payload, object())
        assert_rejected(
            self,
            json_limits.parse_json,
            b"1",
            object(),
        )
        assert_rejected(
            self,
            json_limits.validate_json,
            float("inf"),
            json_limits.JSONLimits(),
        )
        self.assertEqual(
            "query",
            text_utils.extract_cursor_title(
                ["<user_info>hidden</user_info>", "<user_query>query</user_query>"],
                fallback="fallback",
            ),
        )
        self.assertEqual(
            "fallback",
            text_utils.extract_cursor_title([None, "<broken>value"], fallback="fallback"),
        )
        self.assertEqual(
            "[message redacted]",
            text_utils.redact_message("message", "message"),
        )

        hosts = (
            remote_types.RemoteHost("alpha", "ready"),
            remote_types.RemoteHost("beta", "degraded"),
        )
        assert_rejected(self, remote._selected_hosts, hosts, ["ALPHA", "alpha"])
        assert_rejected(self, remote.aggregate_remote, "list", hosts=hosts, max_workers=0)
        assert_rejected(
            self,
            remote.aggregate_remote,
            "list",
            hosts=hosts,
            fleet_timeout=0,
        )
        assert_rejected(
            self,
            remote.aggregate_remote,
            "list",
            hosts=hosts,
            artifact=b"",
        )

