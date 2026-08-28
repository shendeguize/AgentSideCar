import errno
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
import sidecar.adapters.base as adapter_base
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
import sidecar.remote_watch as remote_watch
import sidecar.remote_watch_types as remote_watch_types
import sidecar.remote_watch_transport as remote_watch_transport
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
        class ExplodingRecord(dict):
            def get(self, key, default=None):
                raise TypeError("record")

        assert_rejected(self, audit._validate_record, ExplodingRecord())
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

        with tempfile.TemporaryDirectory() as temporary:
            directory = os.open(temporary, os.O_RDONLY)
            try:
                with mock.patch.object(
                    daemon_log.DaemonLog,
                    "_validate_entry",
                    return_value=None,
                ):
                    assert_rejected(
                        self,
                        daemon_log.DaemonLog._open_private_file,
                        directory,
                        "log",
                    )
            finally:
                os.close(directory)

            log = daemon_log.DaemonLog(Path(temporary), max_bytes=512)
            log.open()
            try:
                with mock.patch.object(
                    log,
                    "_validate_current",
                    return_value=mock.Mock(st_size=0),
                ), mock.patch.object(daemon_log, "_write_all"), mock.patch.object(
                    daemon_log.os, "fstat", return_value=mock.Mock(st_size=0)
                ):
                    self.assertFalse(log.append("size-mismatch"))
                log._disabled = False
                log._closed = False
                log._opened = True
                log._log_fd = None
                self.assertFalse(log.append("missing-fd"))
            finally:
                log.close()

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
                namespace_context = store.open_namespace()
                namespace = namespace_context.__enter__()
                try:
                    namespace.validate()
                    namespace.marker = dict(namespace.marker, key_fingerprint="bad")
                    assert_rejected(self, namespace.validate)
                    namespace.marker = dict(
                        namespace.marker,
                        key_fingerprint=hashlib.sha256(namespace.key).hexdigest(),
                        runtime_dev=-1,
                    )
                    assert_rejected(self, namespace.validate)
                    namespace.marker_exclusive = True
                    with mock.patch.object(
                        audit.fcntl,
                        "flock",
                        side_effect=OSError("downgrade"),
                    ):
                        assert_rejected(self, namespace.downgrade_marker)
                finally:
                    namespace_context.__exit__(None, None, None)
        with mock.patch.object(audit, "fcntl", None):
            with self.assertRaises(Exception):
                with audit.SendAuditStore("/tmp")._open_namespace(
                    hold_exclusive=False
                ):
                    pass
        with mock.patch.object(
            audit,
            "_secure_helpers",
            return_value=mock.Mock(
                _runtime_root=mock.Mock(side_effect=RuntimeError("home"))
            ),
        ), mock.patch.object(
            audit.pwd,
            "getpwuid",
            return_value=mock.Mock(pw_dir="/definitely/missing"),
        ):
            assert_rejected(
                self,
                audit.SendAuditStore("/tmp")._open_anchor,
                mock.Mock(),
                "/tmp",
            )

    def test_audit_append_and_rotation_boundaries(self):
        terminal = dict(
            self.pending,
            outcome="completed",
            delivery="delivered",
            returncode=0,
            error=None,
        )
        store = audit.SendAuditStore("/tmp")
        with tempfile.TemporaryDirectory() as temporary:
            runtime_fd = os.open(temporary, os.O_RDONLY)
            current_path = Path(temporary) / audit.AUDIT_FILE_NAME
            current_fd = os.open(
                str(current_path),
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            try:
                with mock.patch.object(audit, "MAX_AUDIT_BYTES", 1_000_000):
                    store._append(
                        runtime_fd,
                        current_fd,
                        None,
                        terminal,
                        {self.pending["request_id"]: [self.pending]},
                    )
                current_fd = -1
            finally:
                os.close(runtime_fd)
                current_fd = -1

        with tempfile.TemporaryDirectory() as temporary:
            runtime_fd = os.open(temporary, os.O_RDONLY)
            current_path = Path(temporary) / audit.AUDIT_FILE_NAME
            current_fd = os.open(
                str(current_path),
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            try:
                os.ftruncate(current_fd, 999990)
                with mock.patch.object(audit, "MAX_AUDIT_BYTES", 1_000_000):
                    store._append(
                        runtime_fd,
                        current_fd,
                        None,
                        terminal,
                        {self.pending["request_id"]: [self.pending]},
                    )
                current_fd = -1
            finally:
                os.close(runtime_fd)
                current_fd = -1

        for next_created, readback in ((False, None), (True, b"bad")):
            with tempfile.TemporaryDirectory() as temporary:
                runtime_fd = os.open(temporary, os.O_RDONLY)
                current_path = Path(temporary) / audit.AUDIT_FILE_NAME
                current_fd = os.open(
                    str(current_path),
                    os.O_RDWR | os.O_CREAT,
                    0o600,
                )
                next_path = Path(temporary) / audit.AUDIT_NEXT_FILE_NAME
                next_fd = os.open(
                    str(next_path),
                    os.O_RDWR | os.O_CREAT,
                    0o600,
                )
                try:
                    with mock.patch.object(
                        audit,
                        "_validate_named_file",
                    ), mock.patch.object(
                        audit.os,
                        "fstat",
                        return_value=mock.Mock(st_size=999990),
                    ), mock.patch.object(
                        audit,
                        "_open_named_file",
                        return_value=(next_fd, next_created),
                    ), mock.patch.object(
                        audit.os,
                        "read",
                        return_value=readback,
                    ):
                        assert_rejected(
                            self,
                            store._append,
                            runtime_fd,
                            current_fd,
                            None,
                            terminal,
                            {self.pending["request_id"]: [self.pending]},
                        )
                    current_fd = -1
                    next_fd = -1
                finally:
                    os.close(runtime_fd)
                    current_fd = -1
                    next_fd = -1

        with tempfile.TemporaryDirectory() as temporary:
            runtime_fd = os.open(temporary, os.O_RDONLY)
            current_path = Path(temporary) / audit.AUDIT_FILE_NAME
            current_fd = os.open(
                str(current_path),
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            try:
                with mock.patch.object(
                    audit,
                    "_validate_named_file",
                ), mock.patch.object(
                    audit.os,
                    "fstat",
                    return_value=mock.Mock(st_size=0),
                ), mock.patch.object(audit, "_write_all"):
                    assert_rejected(
                        self,
                        store._append,
                        runtime_fd,
                        current_fd,
                        None,
                        terminal,
                        {},
                    )
                current_fd = -1
            finally:
                os.close(runtime_fd)
                current_fd = -1

        with tempfile.TemporaryDirectory() as temporary:
            runtime_fd = os.open(temporary, os.O_RDONLY)
            try:
                unsafe = Path(temporary) / "unsafe"
                unsafe.write_bytes(b"x")
                unsafe.chmod(0o644)
                assert_rejected(
                    self,
                    audit._open_named_file,
                    runtime_fd,
                    "unsafe",
                    create=True,
                )
                missing, created = audit._open_named_file(
                    runtime_fd,
                    "missing",
                    create=False,
                )
                self.assertIsNone(missing)
                self.assertFalse(created)
                safe = Path(temporary) / "safe"
                safe.write_bytes(b"x")
                safe.chmod(0o600)
                helper = mock.Mock()
                helper._entry_stat.return_value = os.stat(safe)
                helper._validate_lock_file.side_effect = RuntimeError("lock")
                with mock.patch.object(audit, "_secure_helpers", return_value=helper):
                    assert_rejected(
                        self,
                        audit._open_named_file,
                        runtime_fd,
                        "safe",
                        create=False,
                    )
            finally:
                os.close(runtime_fd)

        with tempfile.TemporaryDirectory() as temporary:
            runtime_fd = os.open(temporary, os.O_RDONLY)
            marker_path = Path(temporary) / "marker"
            marker_fd = os.open(
                str(marker_path),
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            try:
                marker = {"schema_version": 1, "namespace_epoch": "epoch"}
                with mock.patch.object(
                    audit,
                    "_read_descriptor",
                    return_value=(b"bad", os.fstat(marker_fd)),
                ):
                    assert_rejected(
                        self,
                        audit.SendAuditStore._write_marker,
                        runtime_fd,
                        marker_fd,
                        marker,
                    )
                with mock.patch.object(
                    audit,
                    "_write_all",
                    side_effect=OSError("write"),
                ):
                    assert_rejected(
                        self,
                        audit.SendAuditStore._write_marker,
                        runtime_fd,
                        marker_fd,
                        marker,
                    )
            finally:
                os.close(marker_fd)
                os.close(runtime_fd)

        with tempfile.TemporaryDirectory() as temporary:
            runtime_fd = os.open(temporary, os.O_RDONLY)
            path = Path(temporary) / "audit"
            path.write_bytes(b"x")
            path.chmod(0o600)
            descriptor = os.open(str(path), os.O_RDWR)
            try:
                before = os.stat(path)
                with mock.patch.object(audit, "_validate_named_file"), mock.patch.object(
                    audit.os, "fstat", return_value=before
                ), mock.patch.object(audit.os, "read", return_value=b""):
                    assert_rejected(self, audit._read_file, runtime_fd, "audit", descriptor)
            finally:
                os.close(descriptor)
            descriptor = os.open(str(path), os.O_RDWR)
            try:
                before = os.stat(path)
                after = os.stat(path)
                changed = os.stat_result(
                    (
                        after.st_mode,
                        after.st_ino + 1,
                        after.st_dev,
                        after.st_nlink,
                        after.st_uid,
                        after.st_gid,
                        after.st_size,
                        after.st_atime,
                        after.st_mtime,
                        after.st_ctime,
                    )
                )
                with mock.patch.object(audit, "_validate_named_file"), mock.patch.object(
                    audit.os, "fstat", side_effect=(before, changed)
                ), mock.patch.object(audit.os, "read", return_value=b"x"):
                    assert_rejected(self, audit._read_file, runtime_fd, "audit", descriptor)
            finally:
                os.close(descriptor)
                os.close(runtime_fd)


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

    def test_protocol_envelope_and_reverse_request_boundaries(self):
        request = acp.KimiAcpRequest(
            executable="kimi",
            cwd="/tmp",
            session_id="sid",
            request_id="rid",
            message=b"message",
            deadline=time.monotonic() + 10,
        )
        process = mock.Mock()
        process.write_line.return_value = mock.Mock(
            boundary=acp.DuplexWriteBoundary.COMPLETE
        )
        protocol = acp._Protocol(process, request)
        protocol.pending.add(1)
        self.assertEqual(
            {"jsonrpc": "2.0", "id": 1, "result": {}},
            protocol.consume(
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                expected_id=1,
            ),
        )
        for envelope in (
            {"jsonrpc": "1.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "method": "unknown", "params": {}},
            {"jsonrpc": "2.0", "method": "session/update"},
            {"jsonrpc": "2.0", "method": "session/update", "params": {}},
            {"jsonrpc": "2.0", "id": 4, "result": {}, "error": {}},
        ):
            fresh = acp._Protocol(process, request)
            fresh.pending.add(4)
            assert_rejected(self, fresh.consume, envelope, expected_id=4)
        notification = acp._Protocol(process, request)
        notification.consume(
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {
                    "sessionId": "sid",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "reply"},
                    },
                },
            },
            expected_id=None,
        )
        self.assertEqual("reply", notification.response_text)
        wrong_session = acp._Protocol(process, request)
        assert_rejected(
            self,
            wrong_session._notification,
            {
                "params": {
                    "sessionId": "other",
                    "update": {"sessionUpdate": "agent_message_chunk"},
                }
            },
            "session/update",
        )
        for block in (
            {"type": "image", "data": "data", "mimeType": "image/png"},
            {"type": "text", "text": "\ud800"},
        ):
            non_text = acp._Protocol(process, request)
            with mock.patch.object(acp, "_validate_update"):
                if block["type"] == "text":
                    assert_rejected(
                        self,
                        non_text._notification,
                        {
                            "params": {
                                "sessionId": "sid",
                                "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": block,
                                },
                            }
                        },
                        "session/update",
                    )
                else:
                    non_text._notification(
                        {
                            "params": {
                                "sessionId": "sid",
                                "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": block,
                                },
                            }
                        },
                        "session/update",
                    )
        overflow_notification = acp._Protocol(process, request)
        overflow_notification.response_bytes = acp.ACP_ASSISTANT_BYTES
        with mock.patch.object(acp, "_validate_update"):
            assert_rejected(
                self,
                overflow_notification._notification,
                {
                    "params": {
                        "sessionId": "sid",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "x"},
                        },
                    }
                },
                "session/update",
            )
        permission = acp._Protocol(process, request)
        permission.consume(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "sid",
                    "toolCall": {"toolCallId": "tool", "title": "title"},
                    "options": [
                        {"optionId": "allow", "name": "Allow", "kind": "allow_once"}
                    ],
                },
            },
            expected_id=None,
        )
        unsupported = acp._Protocol(process, request)
        assert_rejected(
            self,
            unsupported.consume,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "other/request",
                "params": {},
            },
            expected_id=None,
        )
        for boundary in (
            acp.DuplexWriteBoundary.NONE,
            acp.DuplexWriteBoundary.PARTIAL,
        ):
            writer = acp._Protocol(process, request)
            writer.write = mock.Mock(
                return_value=boundary,
            )
            assert_rejected(self, writer.request_rpc, 10, "method", {})
        failing_write = acp._Protocol(process, request)
        process.write_line.side_effect = RuntimeError("write")
        assert_rejected(
            self,
            failing_write.write,
            {"jsonrpc": "2.0"},
            request.deadline,
        )
        process.write_line.side_effect = None
        duplicate_request = acp._Protocol(process, request)
        duplicate_request.pending.add(10)
        assert_rejected(self, duplicate_request.request_rpc, 10, "method", {})

        malformed_method = acp._Protocol(process, request)
        assert_rejected(
            self,
            malformed_method.consume,
            {
                "jsonrpc": "2.0",
                "method": "session/update",
                "params": {},
                "extra": True,
            },
            expected_id=None,
        )
        unknown_response = acp._Protocol(process, request)
        unknown_response.pending.add(1)
        assert_rejected(
            self,
            unknown_response.consume,
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            expected_id=1,
        )

        assert_rejected(
            self,
            acp._validate_config_option,
            {
                "type": "select",
                "currentValue": "x",
                "id": "x",
                "name": "x",
                "options": {},
            },
        )
        assert_rejected(
            self,
            acp._validate_config_option,
            {
                "type": "select",
                "currentValue": "x",
                "id": "x",
                "name": "x",
                "options": [{"group": "g", "name": "g", "options": {}}],
            },
        )
        assert_rejected(
            self,
            acp._validate_update,
            {"sessionUpdate": "available_commands_update", "availableCommands": {}},
        )
        assert_rejected(
            self,
            acp._validate_nes_capabilities,
            {"context": {"diagnostics": {"bad": True}}},
        )
        assert_rejected(
            self,
            acp._validate_nes_capabilities,
            {"context": {"editHistory": {"maxCount": -1}}},
        )
        assert_rejected(
            self,
            acp._validate_nes_capabilities,
            {"events": {"document": {"didOpen": {"bad": True}}}},
        )
        assert_rejected(
            self,
            acp._validate_nes_capabilities,
            {"events": {"document": {"didChange": {"syncKind": "bad"}}}},
        )
        assert_rejected(
            self,
            acp._validate_auth_method,
            {
                "type": "env_var",
                "id": "env",
                "name": "env",
                "vars": [{"name": "TOKEN", "optional": "yes"}],
            },
        )
        assert_rejected(
            self,
            acp._validate_auth_method,
            {"type": "terminal", "id": "t", "name": "t", "args": [1]},
        )
        assert_rejected(
            self,
            acp._validate_auth_method,
            {"type": "terminal", "id": "t", "name": "t", "env": {"A": 1}},
        )
        base_capabilities = {
            "loadSession": True,
            "sessionCapabilities": {"list": {}, "resume": {}},
        }
        assert_rejected(
            self,
            acp._validate_agent_capabilities,
            dict(base_capabilities, loadSession=False),
        )
        assert_rejected(
            self,
            acp._validate_agent_capabilities,
            dict(base_capabilities, mcpCapabilities={"acp": "yes"}),
        )
        assert_rejected(
            self,
            acp._validate_agent_capabilities,
            dict(base_capabilities, positionEncoding="bad"),
        )
        assert_rejected(
            self,
            acp._validate_agent_capabilities,
            dict(base_capabilities, providers={"bad": True}),
        )
        assert_rejected(
            self,
            acp._validate_agent_capabilities,
            {
                "loadSession": True,
                "sessionCapabilities": {"list": None, "resume": {}},
            },
        )

        permission = acp._Protocol(process, request)
        for params in (
            {
                "sessionId": "other",
                "toolCall": {"toolCallId": "tool", "title": "title"},
                "options": [{"optionId": "a", "name": "a", "kind": "allow_once"}],
            },
            {
                "sessionId": "sid",
                "toolCall": {"toolCallId": "tool", "title": "title"},
                "options": [],
            },
            {
                "sessionId": "sid",
                "toolCall": {"toolCallId": "tool", "title": "title"},
                "options": [
                    {"optionId": "a", "name": "a", "kind": "allow_once"},
                    {"optionId": "a", "name": "b", "kind": "reject_once"},
                ],
            },
        ):
            assert_rejected(self, permission._validate_permission_params, params)

        invalid_error = acp._Protocol(process, request)
        for error in (
            {"code": "bad", "message": "message"},
            {"code": 1, "message": 1},
            {"code": 1, "message": "message", "extra": True},
        ):
            assert_rejected(self, invalid_error._validate_error, error)
        duplicate = acp._Protocol(process, request)
        duplicate.reverse_ids.add(1)
        assert_rejected(
            self,
            duplicate._reverse_request,
            {"id": 1, "params": {}},
            "other/request",
        )
        overflow = acp._Protocol(process, request)
        overflow.response_bytes = acp.ACP_ASSISTANT_BYTES
        assert_rejected(
            self,
            overflow._notification,
            {
                "params": {
                    "sessionId": "sid",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "x"},
                    },
                },
                "method": "session/update",
            },
            "session/update",
        )
        wrong_session = acp._Protocol(process, request)
        assert_rejected(
            self,
            wrong_session._notification,
            {
                "params": {
                    "sessionId": "other",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "x"},
                    },
                },
            },
            "session/update",
        )
        non_text = acp._Protocol(process, request)
        non_text._notification(
            {
                "params": {
                    "sessionId": "sid",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "image", "data": "x", "mimeType": "image/png"},
                    },
                }
            },
            "session/update",
        )
        unicode_text = acp._Protocol(process, request)
        assert_rejected(
            self,
            unicode_text._notification,
            {
                "params": {
                    "sessionId": "sid",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "\ud800"},
                    },
                }
            },
            "session/update",
        )
        partial_reverse = acp._Protocol(process, request)
        partial_reverse.write = mock.Mock(
            return_value=acp.DuplexWriteBoundary.PARTIAL
        )
        assert_rejected(
            self,
            partial_reverse._reverse_request,
            {
                "id": 3,
                "params": {
                    "sessionId": "sid",
                    "toolCall": {"toolCallId": "tool", "title": "title"},
                    "options": [
                        {"optionId": "a", "name": "a", "kind": "allow_once"}
                    ],
                },
            },
            "session/request_permission",
        )
        assert_rejected(
            self,
            acp._validate_agent_capabilities,
            {
                "loadSession": True,
                "auth": {"logout": {"bad": True}},
                "sessionCapabilities": {"list": {}, "resume": {}},
            },
        )
        listed = {
            "result": {
                "sessions": [
                    {
                        "sessionId": "sid",
                        "cwd": "/tmp",
                        "title": "title",
                        "updatedAt": "now",
                        "additionalDirectories": ["/tmp"],
                    }
                ],
                "nextCursor": None,
            }
        }
        acp._list_result(listed, request, lambda cwd: cwd == "/tmp")
        for sessions, validator in (
            ({}, lambda cwd: True),
            (
                [{"sessionId": "other", "cwd": "/tmp"}],
                lambda cwd: True,
            ),
            (
                [
                    {"sessionId": "sid", "cwd": "/tmp"},
                    {"sessionId": "sid", "cwd": "/tmp"},
                ],
                lambda cwd: True,
            ),
            (
                [{"sessionId": "sid", "cwd": "/tmp", "additionalDirectories": [1]}],
                lambda cwd: True,
            ),
            (
                [{"sessionId": "sid", "cwd": "/tmp"}],
                lambda cwd: (_ for _ in ()).throw(RuntimeError("cwd")),
            ),
            (
                [{"sessionId": "sid", "cwd": "/tmp"}],
                lambda cwd: False,
            ),
        ):
            assert_rejected(
                self,
                acp._list_result,
                {"result": {"sessions": sessions}},
                request,
                validator,
            )
        assert_rejected(
            self,
            acp._list_result,
            {"result": {"sessions": None}},
            request,
            lambda cwd: True,
        )
        cwd_validator = acp._default_cwd_validator("/tmp")
        self.assertFalse(cwd_validator(""))
        self.assertFalse(cwd_validator("/tmp\x00"))
        self.assertTrue(cwd_validator("/tmp"))
        with mock.patch.object(acp.os.path, "samefile", side_effect=OSError):
            self.assertFalse(cwd_validator("/tmp"))
        self.assertFalse(acp._valid_meta_only({"_meta": []}))
        assert_rejected(self, acp._object, {"_meta": []}, set(), {"_meta"})
        assert_rejected(self, acp._validate_config_options, {})
        self.assertTrue(acp._prepare_prompt_frame(request))
        with mock.patch.object(acp, "_encode_frame", return_value=b"null"):
            assert_rejected(self, acp._prepare_prompt_frame, request)
        assert_rejected(
            self,
            acp._prompt_result,
            protocol,
            {"result": {"stopReason": "unknown"}},
        )
        with self.assertRaises(KeyboardInterrupt):
            acp._list_result(
                {"result": {"sessions": [{"sessionId": "sid", "cwd": "/tmp"}]}},
                request,
                lambda cwd: (_ for _ in ()).throw(KeyboardInterrupt()),
            )
        self.assertEqual(
            (None, False, False, 0, 0),
            acp._process_snapshot(None, None),
        )


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
        with mock.patch.object(kimi_identity.os, "pread", return_value=b"xxx"):
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
                regular = kimi_identity._open_regular_at(
                    capture,
                    opened,
                    parent,
                    "file",
                    100,
                )
                self.assertEqual(b"payload", regular[3])
                with mock.patch.object(kimi_identity.os, "read", return_value=b""):
                    assert_rejected(
                        self,
                        kimi_identity._open_regular_at,
                        capture,
                        opened,
                        parent,
                        "file",
                        100,
                    )
                with mock.patch.object(
                    kimi_identity.os,
                    "read",
                    side_effect=(b"payload", b"x"),
                ):
                    assert_rejected(
                        self,
                        kimi_identity._open_regular_at,
                        capture,
                        opened,
                        parent,
                        "file",
                        100,
                    )
                before = os.stat(file_path)
                changed = os.stat_result(
                    (
                        before.st_mode,
                        before.st_ino,
                        before.st_dev,
                        before.st_nlink,
                        before.st_uid,
                        before.st_gid,
                        before.st_size,
                        before.st_atime,
                        before.st_mtime + 1,
                        before.st_ctime,
                    )
                )
                with mock.patch.object(
                    kimi_identity.os,
                    "fstat",
                    side_effect=(before, changed),
                ), mock.patch.object(
                    kimi_identity.os,
                    "read",
                    side_effect=(b"payload", b""),
                ):
                    assert_rejected(
                        self,
                        kimi_identity._open_regular_at,
                        capture,
                        opened,
                        parent,
                        "file",
                        100,
                    )
                regular_tail = kimi_identity._open_regular_tail_at(
                    capture,
                    opened,
                    parent,
                    "file",
                    100,
                )
                self.assertEqual(b"payload", regular_tail[3].payload)
                with mock.patch.object(kimi_identity.os, "O_NOFOLLOW", 0):
                    assert_rejected(
                        self,
                        kimi_identity._open_regular_tail_at,
                        capture,
                        opened,
                        parent,
                        "file",
                        100,
                    )
                with mock.patch.object(
                    kimi_identity,
                    "_owner_safe",
                    return_value=False,
                ):
                    assert_rejected(
                        self,
                        kimi_identity._open_regular_tail_at,
                        capture,
                        opened,
                        parent,
                        "file",
                        100,
                    )
                before_tail = os.stat(file_path)
                changed_tail = os.stat_result(
                    (
                        before_tail.st_mode,
                        before_tail.st_ino,
                        before_tail.st_dev,
                        before_tail.st_nlink,
                        before_tail.st_uid,
                        before_tail.st_gid,
                        before_tail.st_size,
                        before_tail.st_atime,
                        before_tail.st_mtime + 1,
                        before_tail.st_ctime,
                    )
                )
                with mock.patch.object(
                    kimi_identity,
                    "_read_fd_tail",
                    return_value=kimi_identity._TailRead(
                        b"payload",
                        0,
                        False,
                        True,
                    ),
                ), mock.patch.object(
                    kimi_identity.os,
                    "fstat",
                    side_effect=(before_tail, changed_tail),
                ):
                    assert_rejected(
                        self,
                        kimi_identity._open_regular_tail_at,
                        capture,
                        opened,
                        parent,
                        "file",
                        100,
                    )
                child = directory / "child"
                child.mkdir(mode=0o700)
                child_fd, child_identity = kimi_identity._open_directory_at(
                    capture,
                    opened,
                    parent,
                    "child",
                )
                self.assertEqual(os.path.realpath(str(child)), child_identity.canonical_path)
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

    def test_absolute_directory_identity_guard_branches(self):
        capture = mock.Mock()
        capture.keep.side_effect = lambda descriptor: descriptor
        details = mock.Mock(
            st_mode=kimi_identity.stat.S_IFDIR,
            st_uid=os.geteuid(),
        )
        with mock.patch.object(kimi_identity.os, "open", return_value=1), mock.patch.object(
            kimi_identity.os, "fstat", return_value=details
        ), mock.patch.object(kimi_identity, "_owner_safe", return_value=False):
            assert_rejected(
                self,
                kimi_identity._open_absolute_directory,
                capture,
                "/tmp",
                reject_symlink_leaf=True,
            )
        with mock.patch.object(kimi_identity.os.path, "realpath", return_value=""):
            assert_rejected(
                self,
                kimi_identity._open_absolute_directory,
                capture,
                "/tmp",
                reject_symlink_leaf=False,
            )

        linked = mock.Mock()
        opened = mock.Mock()
        root_details = mock.Mock()
        with mock.patch.object(
            kimi_identity.os,
            "open",
            side_effect=(1, 2),
        ), mock.patch.object(
            kimi_identity.os,
            "fstat",
            side_effect=(root_details, opened),
        ), mock.patch.object(
            kimi_identity.os,
            "stat",
            return_value=linked,
        ), mock.patch.object(
            kimi_identity,
            "_same_object",
            return_value=False,
        ):
            assert_rejected(
                self,
                kimi_identity._open_absolute_directory,
                capture,
                "/tmp",
                reject_symlink_leaf=False,
            )
        with mock.patch.object(
            kimi_identity.os,
            "open",
            side_effect=(1, 2),
        ), mock.patch.object(
            kimi_identity.os,
            "fstat",
            side_effect=(root_details, opened),
        ), mock.patch.object(
            kimi_identity.os,
            "stat",
            return_value=linked,
        ), mock.patch.object(
            kimi_identity,
            "_same_object",
            return_value=True,
        ), mock.patch.object(
            kimi_identity,
            "_owner_safe",
            return_value=False,
        ):
            assert_rejected(
                self,
                kimi_identity._open_absolute_directory,
                capture,
                "/tmp",
                reject_symlink_leaf=False,
            )


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
        self.assertEqual(
            ("relative.js", "preload.js", "attached.js", "ignored.js"),
            process._node_candidate_tokens(
                (
                    "node",
                    "-e",
                    "code",
                    "relative.js",
                    "-rpreload.js",
                    "--require=attached.js",
                    "--",
                    "ignored.js",
                )
            ),
        )
        self.assertEqual(
            process._NodeKimiClassification.NEEDS_CWD_IDENTITY,
            process._classify_node_kimi_candidate(
                "node relative.js",
                (1, 2),
                time.monotonic() + 5,
            )[0],
        )
        self.assertEqual(
            process._NodeKimiClassification.ORDINARY,
            process._classify_node_kimi_candidate(
                "node 'relative.js",
                (1, 2),
                time.monotonic() + 5,
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
        for output in (
            b"short row\nx y /bin/node\nx 2 /bin/node\n1 x /bin/node\n1 2\n",
            b"bad row with kimi\n",
            b"x 2 /bin/kimi\n",
            b"1 -2 /bin/kimi\n",
        ):
            with mock.patch.object(
                process,
                "_strict_run",
                return_value=mock.Mock(
                    returncode=0,
                    overflow=None,
                    cleanup_incomplete=False,
                    stdout=output,
                ),
            ):
                if b"kimi" in output:
                    assert_rejected(self, process._strict_process_rows)
                else:
                    self.assertEqual([], process._strict_process_rows())

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

    def test_strict_cwd_identity_and_cleanup_boundaries(self):
        metadata = mock.Mock(
            st_mode=process.stat.S_IFDIR,
            st_dev=2,
            st_ino=3,
        )
        with mock.patch.object(
            process,
            "_strict_run",
            return_value=mock.Mock(stdout=b"p1\0fcwd\0D2\0i3\0n/tmp\0"),
        ), mock.patch.object(
            process.os, "open", return_value=9
        ), mock.patch.object(process.os, "fstat", return_value=metadata):
            self.assertEqual((2, 3, 9), process._strict_macos_cwd_identity(1, time.monotonic() + 1))
        for output in (b"p1\0fcwd\0D2\0i3\0", b"p1\0fcwd\0D2\0i3\0n/tmp\0n/other\0"):
            with mock.patch.object(
                process, "_strict_run", return_value=mock.Mock(stdout=output)
            ):
                assert_rejected(self, process._strict_macos_cwd_identity, 1, time.monotonic() + 1)
        with mock.patch.object(
            process,
            "_strict_run",
            return_value=mock.Mock(stdout=b"p1\0fcwd\0D2\0i3\0n/tmp" + b"\xff"),
        ):
            assert_rejected(self, process._strict_macos_cwd_identity, 1, time.monotonic() + 1)
        with mock.patch.object(process.os, "open", side_effect=OSError):
            assert_rejected(self, process._strict_linux_cwd_identity, 1)
        with mock.patch.object(
            process.os,
            "open",
            return_value=9,
        ), mock.patch.object(
            process.os,
            "fstat",
            return_value=mock.Mock(st_mode=process.stat.S_IFREG),
        ), mock.patch.object(process.os, "close") as close:
            assert_rejected(self, process._strict_linux_cwd_identity, 1)
            close.assert_called_once_with(9)
        with mock.patch.object(
            process,
            "_strict_linux_cwd_identity",
            return_value=(1, 2, 9),
        ), mock.patch.object(
            process,
            "_inspection_remaining",
            side_effect=[1, process.ProcessInspectionError()],
        ), mock.patch.object(process.os, "close") as close, mock.patch.object(
            process.sys, "platform", "linux"
        ):
            assert_rejected(
                self,
                process._strict_candidate_cwd,
                1,
                time.monotonic() + 1,
            )
            close.assert_called_once_with(9)


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
            checkpoint = from_start.export_checkpoint()
            for invalid in (
                dict(checkpoint, identity=[1]),
                dict(checkpoint, identity=[True, 2]),
                dict(checkpoint, anchor=b"x" * 65, offset=65),
                dict(checkpoint, pending=b"x" * 9, offset=9),
                dict(checkpoint, missing_at_start=1),
            ):
                self.assertFalse(from_start.restore_checkpoint(invalid))
            valid_without_identity = dict(
                checkpoint,
                identity=None,
                anchor=b"",
                pending=b"",
                records=(),
            )
            self.assertTrue(from_start.restore_checkpoint(valid_without_identity))

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
        with mock.patch.object(tailer_pool, "MAX_TAIL_ERRORS", 1):
            pool._record_tail_error(("codex", "other"), "RuntimeError: other")
        class ErrorTailer:
            @property
            def errors(self):
                raise RuntimeError("errors")

        class ClearingErrors(list):
            def clear(self):
                raise RuntimeError("clear")

        pool._consume_tailer_errors(key, ErrorTailer())
        pool._consume_tailer_errors(key, mock.Mock(errors=ClearingErrors(["bad"])))
        pool._close_tailer(
            key,
            mock.Mock(
                errors=[],
                follower=ErrorTailer(),
                close=mock.Mock(side_effect=RuntimeError("close")),
            ),
        )
        self.assertFalse(pool._has_unread_data(object()))
        self.assertFalse(pool._drop(key))
        pool.close()
        pool.close()
        self.assertTrue(pool.state.closed)

    def test_jsonl_follower_file_race_and_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_bytes(b"x" * 9)
            follower = tail.JSONLFollower(
                path,
                from_start=False,
                max_line_bytes=8,
            )
            self.assertFalse(follower.poll())
            path.write_bytes(b"x" * 9 + b"\n")
            self.assertEqual([], follower.poll())
            with mock.patch.object(Path, "stat", side_effect=OSError("gone")):
                self.assertEqual([], follower.poll())
            with mock.patch.object(Path, "open", side_effect=OSError("anchor")):
                self.assertEqual(b"", follower._read_anchor(1))
            missing = tail.JSONLFollower(
                Path(temporary) / "missing",
                from_start=False,
            )
            self.assertEqual([], missing.poll())
            with mock.patch.object(
                Path,
                "open",
                side_effect=OSError("read"),
            ):
                self.assertEqual([], follower.poll())


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

    def test_owned_runtime_path_cleanup_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            instance = daemon.SidecarDaemon(runtime_dir=runtime)
            instance.socket_path.parent.mkdir(parents=True, exist_ok=True)
            listener = daemon.socket.socket(daemon.socket.AF_UNIX)
            listener.bind(str(instance.socket_path))
            listener.close()
            socket_details = instance.socket_path.lstat()
            instance._socket_identity = (socket_details.st_dev, socket_details.st_ino)
            instance.pidfile_path.write_text("pid", encoding="utf-8")
            pid_details = instance.pidfile_path.lstat()
            instance._pidfile_owned = True
            instance._pidfile_identity = (pid_details.st_dev, pid_details.st_ino)
            instance._remove_owned_paths()
            self.assertFalse(instance.socket_path.exists())
            self.assertFalse(instance.pidfile_path.exists())
            instance._remove_owned_paths()

            instance.socket_path.touch()
            instance._socket_identity = (0, 0)
            instance._remove_owned_paths()
            instance.socket_path.unlink()
            instance.pidfile_path.write_text("pid", encoding="utf-8")
            instance._pidfile_owned = True
            instance._pidfile_identity = (0, 0)
            instance._remove_owned_paths()
            instance.pidfile_path.unlink()
            instance._pidfile_owned = True
            with mock.patch.object(Path, "lstat", side_effect=OSError("gone")):
                instance._remove_owned_paths()
            instance._pidfile_owned = False
            instance._remove_owned_paths()


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
        for payload in (object(), "\ud800", b"\r", b" x", b"x\nx"):
            assert_rejected(
                self,
                remote_types._parse_one_line_json,
                payload,
                max_bytes=16,
            )
        assert_rejected(
            self,
            remote_types._parse_one_line_json,
            b"x" * 17,
            max_bytes=16,
        )
        for value in (True, float("inf"), -1, remote_types.MAX_SESSION_TIMESTAMP + 1):
            assert_rejected(self, remote_types._validate_updated_at, value)
        assert_rejected(
            self,
            remote_types._validated_session_string,
            {"agent": "\ud800"},
            "agent",
        )
        assert_rejected(self, remote_types._validate_protocol_rows, None, "host")
        assert_rejected(self, remote_types._validate_protocol_rows, [{}], "host")
        for payload in (
            b'{"ok":true,"rows":[],"extra":1}',
            b'{"ok":false,"code":"timeout","extra":1}',
        ):
            assert_rejected(
                self,
                remote_types._parse_execution_response,
                payload,
                "host",
            )
        host = remote_types.RemoteHost("host", "ready")
        empty_aggregate = remote.aggregate_remote(
            "list",
            hosts=(),
            artifact=b"",
        )
        self.assertEqual((), empty_aggregate.hosts)
        with mock.patch.object(
            remote,
            "execute_remote_host",
            return_value=((), None),
        ):
            aggregate = remote.aggregate_remote(
                "list",
                hosts=[host],
                artifact=b"x",
            )
            self.assertEqual(("host",), aggregate.hosts)
        with mock.patch.object(
            remote,
            "execute_remote_host",
            side_effect=RuntimeError("remote"),
        ):
            self.assertEqual(
                "remote",
                remote.aggregate_remote(
                    "list",
                    hosts=[host],
                    artifact=b"x",
                ).failures[0].code,
            )
        with mock.patch.object(
            remote,
            "execute_remote_host",
            return_value=([{"bad": object()}], None),
        ):
            self.assertEqual(
                ("protocol",),
                tuple(
                    failure.code
                    for failure in remote.aggregate_remote(
                        "list",
                        hosts=[host],
                        artifact=b"x",
                    ).failures
                ),
            )
        with mock.patch.object(remote, "MAX_ROWS", 0), mock.patch.object(
            remote,
            "execute_remote_host",
            return_value=([{}], None),
        ):
            self.assertEqual(
                ("resource_limit",),
                tuple(
                    failure.code
                    for failure in remote.aggregate_remote(
                        "list",
                        hosts=[host],
                        artifact=b"x",
                    ).failures
                ),
            )
        watched = remote.watch_remote(hosts=(), artifact=b"")
        watched.close()

        ready = remote_watch_types.RemoteWatchReady("host")
        failure = remote_watch_types.RemoteWatchFailure("host", "timeout")
        for value in (None, "", "\ud800", "x" * 2048):
            assert_rejected(
                self,
                remote_watch_types._validated_text,
                value,
                "agent",
                required=True,
            )
        for extra in (None, object(), {"x": "\ud800"}):
            assert_rejected(self, remote_watch_types._validate_extra, extra)
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
        assert_rejected(
            self,
            json_limits._parse_integer,
            "not-an-integer",
            json_limits.JSONLimits(),
        )
        with mock.patch.object(
            json_limits.json,
            "loads",
            side_effect=ValueError("malformed"),
        ):
            assert_rejected(
                self,
                json_limits.parse_json,
                b"1",
                json_limits.JSONLimits(),
            )
        assert_rejected(
            self,
            json_limits.validate_json,
            1,
            json_limits.JSONLimits(max_nodes=0),
        )
        assert_rejected(
            self,
            json_limits.validate_json,
            8,
            json_limits.JSONLimits(max_integer_bits=2),
        )
        assert_rejected(
            self,
            json_limits.validate_json,
            "too long",
            json_limits.JSONLimits(max_string_bytes=2),
        )

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
            class Entries:
                class Entry:
                    def __init__(self, name, path, failure=False):
                        self.name = name
                        self.path = path
                        self.failure = failure

                    def is_file(self, follow_symlinks=False):
                        if self.failure:
                            raise OSError("entry")
                        return False

                def __enter__(self):
                    return self

                def __exit__(self, *unused):
                    return None

                def __iter__(self):
                    return iter(
                        (
                            self.Entry("not-text", "/tmp/not-text"),
                            self.Entry(
                                "bad.txt",
                                "/tmp/bad.txt",
                                failure=True,
                            ),
                        )
                    )

            with mock.patch.object(state.os, "scandir", return_value=Entries()):
                self.assertFalse(
                    state.cursor_terminal_active(
                        session,
                        terminals_root=Path(temporary),
                        max_files=1,
                    )
                )
                self.assertFalse(
                    state.cursor_terminal_active(
                        session,
                        terminals_root=Path(temporary),
                        max_files=3,
                    )
                )
        assert_rejected(self, scan.Scanner.scan, mock.Mock(), recent=1, recent_seconds=2)
        assert_rejected(self, scan.Scanner.scan, mock.Mock(), recent_seconds=-1)

    def test_adapter_base_parsing_and_cache_boundaries(self):
        self.assertEqual({}, adapter_base.as_mapping([]))
        self.assertIsNone(adapter_base.file_signature("/missing"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "directory"
            directory.mkdir()
            file_path = root / "data"
            file_path.write_text("data", encoding="utf-8")
            self.assertIsNone(adapter_base.file_signature(directory))
            self.assertIsNotNone(adapter_base.file_signature(file_path))
            cache = adapter_base.MetadataCache(1)
            self.assertEqual("a", cache.get_or_load("a", lambda: "a"))
            self.assertEqual("a", cache.get_or_load("a", lambda: "other"))
            self.assertEqual(1, len(cache))
            cache.get_or_load("b", lambda: "b")
            cache.prune(["b"])
            cache.clear()
            self.assertEqual(0, len(cache))
            self.assertEqual({}, adapter_base.read_json_object(file_path, 0))
            self.assertEqual(
                {},
                adapter_base.read_json_object(root / "bad.json", 100),
            )
            valid = root / "valid.json"
            valid.write_text('{"x":1}', encoding="utf-8")
            self.assertEqual({"x": 1}, adapter_base.read_json_object(valid, 100))
            self.assertEqual([], adapter_base.read_jsonl_prefix(valid, 0))
            self.assertEqual([], adapter_base.read_jsonl_prefix(root / "none", 100))
            jsonl = root / "data.jsonl"
            jsonl.write_bytes(b'{"x":1}\nnot-json\n{"x":2}\n')
            self.assertEqual(
                [{"x": 1}, {"x": 2}],
                adapter_base.read_jsonl_prefix(jsonl, 100),
            )
        self.assertEqual(
            "hello",
            adapter_base.sanitize_terminal_text("\x1b[31mhello\x1b[0m"),
        )
        self.assertEqual(
            "hello world",
            adapter_base.sanitize_terminal_text("hello\tworld"),
        )

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
            "<broken>value",
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

    def test_process_discovery_and_identity_boundaries(self):
        def cwd_lookup(pid):
            if pid == 2:
                raise OSError("gone")
            return b"/tmp/project"

        parsed = process.parse_ps_output(
            b"bad\n0 00:01 claude\n1 00:02 /bin/claude --flag\n"
            b"2 00:03 kimi --prompt\n3 00:04 env kimi\n",
            cwd_lookup=cwd_lookup,
        )
        self.assertEqual([1, 2], [item["pid"] for item in parsed])
        assert_rejected(self, process._strict_identity, object())
        for value in (
            mock.Mock(dev=-1, ino=1),
            mock.Mock(dev=1, ino=0),
            mock.Mock(dev=True, ino=1),
        ):
            assert_rejected(self, process._strict_identity, value)
        for value in (object(), mock.Mock(canonical_path=""), mock.Mock(canonical_path="/x\x00")):
            assert_rejected(self, process._strict_canonical_path, value)
        for value in ("", "\ud800"):
            if value == "":
                self.assertIsNone(process._candidate_token_identity(value, 0))
            else:
                assert_rejected(self, process._strict_command_tokens, value)
        with mock.patch.object(process, "run_bounded", side_effect=OSError):
            assert_rejected(
                self,
                process._strict_run,
                ["ps"],
                stdout_limit=10,
                timeout=1,
            )
        with mock.patch.object(process.subprocess, "run", side_effect=OSError):
            self.assertEqual([], process.running_agent_processes())
        with mock.patch.object(process.sys, "platform", "freebsd"):
            self.assertEqual("", process._pid_cwd(1))
            assert_rejected(self, process._strict_pid_cwd, 1)
            assert_rejected(
                self,
                process._strict_candidate_cwd,
                1,
                time.monotonic() + 1,
            )
        with mock.patch.object(process.os, "readlink", side_effect=OSError):
            self.assertEqual("", process._linux_pid_cwd(1))
        with tempfile.TemporaryDirectory() as temporary:
            assert_rejected(self, process._path_identity, temporary)
            file_path = Path(temporary) / "agent"
            file_path.write_text("x", encoding="utf-8")
            self.assertEqual(os.stat(file_path).st_dev, process._path_identity(str(file_path))[0])

    def test_kimi_protocol_validator_matrix(self):
        assert_rejected(self, acp._mapping, None)
        assert_rejected(self, acp._rpc_id, True)
        self.assertTrue(acp._valid_meta_only({}))
        self.assertTrue(acp._valid_meta_only({"_meta": None}))
        self.assertFalse(acp._valid_meta_only({"extra": True}))
        for value in (None, {}, {"data": {}}, {"data": {"error": {"code": "turn.agent_busy"}}}):
            self.assertIn(acp._error_is_busy(value), (True, False))
        assert_rejected(self, acp._object, {}, {"required"}, set())
        for value in (None, 1):
            assert_rejected(self, acp._string, value)
        assert_rejected(self, acp._string, "", nonempty=True)
        acp._string(None, nullable=True)
        acp._string("ok", nonempty=True)
        for value in (True, "1", float("inf")):
            assert_rejected(self, acp._number, value)
        acp._validate_annotations({"audience": ["assistant"], "priority": 1})
        assert_rejected(self, acp._validate_annotations, {"audience": ["system"]})
        content_cases = (
            {"type": "text", "text": "x"},
            {"type": "image", "data": "x", "mimeType": "text/plain", "uri": "u"},
            {"type": "audio", "data": "x", "mimeType": "audio/wav"},
            {"type": "resource_link", "name": "n", "uri": "u", "size": 1},
            {"type": "resource", "resource": {"uri": "u", "text": "x"}},
            {"type": "resource", "resource": {"uri": "u", "blob": "x"}},
        )
        for content in content_cases:
            acp._validate_content_block(content)
        for bad in (
            {},
            {"type": "text"},
            {"type": "resource", "resource": {"uri": "u", "text": "x", "blob": "y"}},
            {"type": "unknown"},
        ):
            assert_rejected(self, acp._validate_content_block, bad)
        acp._validate_tool_location({"path": "x", "line": 1})
        assert_rejected(self, acp._validate_tool_location, {"path": "x", "line": -1})
        acp._validate_tool_content({"type": "content", "content": {"type": "text", "text": "x"}})
        acp._validate_tool_content({"type": "diff", "newText": "n", "path": "p"})
        acp._validate_tool_content({"type": "terminal", "terminalId": "t"})
        assert_rejected(self, acp._validate_tool_content, {"type": "other"})

    def test_audit_parser_and_filesystem_error_boundaries(self):
        for error in (
            OSError(40, "loop"),
            OSError(20, "not directory"),
            RuntimeError("other"),
        ):
            translated = audit._translate_filesystem_error(error)
            self.assertIn(translated.code, ("unsafe_audit", "audit_error"))
        record = {
            "request_id": "request-matrix",
            "outcome": "pending",
        }
        self.assertEqual({}, audit._validate_histories([]))
        assert_rejected(self, audit._validate_histories, [record])
        assert_rejected(
            self,
            audit._parse_file,
            b'{"request_id":"request-matrix"}\nnot-json\n',
        )
        assert_rejected(self, audit._parse_file, b"unterminated")
        self.assertEqual([], audit._parse_file(b""))
        with tempfile.TemporaryDirectory() as temporary:
            runtime = os.open(temporary, os.O_RDONLY)
            try:
                descriptor, created = audit._open_named_file(
                    runtime,
                    "audit.tmp",
                    create=True,
                )
                self.assertTrue(created)
                self.assertIsInstance(descriptor, int)
                os.close(descriptor)
                descriptor, created = audit._open_named_file(
                    runtime,
                    "audit.tmp",
                    create=True,
                )
                self.assertFalse(created)
                os.close(descriptor)
                missing, created = audit._open_named_file(
                    runtime,
                    "missing.tmp",
                    create=False,
                )
                self.assertIsNone(missing)
                self.assertFalse(created)
            finally:
                os.close(runtime)

        with tempfile.TemporaryDirectory() as temporary:
            runtime_path = Path(temporary)
            runtime = os.open(str(runtime_path), os.O_RDONLY)
            try:
                invalid = runtime_path / "invalid"
                invalid.write_bytes(b"x")
                invalid.chmod(0o644)
                assert_rejected(
                    self,
                    audit._open_named_file,
                    runtime,
                    "invalid",
                    create=True,
                )
                assert_rejected(
                    self,
                    audit._open_named_file,
                    runtime,
                    "invalid",
                    create=False,
                )
                valid = runtime_path / "valid"
                valid.write_bytes(b"payload")
                valid.chmod(0o600)
                descriptor = os.open(str(valid), os.O_RDONLY)
                try:
                    with mock.patch.object(audit.os, "read", return_value=b""):
                        assert_rejected(self, audit._read_file, runtime, "valid", descriptor)
                    with mock.patch.object(
                        audit,
                        "_file_signature",
                        side_effect=[(1,), (2,)],
                    ):
                        assert_rejected(self, audit._read_file, runtime, "valid", descriptor)
                finally:
                    os.close(descriptor)
            finally:
                os.close(runtime)

        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "source"
            destination_path = Path(temporary) / "destination"
            source_path.mkdir(mode=0o700)
            destination_path.mkdir(mode=0o700)
            source_fd = os.open(str(source_path), os.O_RDONLY)
            destination_fd = os.open(str(destination_path), os.O_RDONLY)
            try:
                archived = source_path / "audit"
                archived.write_bytes(b"audit")
                archived.chmod(0o600)
                audit.SendAuditStore._archive_files(
                    source_fd,
                    destination_fd,
                    ("missing", "audit"),
                )
                self.assertTrue((destination_path / "audit").exists())
                nested = destination_path / "nested"
                nested.mkdir(mode=0o700)
                nested_file = nested / "record"
                nested_file.write_bytes(b"record")
                nested_file.chmod(0o600)
                audit.SendAuditStore._purge_directory(destination_fd)
                self.assertEqual([], list(destination_path.iterdir()))
                audit.SendAuditStore._validate_archive_entry(destination_fd)
                unknown = destination_path / "unknown"
                unknown.write_bytes(b"unknown")
                unknown.chmod(0o600)
                assert_rejected(
                    self,
                    audit.SendAuditStore._validate_archive_entry,
                    destination_fd,
                )
                unknown.unlink()
                archive = destination_path / audit.AUDIT_ARCHIVE_DIR_NAME
                helper = mock.Mock()
                helper._open_directory_at.side_effect = lambda *args, **kwargs: (
                    os.open(str(archive), os.O_RDONLY),
                    None,
                )
                opened, created = audit.SendAuditStore(temporary)._open_archive_directory(
                    helper,
                    destination_fd,
                    create=True,
                )
                self.assertTrue(created)
                os.close(opened)
                archive.mkdir(exist_ok=True)
                existing, created = audit.SendAuditStore(temporary)._open_archive_directory(
                    mock.Mock(),
                    destination_fd,
                    create=False,
                )
                self.assertFalse(created)
                os.close(existing)
            finally:
                os.close(source_fd)
                os.close(destination_fd)

        pending_record = {
            "request_id": "pending",
            "timestamp": 1.0,
            "outcome": "pending",
        }
        terminal_record = dict(pending_record, outcome="completed")
        self.assertTrue(
            audit.SendAuditStore._active_pending_payload({}, pending_record)
        )
        self.assertFalse(
            audit.SendAuditStore._active_pending_payload(
                {"pending": [pending_record]},
                terminal_record,
            )
        )
        with mock.patch.object(audit, "MAX_ACTIVE_PENDING_RECORDS", 0):
            assert_rejected(
                self,
                audit.SendAuditStore._active_pending_payload,
                {},
                pending_record,
            )
        with tempfile.TemporaryDirectory() as temporary:
            directory = os.open(temporary, os.O_RDONLY)
            try:
                assert_rejected(
                    self,
                    audit.SendAuditStore(temporary)._open_key,
                    directory,
                    allow_create=False,
                )
                key = Path(temporary) / audit.AUDIT_KEY_NAME
                key.write_bytes(b"short")
                key.chmod(0o600)
                assert_rejected(
                    self,
                    audit.SendAuditStore(temporary)._open_key,
                    directory,
                    allow_create=True,
                )
                with mock.patch.object(
                    audit.os,
                    "open",
                    side_effect=OSError("open"),
                ):
                    assert_rejected(
                        self,
                        audit.SendAuditStore(temporary)._open_key,
                        directory,
                        allow_create=True,
                    )
            finally:
                os.close(directory)

        pending = {
            "request_id": "r",
            "agent": "a",
            "target_hmac": "t",
            "request_hmac": "q",
            "confirmation_mode": "m",
            "outcome": "pending",
        }
        terminal = dict(pending, outcome="failed")
        self.assertEqual({"r": [pending]}, audit._validate_histories([pending]))
        self.assertEqual(
            {"r": [pending, terminal]},
            audit._validate_histories([pending, terminal]),
        )
        assert_rejected(
            self,
            audit._validate_histories,
            [pending, dict(pending, target_hmac="other")],
        )
        assert_rejected(self, audit._validate_histories, [terminal])
        assert_rejected(
            self,
            audit._validate_histories,
            [pending, terminal, dict(pending, outcome="completed")],
        )
        assert_rejected(
            self,
            audit._validate_histories,
            [pending, dict(pending, outcome="pending", request_hmac="other")],
        )

    def test_tailer_pool_lifecycle_and_poll_boundaries(self):
        published = []
        pool = tailer_pool.TailerPool(published.append)
        assert_rejected(self, tailer_pool.TailerPool, object())
        for value in (-1,):
            assert_rejected(self, tailer_pool.TailerPool, published.append, tail_recent_seconds=value)
        self.assertEqual("unknown", pool._bounded_identifier("\x00\x01", 4))
        self.assertEqual("RuntimeError", pool._safe_error_code(RuntimeError("private")))
        self.assertEqual("tail_error", pool._safe_error_code("bad code: detail"))

        class ErrorList(list):
            def clear(self):
                raise RuntimeError("clear")

        tailer = mock.Mock(
            errors=ErrorList(["first", "second"]),
            follower=mock.Mock(path="/missing", offset=0),
            has_pending_records=False,
        )
        tailer.close.side_effect = RuntimeError("close")
        pool._consume_tailer_errors(("agent", "session"), tailer)
        pool._close_tailer(("agent", "session"), tailer)
        self.assertGreaterEqual(len(pool.tail_errors), 1)
        self.assertFalse(pool._has_unread_data(tailer))
        pending = mock.Mock(return_value=True)
        self.assertTrue(
            pool._has_unread_data(mock.Mock(has_pending_records=pending))
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events"
            path.write_text("data", encoding="utf-8")
            follower = mock.Mock(path=str(path), offset=0)
            self.assertTrue(
                pool._has_unread_data(
                    mock.Mock(follower=follower, has_pending_records=False)
                )
            )
            follower.offset = 100
            self.assertFalse(
                pool._has_unread_data(
                    mock.Mock(follower=follower, has_pending_records=False)
                )
            )

        event = daemon.Event("now", "claude", "sid", "assistant", "text")
        polling = mock.Mock(
            poll=mock.Mock(side_effect=([event, {"extra": True}], [])),
            errors=[],
            single_poll_per_refresh=False,
            has_pending_records=False,
        )
        self.assertFalse(pool._poll(("claude", "sid"), polling))
        self.assertEqual(2, len(published))
        failing = mock.Mock(poll=mock.Mock(side_effect=RuntimeError("poll")), errors=[])
        self.assertFalse(pool._poll(("claude", "sid"), failing))
        single = mock.Mock(
            poll=mock.Mock(return_value=[]),
            errors=[],
            single_poll_per_refresh=True,
            has_pending_records=False,
        )
        self.assertFalse(pool._poll(("claude", "sid"), single))

        session = daemon.Session(
            "claude",
            "sid",
            "/tmp",
            "/tmp/events.jsonl",
            time.time(),
            status=daemon.Status.WAITING,
            extra={},
        )
        self.assertTrue(pool.supports_tailing(session))
        self.assertTrue(pool.should_tail(session))
        self.assertFalse(
            pool.supports_tailing(
                daemon.Session("claude", "sid", "/tmp", "", 1, status=daemon.Status.IDLE)
            )
        )

        def keyword_factory(current, *, from_start=False):
            return mock.Mock(
                session=current,
                follower=None,
                errors=[],
                export_checkpoint=lambda: {"offset": 1},
            )

        def positional_factory(current):
            return mock.Mock(session=current, follower=None, errors=[])

        self.assertIsNotNone(pool._make_tailer(session, True))
        pool.tailer_factory = keyword_factory
        self.assertIsNotNone(pool._make_tailer(session, True))
        pool.tailer_factory = positional_factory
        self.assertIsNotNone(pool._make_tailer(session, True))

    def test_daemon_log_record_rotation_and_disabled_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            os.chmod(runtime, 0o700)
            log = daemon_log.DaemonLog(
                runtime,
                version="v",
                max_bytes=1024,
                backups=1,
                line_bytes=1024,
            )
            self.assertIs(log.open(), log)
            self.assertIs(log.open(), log)
            self.assertTrue(
                log.append(
                    "event",
                    durable=True,
                    session_id="session",
                    level="critical",
                    http_enabled=True,
                    timed_out=False,
                    http_port=70000,
                    count=-1,
                )
            )
            self.assertTrue(log.append("event-2", level="warning"))
            assert_rejected(self, log._validate_record_line, b"not-json")
            assert_rejected(self, log._validate_record_line, b"{}")
            log.close()
            log.close()
            self.assertFalse(log.append("after-close"))
            with self.assertRaises(daemon_log.DaemonLogError):
                log.open()
        self.assertEqual("unknown", daemon_log.DaemonLog._session_identifier(None))
        self.assertEqual("unknown", daemon_log.DaemonLog._session_identifier(""))
        self.assertTrue(
            daemon_log.DaemonLog._session_identifier("session").startswith("sha256:")
        )

        log = daemon_log.DaemonLog(Path("/tmp/runtime"), line_bytes=512)
        record = log._record("event", {})
        for field, value in (
            ("schema_version", True),
            ("schema_version", 0),
            ("pid", True),
            ("pid", 0),
            ("ts", 1),
            ("ts_epoch", True),
            ("ts_epoch", float("nan")),
            ("component", None),
            ("unexpected", "field"),
        ):
            malformed = dict(record, **{field: value})
            assert_rejected(
                self,
                log._validate_record_line,
                json.dumps(malformed, separators=(",", ":")).encode("ascii"),
            )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "daemon.log"
            valid = log._encode("event", {})
            path.write_bytes(valid + b"partial")
            descriptor = os.open(str(path), os.O_RDWR)
            try:
                log._repair_current(descriptor)
                self.assertEqual(valid, path.read_bytes())
                path.write_bytes(valid + b"partial")
                with mock.patch.object(
                    daemon_log.os, "ftruncate", side_effect=OSError("repair")
                ):
                    assert_rejected(self, log._repair_current, descriptor)
            finally:
                os.close(descriptor)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "repair"
            path.write_bytes(b"x")
            descriptor = os.open(str(path), os.O_RDWR)
            try:
                stat_size_one = mock.Mock(st_size=1)
                with mock.patch.object(
                    daemon_log.os,
                    "fstat",
                    return_value=stat_size_one,
                ), mock.patch.object(daemon_log.os, "read", return_value=b""):
                    assert_rejected(self, log._repair_current, descriptor)
                with mock.patch.object(
                    daemon_log.os,
                    "fstat",
                    side_effect=(mock.Mock(st_size=1), mock.Mock(st_size=2)),
                ), mock.patch.object(daemon_log.os, "read", return_value=b"x"):
                    assert_rejected(self, log._repair_current, descriptor)
                with mock.patch.object(
                    daemon_log.os,
                    "fstat",
                    return_value=mock.Mock(st_size=1),
                ), mock.patch.object(
                    daemon_log.os,
                    "read",
                    side_effect=OSError("read"),
                ):
                    assert_rejected(self, log._repair_current, descriptor)
                path.write_bytes(b"x" * log.line_bytes)
                assert_rejected(self, log._repair_current, descriptor)
            finally:
                os.close(descriptor)

        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            extra = runtime / "daemon.log.2"
            extra.write_bytes(b"extra")
            extra.chmod(0o600)
            rotating = daemon_log.DaemonLog(runtime, backups=1, max_bytes=1024)
            with rotating:
                self.assertTrue(rotating.append("context"))
            disabled = daemon_log.DaemonLog(runtime)
            disabled._opened = True
            disabled._closed = False
            disabled._log_fd = None
            self.assertFalse(disabled.append("missing"))
            disabled._disabled = False
            disabled._log_fd = 1
            with mock.patch.object(daemon_log.os, "close", side_effect=OSError):
                disabled._disable("close")

        with tempfile.TemporaryDirectory() as temporary:
            closing = daemon_log.DaemonLog(Path(temporary))
            closing.open()
            with mock.patch.object(daemon_log.os, "close", side_effect=OSError):
                closing.close()

        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            rotating = daemon_log.DaemonLog(runtime, backups=2, max_bytes=1024)
            rotating.open()
            backup = runtime / "daemon.log.2"
            backup.write_bytes(b"backup")
            backup.chmod(0o600)
            rotating._validate_current = mock.Mock(
                return_value=mock.Mock(st_size=0)
            )
            rotating._validate_entry = mock.Mock()
            rotating._rotate()
            rotating.close()
            current = daemon_log.DaemonLog(runtime)
            current.open()
            try:
                current._validate_entry = mock.Mock(return_value=None)
                assert_rejected(self, current._validate_current)
            finally:
                current.close()

    def test_remote_watch_session_constructor_and_worker_boundaries(self):
        host = remote_types.RemoteHost("edge", "ready")
        assert_rejected(self, remote_watch.RemoteWatchSession, object(), b"x")
        with mock.patch.object(remote_watch, "MAX_HOSTS", 0):
            assert_rejected(self, remote_watch.RemoteWatchSession, [host], b"x")
        assert_rejected(self, remote_watch.RemoteWatchSession, [object()], b"x")
        assert_rejected(
            self,
            remote_watch.RemoteWatchSession,
            [host, remote_types.RemoteHost("EDGE", "ready")],
            b"x",
        )
        for artifact in (None, b""):
            assert_rejected(self, remote_watch.RemoteWatchSession, [host], artifact)
        assert_rejected(
            self,
            remote_watch.RemoteWatchSession,
            [host],
            b"x",
            from_start=1,
        )
        assert_rejected(
            self,
            remote_watch.RemoteWatchSession,
            [host],
            b"x",
            cancel_event=object(),
        )
        failure = remote_types.RemoteFailure("edge", "timeout")

        def opener(*args, **kwargs):
            return None, failure

        session = remote_watch.RemoteWatchSession(
            [host],
            b"x",
            host_opener=opener,
            queue_items=1,
        )
        try:
            item = next(session)
            self.assertEqual("timeout", item.code)
            with self.assertRaises(StopIteration):
                next(session)
            self.assertEqual(("edge",), session.hosts)
            self.assertTrue(session.all_failed)
            self.assertFalse(session.empty)
            session.cancel()
            session.close()
        finally:
            session.close()
        empty = remote_watch.RemoteWatchSession([], b"")
        self.assertTrue(empty.empty)
        with self.assertRaises(StopIteration):
            next(empty)

        def valid_opener(*args, **kwargs):
            return (
                remote_watch_transport.RemoteWatchHostStream(
                    host,
                    iter(
                        (
                            remote_watch_transport.READY_FRAME,
                            remote_watch_transport.END_FRAME,
                        )
                    ),
                ),
                None,
            )

        successful = remote_watch.RemoteWatchSession(
            [host],
            b"x",
            host_opener=valid_opener,
        )
        try:
            self.assertIsInstance(next(successful), remote_watch_types.RemoteWatchReady)
            with self.assertRaises(StopIteration):
                next(successful)
            self.assertEqual(("edge",), successful.ready_hosts)
        finally:
            successful.close()

        for opened in ((None, object()), (None, None), object()):
            broken = remote_watch.RemoteWatchSession(
                [host],
                b"x",
                host_opener=lambda *args, opened=opened, **kwargs: opened,
            )
            try:
                item = next(broken)
                self.assertIsInstance(item, remote_watch_types.RemoteWatchFailure)
                self.assertEqual(("edge",), broken.hosts)
            finally:
                broken.close()

        cancelled = threading.Event()
        cancelled.set()
        skipped = remote_watch.RemoteWatchSession(
            [host],
            b"x",
            cancel_event=cancelled,
            host_opener=valid_opener,
        )
        skipped.close()

        internal = threading.Event()
        buffer = remote_watch._FleetBuffer(
            ("edge",),
            1,
            remote_watch._CombinedCancel(internal, None),
        )
        self.assertTrue(buffer.put("edge", "item", priority=False))
        self.assertEqual("item", buffer.get(0))
        with self.assertRaises(queue.Empty):
            buffer.get(0)
        buffer._size = 1
        with self.assertRaises(queue.Empty):
            buffer.get(0)
        closed_session = remote_watch.RemoteWatchSession([], b"")
        closed_session._closed = True
        with self.assertRaises(StopIteration):
            next(closed_session)
        timeout_session = remote_watch.RemoteWatchSession([], b"")
        timeout_session._workers = [mock.Mock()]
        timeout_session._buffer = mock.Mock(
            get=mock.Mock(side_effect=queue.Empty()),
            empty=mock.Mock(return_value=True),
            wake_all=mock.Mock(),
        )
        with self.assertRaises(StopIteration):
            next(timeout_session)
        close_session = remote_watch.RemoteWatchSession([], b"")
        close_session._active = {
            "edge": mock.Mock(close=mock.Mock(side_effect=RuntimeError("close")))
        }
        close_session._workers = [mock.Mock()]
        with mock.patch.object(
            remote_watch.time,
            "monotonic",
            side_effect=(0.0, 100.0),
        ):
            close_session.close()
        internal.set()
        self.assertFalse(buffer.put("edge", "cancelled", priority=True))

        direct = remote_watch.RemoteWatchSession([], b"")
        direct._offer = mock.Mock(return_value=True)
        direct._host_opener = lambda *args, **kwargs: object()
        direct._run_host(host)
        self.assertEqual("remote", direct.failures[0].code)
        transport_failure = remote_watch.RemoteWatchSession([], b"")
        transport_failure._offer = mock.Mock(return_value=True)
        transport_failure._host_opener = mock.Mock(
            side_effect=remote_watch_transport.RemoteWatchTransportError("timeout")
        )
        transport_failure._run_host(host)
        self.assertEqual("timeout", transport_failure.failures[0].code)

        class BrokenStream(remote_watch_transport.RemoteWatchHostStream):
            def __init__(self):
                self._closed = False

            def read_ready(self):
                return remote_watch_types.RemoteWatchReady("edge")

            def __iter__(self):
                return iter((object(),))

            def close(self):
                raise RuntimeError("close")

        broken_stream = remote_watch.RemoteWatchSession([], b"")
        broken_stream._offer = mock.Mock(return_value=True)
        broken_stream._host_opener = lambda *args, **kwargs: (BrokenStream(), None)
        broken_stream._run_host(host)
        self.assertEqual("remote", broken_stream.failures[0].code)
        stopped_ready = remote_watch.RemoteWatchSession([], b"")
        stopped_ready._offer = mock.Mock(return_value=False)
        stopped_ready._host_opener = lambda *args, **kwargs: (
            BrokenStream(),
            None,
        )
        stopped_ready._run_host(host)

        class EventStream(remote_watch_transport.RemoteWatchHostStream):
            def __init__(self):
                self._closed = False

            def read_ready(self):
                return remote_watch_types.RemoteWatchReady("edge")

            def __iter__(self):
                return iter(
                    (
                        remote_watch_types.RemoteWatchEvent(
                            "edge",
                            "now",
                            "claude",
                            "sid",
                            "text",
                            "text",
                            {},
                        ),
                    )
                )

            def close(self):
                return None

        stopped_event = remote_watch.RemoteWatchSession([], b"")
        stopped_event._offer = mock.Mock(side_effect=(True, False))
        stopped_event._host_opener = lambda *args, **kwargs: (
            EventStream(),
            None,
        )
        stopped_event._run_host(host)

        invalid_queue = remote_watch.RemoteWatchSession([], b"")
        invalid_queue._workers = [mock.Mock()]
        invalid_queue._buffer = mock.Mock()
        invalid_queue._buffer.get.return_value = object()
        with self.assertRaises(RuntimeError):
            next(invalid_queue)
        interrupted = remote_watch.RemoteWatchSession([], b"")
        interrupted._workers = [mock.Mock()]
        interrupted._buffer = mock.Mock()
        interrupted._buffer.get.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            next(interrupted)

    def test_kimi_capability_and_session_update_boundaries(self):
        acp._validate_config_option(
            {"type": "boolean", "currentValue": True, "id": "b", "name": "bool"}
        )
        acp._validate_config_option(
            {
                "type": "select",
                "currentValue": "one",
                "id": "s",
                "name": "select",
                "options": [{"name": "one", "value": "one", "description": "d"}],
            }
        )
        acp._validate_config_option(
            {
                "type": "select",
                "currentValue": "one",
                "id": "g",
                "name": "grouped",
                "options": [
                    {
                        "group": "group",
                        "name": "group",
                        "options": [{"name": "one", "value": "one"}],
                    }
                ],
            }
        )
        acp._validate_mode_state(
            {
                "availableModes": [{"id": "default", "name": "Default", "description": "d"}],
                "currentModeId": "default",
            }
        )
        acp._validate_model_state(
            {
                "availableModels": [{"modelId": "model", "name": "Model"}],
                "currentModelId": "model",
            }
        )
        acp._validate_usage(
            {
                "inputTokens": 1,
                "outputTokens": 2,
                "totalTokens": 3,
                "cachedReadTokens": None,
                "cachedWriteTokens": 4,
                "thoughtTokens": 5,
            }
        )
        acp._validate_auth_method(
            {
                "type": "env_var",
                "id": "env",
                "name": "Environment",
                "vars": [{"name": "TOKEN", "label": "Token", "optional": True, "secret": True}],
                "link": "https://example.test",
            }
        )
        acp._validate_auth_method(
            {"type": "terminal", "id": "terminal", "name": "Terminal", "args": ["--login"], "env": {"A": "B"}}
        )
        acp._validate_auth_method({"id": "other", "name": "Other"})
        acp._validate_nes_capabilities(
            {
                "context": {
                    "diagnostics": {},
                    "editHistory": {"maxCount": 1},
                    "openFiles": {"_meta": None},
                },
                "events": {
                    "document": {
                        "didChange": {"syncKind": "incremental"},
                        "didOpen": {},
                    }
                },
            }
        )
        acp._validate_agent_capabilities(
            {
                "loadSession": True,
                "mcpCapabilities": {"acp": True, "http": False, "sse": True},
                "promptCapabilities": {"audio": False, "embeddedContext": True, "image": True},
                "positionEncoding": "utf-8",
                "providers": {},
                "sessionCapabilities": {"list": {}, "resume": {}, "close": None},
            }
        )
        for update in (
            {"sessionUpdate": "plan", "entries": []},
            {"sessionUpdate": "plan_removed", "id": "plan"},
            {"sessionUpdate": "current_mode_update", "currentModeId": "default"},
            {"sessionUpdate": "session_info_update", "title": "title", "updatedAt": "now"},
            {"sessionUpdate": "usage_update", "size": 1, "used": 1},
            {
                "sessionUpdate": "available_commands_update",
                "availableCommands": [{"description": "d", "name": "n"}],
            },
            {
                "sessionUpdate": "config_option_update",
                "configOptions": [
                    {"type": "boolean", "currentValue": True, "id": "b", "name": "bool"}
                ],
            },
        ):
            acp._validate_update(update)
        assert_rejected(self, acp._validate_auth_method, {"type": "unknown", "id": "x", "name": "x"})

    def test_inject_validation_and_executable_boundaries(self):
        self.assertEqual(b"hello", inject.validate_message("hello"))
        for message in (None, "\x00", " \t", "\ud800"):
            assert_rejected(self, inject.validate_message, message)
        assert_rejected(self, inject._validate_session_id, "")
        assert_rejected(self, inject._validate_session_id, object())
        assert_rejected(self, inject._validate_session_id, "bad id")
        assert_rejected(self, inject._validate_session_id, "\ud800")
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            file_path = project / "file"
            file_path.write_text("data", encoding="utf-8")
            executable = project / "agent"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o700)
            self.assertEqual(
                os.path.realpath(str(project)),
                inject._resolve_project(str(project)),
            )
            assert_rejected(self, inject._resolve_project, str(file_path))
            for value in ("relative", "", "bad\x00path"):
                assert_rejected(self, inject._resolve_project, value)
            assert_rejected(self, inject._directory_identity, str(file_path))
            self.assertEqual(str(file_path), inject._transcript_path(str(file_path)))
            for value in ("relative", "", "bad\x00path"):
                assert_rejected(self, inject._transcript_path, value)
            self.assertEqual(
                (str(file_path) + "-missing", False, 0, 0, 0, 0, 0, 0),
                inject._stat_source(str(file_path) + "-missing", required=False),
            )
            assert_rejected(
                self,
                inject._stat_source,
                str(file_path) + "-missing",
                required=True,
            )
            identity = inject._executable_identity(str(executable))
            self.assertEqual(str(executable), identity[0])
            with mock.patch.object(inject.os, "access", return_value=False):
                assert_rejected(self, inject._executable_identity, str(executable))
            assert_rejected(self, inject._executable_identity, str(executable) + "-missing")
            for resolved in (None, 1, "", "bad\x00path", str(file_path)):
                assert_rejected(self, inject._resolve_executable, "agent", lambda _: resolved)
            assert_rejected(
                self,
                inject._resolve_executable,
                "agent",
                lambda _: (_ for _ in ()).throw(OSError("missing")),
            )
            self.assertEqual(
                os.path.realpath(str(executable)),
                inject._resolve_executable("agent", lambda _: str(executable)),
            )
            with mock.patch.object(Path, "resolve", side_effect=RuntimeError("resolve")):
                assert_rejected(
                    self,
                    inject._resolve_executable,
                    "agent",
                    lambda _: str(executable),
                )
            with mock.patch.object(
                inject.os.path,
                "abspath",
                side_effect=RuntimeError("absolute"),
            ):
                assert_rejected(self, inject._transcript_path, str(file_path))
            with mock.patch.object(inject.os, "stat", side_effect=OSError("stat")):
                assert_rejected(
                    self,
                    inject._stat_source,
                    str(file_path),
                    required=False,
                )
            self.assertEqual(
                (str(file_path), str(file_path) + "-wal"),
                inject._source_paths(
                    inject.Session(
                        "cursor-cli",
                        "sid",
                        str(project),
                        str(file_path),
                        1,
                        extra={"transcript_kind": "cursor-chat-sqlite"},
                    ),
                    str(file_path),
                ),
            )
            for agent in ("claude", "codex", "cursor-cli", "kimi"):
                self.assertTrue(inject._send_arguments(agent, str(executable), "sid", "msg"))
            assert_rejected(
                self,
                inject._send_arguments,
                "unknown",
                str(executable),
                "sid",
                "msg",
            )
        for agent in ("claude", "codex", "cursor-cli", "kimi", "unknown"):
            session = inject.Session(
                agent,
                "sid",
                "/tmp",
                "/tmp/transcript",
                1,
                extra={},
            )
            if agent == "unknown":
                assert_rejected(self, inject._session_agent, session)
            else:
                self.assertEqual(agent, inject._session_agent(session))
        self.assertEqual(
            "invalid_session",
            inject._send_error_from_kimi_identity(
                kimi_identity.KimiIdentityError("invalid_session")
            ).code,
        )
        self.assertEqual(
            "invalid_session",
            inject._send_error_from_kimi_identity(mock.Mock(code="private")).code,
        )
        with self.assertRaises(inject.SendError):
            inject._validate_session(object())
        for status in (inject.Status.WORKING, inject.Status.DEAD):
            assert_rejected(
                self,
                inject._validate_session,
                inject.Session("claude", "sid", "/tmp", "/tmp/t", 1, status=status),
            )
        assert_rejected(
            self,
            inject._validate_session,
            inject.Session(
                "claude",
                "sid",
                "/tmp",
                "/tmp/t",
                1,
                extra={"remote": True},
            ),
        )
        invalid_status = inject.Session(
            "claude",
            "sid",
            "/tmp",
            "/tmp/t",
            1,
        )
        invalid_status.status = "unexpected"
        assert_rejected(self, inject._validate_session, invalid_status)
        assert_rejected(
            self,
            inject._validate_session,
            inject.Session(
                "claude",
                "sid",
                "/tmp",
                "/tmp/t",
                1,
                extra="invalid",
            ),
        )
        with mock.patch.object(inject, "_executable_identity", return_value=("x",)):
            for stdout in (b"0.38.0\n", "0.38.0\n", b"bad"):
                result = mock.Mock(
                    stdout=stdout,
                    returncode=0,
                    overflow=None,
                    cleanup_incomplete=False,
                )
                if stdout in (b"0.38.0\n", "0.38.0\n"):
                    inject._probe_kimi_version("x", ("x",), lambda *a, **k: result)
                else:
                    assert_rejected(
                        self,
                        inject._probe_kimi_version,
                        "x",
                        ("x",),
                        lambda *a, **k: result,
                    )
            object_result = mock.Mock(
                stdout=object(),
                returncode=0,
                overflow=None,
                cleanup_incomplete=False,
            )
            assert_rejected(
                self,
                inject._probe_kimi_version,
                "x",
                ("x",),
                lambda *a, **k: object_result,
            )

        with mock.patch.object(inject.os, "write", return_value=0):
            assert_rejected(self, inject._write_all, 1, b"x")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "kimi"
            source.write_text("#!/bin/sh\n", encoding="utf-8")
            source.chmod(0o500)
            source_identity = inject._executable_identity(str(source))
            destination = root / "snapshot"
            copied = inject._snapshot_verified_file(
                str(source),
                destination,
                expected=source_identity,
                limit=inject.KIMI_RUNTIME_FILE_BYTES,
                mode=0o500,
            )
            self.assertEqual(source_identity[7], copied[7])
            analysis_copy = root / "analysis-copy"
            analyzed = inject._snapshot_runtime_asset_for_analysis(
                str(source),
                analysis_copy,
                "analysis-copy",
            )
            self.assertEqual("analysis-copy", analyzed.relative_path)
            asset = inject._runtime_asset(str(source), "kimi")
            manifest = inject._KimiRuntimeManifest(
                package_root=str(root),
                package_assets=(asset,),
            )
            inject._validate_runtime_manifest(manifest)
            node_manifest = inject._KimiRuntimeManifest(
                package_root=str(root),
                package_assets=(asset,),
                node=asset,
            )
            bound_probe = inject._BoundKimiExecutable(
                node_manifest,
                str(source),
                mock.Mock(),
            )
            probe_process = mock.Mock()
            probe_process.read_line.return_value = json.dumps(
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
            ).encode("ascii")
            probe_process.terminate_tree.return_value = mock.Mock(
                cleanup_complete=True
            )
            with mock.patch.object(
                inject,
                "BoundedDuplexLineProcess",
                return_value=probe_process,
            ):
                inject._probe_bound_kimi_initialize(bound_probe)
            inject._probe_bound_kimi_version(
                bound_probe,
                lambda *args, **kwargs: mock.Mock(
                    stdout="0.38.0",
                    returncode=0,
                    overflow=None,
                    cleanup_incomplete=False,
                ),
            )
            assert_rejected(
                self,
                inject._probe_bound_kimi_version,
                bound_probe,
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    RuntimeError("version")
                ),
            )
            bad_probe = mock.Mock()
            bad_probe.read_line.return_value = b"{}"
            bad_probe.terminate_tree.return_value = mock.Mock(
                cleanup_complete=True
            )
            with mock.patch.object(
                inject,
                "BoundedDuplexLineProcess",
                return_value=bad_probe,
            ):
                assert_rejected(
                    self,
                    inject._probe_bound_kimi_initialize,
                    bound_probe,
                )
            with mock.patch.object(
                inject,
                "BoundedDuplexLineProcess",
                side_effect=KeyboardInterrupt(),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    inject._probe_bound_kimi_initialize(bound_probe)
            bound = inject._bind_kimi_executable(manifest)
            self.assertTrue(Path(bound.executable).is_file())
            bound.close()
            self.assertEqual(3, len(inject._capture_package_assets(root)))
            non_node = inject._capture_kimi_runtime_manifest(
                str(source),
                source_identity,
            )
            self.assertEqual((asset,), non_node.package_assets)
            with mock.patch.object(inject.sys, "platform", "linux"), mock.patch.object(
                inject, "_runtime_asset", return_value=asset
            ):
                self.assertEqual(
                    (asset, (), ()),
                    inject._capture_macho_closure(str(source)),
                )
            assert_rejected(
                self,
                inject._snapshot_verified_file,
                str(source),
                root / "bad",
                expected=tuple(source_identity[:-1]) + ("wrong",),
                limit=inject.KIMI_RUNTIME_FILE_BYTES,
                mode=0o500,
            )
            with mock.patch.object(inject.os, "read", return_value=b""):
                assert_rejected(
                    self,
                    inject._snapshot_verified_file,
                    str(source),
                    root / "empty-copy",
                    expected=source_identity,
                    limit=inject.KIMI_RUNTIME_FILE_BYTES,
                    mode=0o500,
                )
            session = inject.Session(
                "claude",
                "sid",
                str(root),
                str(source),
                1,
                status=inject.Status.WAITING,
                extra={},
            )
            plan = inject.build_send_plan(
                session,
                "message",
                lambda _name: str(source),
            )
            assert_rejected(
                self,
                inject._refreshed_send_plan,
                plan,
                "message",
                lambda: (_ for _ in ()).throw(RuntimeError("refresh")),
                lambda _name: str(source),
            )
            assert_rejected(
                self,
                inject._refreshed_send_plan,
                plan,
                "message",
                lambda: [],
                lambda _name: str(source),
            )
            assert_rejected(
                self,
                inject._refreshed_send_plan,
                plan,
                "message",
                lambda: [session, session],
                lambda _name: str(source),
            )
            self.assertEqual(plan, inject._preflight_plan(plan, filesystem=True)[0])
            for invalid in (
                inject.replace(plan, agent="unknown"),
                inject.replace(plan, prompt_transport="argv"),
                inject.replace(plan, transport="bad"),
                inject.replace(plan, cwd="relative"),
                inject.replace(plan, executable="relative"),
                inject.replace(plan, argv=("bad",)),
                inject.replace(plan, target=inject.replace(plan.target, project="other")),
            ):
                assert_rejected(self, inject._preflight_plan, invalid)

    def test_daemon_runtime_path_failure_matrix(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / "runtime"
            instance = daemon.SidecarDaemon(runtime_dir=runtime)
            with mock.patch.object(Path, "lstat", side_effect=OSError("inspect")):
                assert_rejected(self, instance._prepare_runtime_paths)

            runtime.mkdir(mode=0o700, exist_ok=True)
            for mode, uid in (
                (daemon.stat.S_IFREG | 0o700, os.geteuid()),
                (daemon.stat.S_IFLNK | 0o700, os.geteuid()),
                (daemon.stat.S_IFDIR | 0o700, os.geteuid() + 1),
            ):
                details = mock.Mock(st_mode=mode, st_uid=uid)
                with mock.patch.object(Path, "lstat", return_value=details):
                    assert_rejected(self, instance._prepare_runtime_paths)

            details = mock.Mock(
                st_mode=daemon.stat.S_IFDIR | 0o755,
                st_uid=os.geteuid(),
                st_dev=1,
                st_ino=2,
            )
            with mock.patch.object(Path, "lstat", return_value=details), mock.patch.object(
                daemon.os, "chmod", side_effect=OSError("chmod")
            ):
                assert_rejected(self, instance._prepare_runtime_paths)

            changed = mock.Mock(
                st_mode=daemon.stat.S_IFDIR | 0o700,
                st_uid=os.geteuid(),
                st_dev=1,
                st_ino=3,
            )
            with mock.patch.object(
                Path, "lstat", side_effect=[details, changed]
            ):
                assert_rejected(self, instance._prepare_runtime_paths)

            instance._acquire_runtime_lock = mock.Mock()
            instance.socket_path.parent.mkdir(parents=True, exist_ok=True)
            instance.socket_path.write_text("not socket", encoding="utf-8")
            assert_rejected(self, instance._prepare_runtime_paths)
            instance.socket_path.unlink()
            instance.socket_path.touch()
            with mock.patch.object(daemon, "_socket_is_live", return_value=True):
                assert_rejected(self, instance._prepare_runtime_paths)
            instance.socket_path.unlink()

            instance.pidfile_path.write_text("pid", encoding="utf-8")
            with mock.patch.object(Path, "unlink", side_effect=OSError("unlink")):
                assert_rejected(self, instance._prepare_runtime_paths)
            instance.pidfile_path.unlink()
            instance.pidfile_path.mkdir()
            assert_rejected(self, instance._prepare_runtime_paths)
            instance.pidfile_path.rmdir()

            for payload in ("0", "bad", "\ud800"):
                instance.pidfile_path.write_bytes(
                    payload.encode("utf-8", "surrogatepass")
                )
                self.assertIsNone(daemon.read_pid(runtime))
                instance.pidfile_path.unlink()

            instance._acquire_runtime_lock = mock.Mock()
            real_lstat = Path.lstat
            with mock.patch.object(
                Path,
                "lstat",
                side_effect=lambda path: (
                    (_ for _ in ()).throw(OSError("socket inspect"))
                    if path == instance.socket_path
                    else real_lstat(path)
                ),
            ):
                assert_rejected(self, instance._prepare_runtime_paths)
            with mock.patch.object(
                Path,
                "lstat",
                side_effect=lambda path: (
                    (_ for _ in ()).throw(OSError("pid inspect"))
                    if path == instance.pidfile_path
                    else real_lstat(path)
                ),
            ):
                assert_rejected(self, instance._prepare_runtime_paths)

            lock_instance = daemon.SidecarDaemon(runtime_dir=runtime)
            lock_instance.lockfile_path.touch()
            with mock.patch.object(daemon.os, "open", side_effect=OSError("open")):
                assert_rejected(self, lock_instance._acquire_runtime_lock)
            lock_instance.lockfile_path.chmod(0o644)
            opened = os.stat(lock_instance.lockfile_path)
            with mock.patch.object(
                daemon.os,
                "fstat",
                side_effect=(opened, opened),
            ), mock.patch.object(daemon.os, "fchmod"):
                assert_rejected(self, lock_instance._acquire_runtime_lock)
            lock_instance.lockfile_path.chmod(0o600)
            with mock.patch.object(
                daemon._fcntl,
                "flock",
                side_effect=OSError(daemon.errno.EAGAIN, "busy"),
            ):
                assert_rejected(self, lock_instance._acquire_runtime_lock)
            lock_instance._runtime_lock_fd = os.open(
                str(lock_instance.lockfile_path),
                os.O_RDWR,
            )
            with mock.patch.object(daemon.os, "close", side_effect=OSError("close")):
                lock_instance._release_runtime_lock()

        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            cleanup = daemon.SidecarDaemon(runtime_dir=runtime)
            cleanup.socket_path.parent.mkdir(parents=True, exist_ok=True)
            socket_value = daemon.socket.socket(daemon.socket.AF_UNIX)
            try:
                socket_value.bind(str(cleanup.socket_path))
                socket_stat = cleanup.socket_path.lstat()
                cleanup._socket_identity = (
                    socket_stat.st_dev,
                    socket_stat.st_ino,
                )
                with mock.patch.object(
                    Path,
                    "unlink",
                    side_effect=OSError("socket unlink"),
                ):
                    cleanup._remove_owned_paths()
            finally:
                socket_value.close()
                if cleanup.socket_path.exists():
                    cleanup.socket_path.unlink()
            cleanup.pidfile_path.write_text("pid", encoding="ascii")
            pid_stat = cleanup.pidfile_path.lstat()
            cleanup._pidfile_owned = True
            cleanup._pidfile_identity = (pid_stat.st_dev, pid_stat.st_ino)
            with mock.patch.object(
                Path,
                "unlink",
                side_effect=OSError("pid unlink"),
            ):
                cleanup._remove_owned_paths()


class CoverageRatchetAdditionalTests(unittest.TestCase):
    def test_remaining_json_validation_branches(self):
        with self.assertRaises(TypeError):
            json_limits.validate_json(1, object())
        assert_rejected(
            self,
            json_limits.parse_json,
            b"4",
            json_limits.JSONLimits(max_integer_bits=2),
        )
        assert_rejected(
            self,
            json_limits.validate_json,
            4,
            json_limits.JSONLimits(max_integer_bits=2),
        )
        assert_rejected(
            self,
            json_limits.validate_json,
            float("nan"),
            json_limits.JSONLimits(),
        )
        assert_rejected(
            self,
            json_limits.validate_json,
            "é",
            json_limits.JSONLimits(max_string_bytes=1),
        )

    def test_process_platform_and_file_guard_branches(self):
        self.assertEqual("abc…", process._snip("abcdef", 4))
        with mock.patch.object(process.sys, "platform", "linux"), mock.patch.object(
            process, "_linux_pid_cwd", return_value="/linux"
        ):
            self.assertEqual("/linux", process._pid_cwd(1))
        with mock.patch.object(process.sys, "platform", "darwin"), mock.patch.object(
            process, "_macos_pid_cwd", return_value="/mac"
        ):
            self.assertEqual("/mac", process._pid_cwd(1))
        with mock.patch.object(
            process.subprocess,
            "run",
            return_value=mock.Mock(returncode=1, stdout=b""),
        ):
            self.assertEqual([], process.running_agent_processes())
        with mock.patch.object(
            process,
            "_strict_run",
            return_value=mock.Mock(
                returncode=0,
                overflow=None,
                cleanup_incomplete=False,
                stdout=b"\nshort\nx y /bin/node\nx 2 /bin/node\n"
                b"1 x /bin/node\n1 2\n",
            ),
        ):
            self.assertEqual([], process._strict_process_rows())
        with mock.patch.object(process.os, "readlink", return_value=""):
            self.assertEqual("", process._linux_pid_cwd(1))
        with mock.patch.object(
            process,
            "_strict_run",
            return_value=mock.Mock(stdout=b"foo\nbar\n"),
        ):
            assert_rejected(self, process._strict_macos_pid_cwd, 1)
        with mock.patch.object(process.os, "open", return_value=9), mock.patch.object(
            process.os, "fstat", side_effect=OSError("gone")
        ), mock.patch.object(process.os, "close") as close:
            assert_rejected(self, process._strict_linux_cwd_identity, 1)
            close.assert_called_once_with(9)
        with mock.patch.object(process.os, "stat", side_effect=OSError("stat")):
            assert_rejected(self, process._candidate_token_identity, "/x", -1)
        with mock.patch.object(
            process.os,
            "stat",
            return_value=mock.Mock(st_mode=process.stat.S_IFDIR),
        ):
            self.assertIsNone(process._candidate_token_identity("/x", -1))
        with mock.patch.object(process.os, "lstat", side_effect=OSError("missing")):
            assert_rejected(
                self,
                process._strict_executable_binding,
                "/missing",
                (1, 2),
            )

    def test_process_node_identity_classification_branches(self):
        deadline = time.monotonic() + 5
        with mock.patch.object(
            process,
            "_strict_command_tokens",
            side_effect=process.ProcessInspectionError(),
        ), mock.patch.object(
            process,
            "_candidate_token_identity",
            return_value=(1, 2),
        ):
            self.assertEqual(
                process._NodeKimiClassification.DEFINITE,
                process._classify_node_kimi_candidate(
                    "node /opt/kimi", (1, 2), deadline
                )[0],
            )
        with mock.patch.object(
            process,
            "_candidate_token_identity",
            side_effect=process.ProcessInspectionError(),
        ):
            self.assertTrue(
                process._node_tokens_have_kimi_hint(
                    ("node", "/opt/entry"), (1, 2), deadline
                )
            )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            regular = root / "regular"
            regular.write_text("x", encoding="utf-8")
            descriptor = os.open(str(root), os.O_RDONLY)
            try:
                with mock.patch.object(
                    process.os,
                    "stat",
                    return_value=mock.Mock(st_mode=process.stat.S_IFLNK),
                ), mock.patch.object(
                    process.os,
                    "open",
                    side_effect=FileNotFoundError(),
                ):
                    self.assertIsNone(
                        process._relative_candidate_token_identity("regular", descriptor)
                    )
                with mock.patch.object(
                    process.os,
                    "stat",
                    return_value=mock.Mock(st_mode=process.stat.S_IFREG),
                ), mock.patch.object(process.os, "open", return_value=9), mock.patch.object(
                    process.os, "fstat", return_value=mock.Mock(st_mode=process.stat.S_IFREG)
                ), mock.patch.object(process.os, "close") as close:
                    self.assertIsNotNone(
                        process._relative_candidate_token_identity("regular", descriptor)
                    )
                    close.assert_called_once_with(9)
            finally:
                os.close(descriptor)

    def test_state_tail_and_remote_aggregation_branches(self):
        tail_state = state._tail_state(
            [
                {"message": {"tool_calls": [{}]}},
                {"role": "tool", "content": []},
            ]
        )
        self.assertEqual("tool_result", tail_state.last_kind)
        self.assertEqual(1, tail_state.unmatched_tools)

        session = daemon.Session("claude", "sid", "/tmp", "", 1)
        engine = state.StateEngine()
        self.assertEqual(daemon.Status.DEAD, engine.infer_status(session))
        callback_errors = []
        adapter = mock.Mock()
        adapter.infer_status.side_effect = RuntimeError("adapter")
        def on_error(stage, error):
            callback_errors.append((stage, error))

        self.assertEqual(
            daemon.Status.DEAD,
            engine.infer_status(session, adapter=adapter, on_error=on_error),
        )
        self.assertEqual("infer_status", callback_errors[0][0])

        hosts = (
            remote_types.RemoteHost("alpha", "ready"),
            remote_types.RemoteHost("beta", "degraded"),
        )
        with mock.patch.object(remote, "MAX_HOSTS", 1):
            assert_rejected(self, remote._selected_hosts, hosts, None)
        with mock.patch.object(remote, "build_zipapp_bytes", return_value=b"zip"):
            def worker(host, *_args, **_kwargs):
                if host.alias == "alpha":
                    return ([{"host": "alpha", "agent": "claude", "session_id": "s"}], None)
                return (None, remote.RemoteFailure("beta", "timeout"))

            with mock.patch.object(remote, "execute_remote_host", side_effect=worker):
                aggregate = remote.aggregate_remote(
                    "list",
                    hosts=hosts,
                    max_workers=2,
                    fleet_timeout=5,
                )
        self.assertEqual(("alpha",), aggregate.succeeded)
        self.assertEqual(("beta",), tuple(item.host for item in aggregate.failures))
        self.assertTrue(aggregate.partial)

    def test_tailer_pool_and_daemon_log_failure_branches(self):
        published = []
        pool = tailer_pool.TailerPool(published.append)
        with mock.patch.object(
            tailer_pool.inspect,
            "signature",
            side_effect=ValueError("signature"),
        ):
            factory = mock.Mock()
            pool.tailer_factory = factory
            pool._make_tailer(
                daemon.Session("claude", "sid", "/tmp", "/tmp/e.jsonl", 1),
                True,
            )
            factory.assert_called_once()
        with mock.patch.object(Path, "stat", side_effect=OSError("gone")):
            self.assertFalse(
                pool._has_unread_data(
                    mock.Mock(
                        follower=mock.Mock(path="/missing", offset=0),
                        has_pending_records=False,
                    )
                )
            )
        key = ("claude", "sid")
        exporter = mock.Mock(side_effect=RuntimeError("checkpoint"))
        tailer = mock.Mock(
            export_checkpoint=exporter,
            errors=[],
            close=mock.Mock(),
        )
        pool._tailers[key] = tailer
        pool._paths[key] = "/tmp/e.jsonl"
        pool._drop(key, retain_checkpoint=True)
        self.assertTrue(pool.tail_errors)
        with mock.patch.object(
            pool,
            "should_tail",
            side_effect=RuntimeError("status"),
        ):
            assert_rejected(
                self,
                pool._update,
                key,
                daemon.Session("claude", "sid", "/tmp", "/tmp/e.jsonl", 1),
                initial=True,
                new_session=True,
                now=1,
            )
        instance = daemon.SidecarDaemon(runtime_dir=Path("/tmp/runtime"))
        self.assertEqual("log_error", instance._safe_log_error_code(None))
        self.assertEqual("bad", instance._safe_log_error_code("bad"))
        instance._record_log_error("bad code")
        instance._record_log_error("second")
        self.assertEqual("log_error", instance.log_error)
        logger = mock.Mock(append=mock.Mock(return_value=False), error_code="write")
        instance._daemon_log = logger
        instance._log_event("event")
        self.assertTrue(instance._logging_disabled)
        logger.close.side_effect = RuntimeError("close")
        instance._daemon_log = logger
        instance._close_daemon_log()
        self.assertIsNone(instance._daemon_log)

    def test_inject_output_and_plan_helper_branches(self):
        for value in (None, "nan", float("inf"), 0.5, 1001):
            assert_rejected(self, inject._validate_timeout, value)
        self.assertEqual(1.5, inject._validate_timeout(1.5))
        self.assertEqual((b"abc", False), inject._output_bytes("abc", 4))
        self.assertEqual((b"ab", True), inject._output_bytes(b"abc", 2))
        self.assertEqual((b"ab", True), inject._output_bytes(bytearray(b"abc"), 2))
        self.assertEqual((b"", False), inject._output_bytes(object(), 2))
        self.assertEqual("nested", inject._content_text({"content": {"text": "nested"}}))
        self.assertEqual("", inject._content_text("x", depth=9))
        self.assertEqual([], inject._json_values("{bad}\n\nalso bad"))
        self.assertEqual(
            "assistant",
            inject._parse_claude_or_cursor(
                json.dumps({"type": "assistant", "content": "assistant"})
            ),
        )
        self.assertEqual(
            "final",
            inject._parse_codex(
                json.dumps({"type": "agent_message", "text": "message",
                            "last_agent_message": "final"})
            ),
        )

    def test_process_remaining_cleanup_and_binding_branches(self):
        with mock.patch.object(
            process.sys, "platform", "darwin"
        ), mock.patch.object(
            process, "_strict_macos_cwd_identity", return_value=(1, 2, 9)
        ):
            self.assertEqual(
                (1, 2, 9),
                process._strict_candidate_cwd(1, time.monotonic() + 5),
            )
        with mock.patch.object(
            process.os,
            "stat",
            return_value=mock.Mock(st_mode=process.stat.S_IFREG),
        ), mock.patch.object(
            process.os, "open", return_value=9
        ), mock.patch.object(
            process.os,
            "fstat",
            side_effect=OSError("fstat"),
        ), mock.patch.object(process.os, "close") as close:
            assert_rejected(
                self,
                process._relative_candidate_token_identity,
                "entry",
                -1,
            )
            close.assert_called_once_with(9)
        with mock.patch.object(
            process.os,
            "stat",
            return_value=mock.Mock(st_mode=process.stat.S_IFREG),
        ), mock.patch.object(
            process.os, "open", return_value=9
        ), mock.patch.object(
            process.os,
            "fstat",
            return_value=mock.Mock(st_mode=process.stat.S_IFREG, st_dev=3, st_ino=4),
        ), mock.patch.object(
            process.os, "close", side_effect=OSError("close")
        ):
            self.assertEqual(
                (3, 4),
                process._relative_candidate_token_identity("entry", -1),
            )
        with mock.patch.object(
            process,
            "_candidate_token_identity",
            return_value=(1, 2),
        ):
            self.assertEqual(
                process._NodeKimiClassification.DEFINITE,
                process._classify_node_kimi_candidate(
                    "node /absolute/entry", (1, 2), time.monotonic() + 5
                )[0],
            )
        self.assertEqual(
            process._NodeKimiClassification.NEEDS_CWD_IDENTITY,
            process._classify_node_kimi_candidate(
                "node relative.js", (1, 2), time.monotonic() + 5
            )[0],
        )
        with mock.patch.object(
            process,
            "_strict_executable_binding",
            side_effect=[(1, 2, 3), (1, 2, 4)],
        ), mock.patch.object(
            process,
            "_strict_identity",
            return_value=(1, 2),
        ), mock.patch.object(
            process,
            "_strict_canonical_path",
            return_value="/agent",
        ), mock.patch.object(process, "_assert_no_live_kimi_in_project"):
            assert_rejected(
                self,
                process.assert_no_live_kimi_in_project,
                mock.Mock(),
                mock.Mock(),
            )

    def test_remote_failure_and_overflow_branches(self):
        hosts = (
            remote_types.RemoteHost("alpha", "ready"),
            remote_types.RemoteHost("beta", "ready"),
        )
        with mock.patch.object(remote, "build_zipapp_bytes", return_value=b"zip"):
            def raising_worker(host, *_args, **_kwargs):
                if host.alias == "alpha":
                    raise RuntimeError("worker")
                return ([object()], None)

            with mock.patch.object(
                remote, "execute_remote_host", side_effect=raising_worker
            ):
                result = remote.aggregate_remote(
                    "list",
                    hosts=hosts,
                    max_workers=2,
                    fleet_timeout=5,
                )
        self.assertEqual(
            ("alpha", "beta"),
            tuple(item.host for item in result.failures),
        )
        with mock.patch.object(remote, "build_zipapp_bytes", return_value=b"zip"), mock.patch.object(
            remote, "MAX_ROWS", 0
        ):
            with mock.patch.object(
                remote,
                "execute_remote_host",
                return_value=([{"host": "alpha"}], None),
            ):
                overflow = remote.aggregate_remote(
                    "list",
                    hosts=(hosts[0],),
                    max_workers=1,
                    fleet_timeout=5,
                )
        self.assertEqual("resource_limit", overflow.failures[0].code)
        self.assertEqual((), overflow.rows)
        self.assertEqual(
            (),
            remote.aggregate_remote(
                "list",
                hosts=hosts,
                selected=[],
                artifact=b"",
            ).hosts,
        )
        assert_rejected(
            self,
            remote.watch_remote,
            hosts=hosts,
            artifact=b"",
        )
        assert_rejected(self, remote._selected_hosts, hosts, ["ALPHA", "alpha"])

    def test_jsonl_and_cursor_tailer_error_branches(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_bytes(b"123456789")
            follower = tail.JSONLFollower(
                path, from_start=False, max_line_bytes=3
            )
            follower._consume_chunk(b"x\n")
            self.assertEqual([], follower.poll())
            normal = tail.JSONLFollower(
                path, from_start=True, max_line_bytes=32
            )
            normal._consume_chunk(b'{"x": 1}\n')
            self.assertEqual([{"x": 1}], normal.poll())
            follower._records.append({"queued": True})
            checkpoint = follower.export_checkpoint()
            self.assertFalse(
                follower.restore_checkpoint(dict(checkpoint, records=[None]))
            )
            follower._records.clear()
            self.assertEqual([], follower.poll())
            with mock.patch.object(Path, "open", side_effect=OSError("read")):
                self.assertEqual([], follower.poll())

        adapter = mock.Mock(name="adapter")
        adapter.name = "other"
        session = daemon.Session("claude", "sid", "/tmp", "/tmp/e.jsonl", 1)
        tailer = tail.SessionTailer(session, adapter=adapter)
        tailer._is_cursor_chat = True
        tailer.follower = mock.Mock(
            poll=mock.Mock(side_effect=tail.CursorChatError("cursor")),
            last_error=tail.CursorChatError("cursor"),
        )
        self.assertEqual([], tailer._poll_cursor_records())
        tailer.follower.poll.side_effect = None
        tailer.follower.poll.return_value = [{"ok": True}]
        tailer.follower.last_error = None
        self.assertEqual([{"ok": True}], tailer._poll_cursor_records())
        tailer.follower.export_checkpoint.return_value = {
            "kind": "cursor_chat",
            "initialized": False,
        }
        assert_rejected(self, tailer.export_checkpoint)

    def test_daemon_log_and_state_terminal_branches(self):
        instance = daemon.SidecarDaemon(runtime_dir=Path("/tmp/runtime"))
        with mock.patch.object(
            instance,
            "_new_daemon_log",
            side_effect=daemon.DaemonLogError("log_open"),
        ):
            instance._open_daemon_log()
        self.assertEqual("log_open", instance.log_error)
        instance._daemon_log = mock.Mock(
            append=mock.Mock(side_effect=RuntimeError("append"))
        )
        instance._logging_disabled = False
        instance._log_event("event")
        self.assertTrue(instance._logging_disabled)
        instance._daemon_log = mock.Mock(close=mock.Mock(side_effect=OSError("close")))
        instance._close_daemon_log()
        with mock.patch.object(daemon.socket, "socket") as socket_factory:
            listener = socket_factory.return_value
            listener.bind.side_effect = OSError(daemon.errno.EADDRINUSE, "busy")
            with self.assertRaises(daemon.DaemonAlreadyRunning):
                instance._bind_listener()
            listener.close.assert_called()
        instance._serve_thread = threading.current_thread()
        self.assertTrue(instance.stop(0))
        self.assertFalse(
            state.terminal_metadata_is_active(
                {"last-command": "echo", "last-exit-code": "1"}
            )
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "terminal"
            path.write_text("---\nstatus: running\n---\nbody", encoding="utf-8")
            with mock.patch.object(state.Path, "stat", side_effect=OSError("stat")):
                self.assertFalse(state.terminal_file_is_active(path, now=1))

    def test_protocol_and_inject_defensive_branches(self):
        request = acp.KimiAcpRequest(
            executable="/bin/kimi",
            cwd="/tmp",
            session_id="sid",
            request_id="rid",
            message=b"prompt",
            deadline=time.monotonic() + 10,
        )
        protocol = acp._Protocol(mock.Mock(), request)
        protocol.pending.add(1)
        invalid_envelopes = (
            {},
            {"jsonrpc": "1.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "method": 1, "params": {}},
            {"jsonrpc": "2.0", "method": "notice"},
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
        )
        for envelope in invalid_envelopes:
            with self.subTest(envelope=envelope):
                assert_rejected(
                    self,
                    protocol.consume,
                    envelope,
                    expected_id=1,
                )
        for value in (
            {"type": "agent_message_chunk", "content": {"type": "text", "text": "x"}},
            {"type": "plan", "entries": []},
            {"type": "session_info_update"},
            {"type": "usage_update", "size": 1, "used": 1, "cost": None},
        ):
            value = dict(value, sessionUpdate=value["type"])
            value.pop("type")
            acp._validate_update(value)

        error = inject.SendError("invalid_plan", detail="detail")
        self.assertEqual(
            {"code": "invalid_plan", "detail": "detail"},
            error.to_dict(),
        )
        self.assertEqual(
            ("/tmp/e.jsonl",),
            inject._source_paths(
                inject.Session(
                    "cursor-cli",
                    "sid",
                    "/tmp",
                    "/tmp/e.jsonl",
                    1,
                    extra={"transcript_kind": "cursor-chat-sqlite", "wal": "/tmp/e.jsonl"},
                ),
                "/tmp/e.jsonl",
            ),
        )
        self.assertEqual("", inject._content_text({"text": ""}))
        self.assertEqual(
            "fallback",
            inject._assistant_record_text(
                {"role": "assistant", "content": "fallback"}
            ),
        )
        self.assertEqual(
            "two",
            inject._parse_claude_or_cursor(
                json.dumps(
                    [
                        {"type": "assistant", "content": {"text": "one"}},
                        {"type": "assistant", "content": {"text": "two"}},
                    ]
                )
            ),
        )
        self.assertEqual(
            "m",
            inject._parse_codex(
                json.dumps({"item": {"type": "assistant", "text": "m"}})
            ),
        )

    def test_protocol_cleanup_and_tail_watch_branches(self):
        result = mock.Mock(
            returncode=0,
            clean_exit=True,
            cleanup_complete=True,
            stdout_bytes_read=2,
            stderr=b"err",
        )
        process_value = mock.Mock(result=result, stderr=b"stderr")
        self.assertEqual(
            (0, True, True, 2, 3),
            acp._process_snapshot(process_value, None),
        )
        self.assertEqual(
            (0, True, True, 2, 3),
            acp._process_snapshot(None, result),
        )
        process_value.result = None
        self.assertEqual(
            (None, False, False, 0, 6),
            acp._process_snapshot(process_value, None),
        )
        process_value.result = result
        process_value.write_line.side_effect = acp.BoundedDuplexLineProcessError(
            "cancel"
        )
        process_value.wait_clean.side_effect = acp.BoundedDuplexLineProcessError(
            "wait"
        )
        process_value.terminate_tree.side_effect = acp.BoundedDuplexLineProcessError(
            "terminate"
        )
        process_value.result = result
        self.assertIs(
            result,
            acp._bounded_cleanup(
                process_value,
                acp.PromptWriteBoundary.COMPLETE,
                "sid",
                time.monotonic,
            ),
        )
        self.assertTrue(process_value.close_stdin.called)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events"
            path.write_bytes(b"data")
            follower = tail.JSONLFollower(path, from_start=False)
            follower._consume_chunk(b"partial")
            self.assertTrue(follower._pending)
            follower._consume_chunk(b"\n")
            self.assertFalse(follower._pending)
        callback = mock.Mock()
        self.assertEqual(
            [],
            list(tail.watch_sessions([], on_ready=callback)),
        )
        callback.assert_not_called()
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(
            [],
            list(tail.watch_sessions([], cancel_event=cancel, on_ready=callback)),
        )


class SendAuditExceptionalBranchTests(unittest.TestCase):
    def test_namespace_close_and_lock_lifecycle_edges(self):
        namespace = audit.AuditNamespace(
            audit.SendAuditStore(),
            [10, 11],
            [],
            1,
            2,
            "runtime",
            12,
            "marker",
            13,
            "transaction",
            {"namespace_epoch": "e_test"},
            (1,),
            14,
            b"k",
            (2,),
            True,
        )
        with mock.patch.object(audit.os, "close") as close, mock.patch.object(
            audit.fcntl, "flock"
        ):
            namespace.close()
        self.assertEqual(
            [mock.call(14), mock.call(12), mock.call(13), mock.call(11), mock.call(10)],
            close.call_args_list,
        )
        namespace.close()
        self.assertEqual(5, close.call_count)

        empty = audit.AuditNamespace(
            audit.SendAuditStore(),
            [],
            [],
            1,
            2,
            "runtime",
            None,
            "marker",
            None,
            "transaction",
            {"namespace_epoch": "e_test"},
            (1,),
            None,
            b"k",
            (2,),
            False,
        )
        empty.close()
        with self.assertRaises(audit.AuditError):
            with audit.SendAuditStore()._locked(
                mock.Mock(transaction_fd=None)
            ):
                pass

    def test_locked_context_translates_unlock_failures(self):
        namespace = mock.Mock(transaction_fd=9)
        namespace.validate.return_value = None
        with mock.patch.object(
            audit.fcntl,
            "flock",
            side_effect=(None, OSError("unlock")),
        ):
            with audit.SendAuditStore()._locked(namespace):
                pass
        self.assertEqual(3, namespace.validate.call_count)

    def test_exception_only_filesystem_boundaries_preserve_base_exceptions(self):
        helper = mock.Mock()
        helper._validate_lock_file.side_effect = KeyboardInterrupt()
        with mock.patch.object(audit, "_secure_helpers", return_value=helper):
            with self.assertRaises(KeyboardInterrupt):
                audit._validate_named_file(1, "name", 2)

        with mock.patch.object(
            audit,
            "_validate_named_file",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                audit._read_file(1, "name", 2)

        namespace = audit.AuditNamespace(
            audit.SendAuditStore(),
            [1],
            [],
            1,
            2,
            "runtime",
            3,
            "marker",
            4,
            "transaction",
            {"namespace_epoch": "e_test"},
            (1,),
            5,
            b"k",
            (2,),
            True,
        )
        helper._validate_directory.side_effect = KeyboardInterrupt()
        with mock.patch.object(audit, "_secure_helpers", return_value=helper):
            with mock.patch.object(audit.os, "fstat", return_value=mock.Mock()):
                with self.assertRaises(KeyboardInterrupt):
                    namespace.validate()

    def test_open_existing_runtime_rejects_root_without_components(self):
        helper = mock.Mock()
        helper._runtime_root.return_value = Path("/")
        with self.assertRaises(audit.AuditError):
            audit.SendAuditStore._open_existing_runtime(helper, None)

    def test_tailer_pool_normalizes_scalar_errors(self):
        published = []
        pool = tailer_pool.TailerPool(published.append)
        tailer = mock.Mock(errors="scalar-error")
        pool._consume_tailer_errors(("agent", "session"), tailer)
        self.assertEqual(
            [{"agent": "agent", "session_id": "session", "code": "tail_error"}],
            list(pool._tail_errors),
        )

    def test_daemon_log_error_records_first_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance = daemon.SidecarDaemon(
                scanner=mock.Mock(),
                runtime_dir=Path(temporary),
            )
            with mock.patch.object(daemon.sys, "stderr") as stderr:
                instance._record_log_error("audit_error")
            self.assertEqual("audit_error", instance._log_error)
            self.assertEqual(
                ("daemon log unavailable (audit_error)",),
                instance._shutdown_diagnostics,
            )
            stderr.write.assert_called_once()

    def test_daemon_log_open_removes_extra_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance = daemon_log.DaemonLog(
                Path(temporary),
                backups=0,
            )
            instance._open_directory = mock.Mock(return_value=10)
            instance._validate_entry = mock.Mock()
            instance._entry_stat = mock.Mock(return_value=mock.Mock())
            instance._open_private_file = mock.Mock(
                side_effect=((11, (1, 1)), (12, (2, 2)))
            )
            instance._repair_current = mock.Mock()
            with mock.patch.object(daemon_log._fcntl, "flock"):
                with mock.patch.object(daemon_log.os, "unlink") as unlink:
                    with mock.patch.object(daemon_log.os, "fsync") as fsync:
                        self.assertIs(instance.open(), instance)
            self.assertTrue(unlink.called)
            self.assertTrue(fsync.called)

    def test_inject_parser_and_guard_exception_edges(self):
        self.assertEqual(
            "result",
            inject._assistant_record_text({"result": {"text": "result"}}),
        )
        self.assertEqual(("", ""), inject._codex_record_text(None))
        self.assertEqual(
            ("", ""),
            inject._codex_record_text(
                {"item": {"type": "tool", "content": "ignored"}}
            ),
        )

        plan = mock.Mock()
        plan.target.executable_identity = ("kimi", 1, 2)
        evidence = mock.Mock(project="/tmp/project")
        with self.assertRaises(KeyboardInterrupt):
            inject._guard_kimi_processes(
                plan,
                evidence,
                mock.Mock(side_effect=KeyboardInterrupt()),
            )
        expected = inject.SendError("session_busy")
        with self.assertRaises(inject.SendError) as raised:
            inject._guard_kimi_processes(
                plan,
                evidence,
                mock.Mock(side_effect=expected),
            )
        self.assertIs(expected, raised.exception)

    def test_inject_kimi_file_and_state_edges(self):
        with mock.patch.object(inject.os, "pread", return_value=b"xxx"):
            with self.assertRaises(inject.SendError):
                inject._read_kimi_file(1, 2, 4)

        evidence = mock.Mock(state_generation=mock.Mock(size=1))
        evidence._anchors.descriptor.return_value = 7
        with mock.patch.object(
            inject.os,
            "fstat",
            return_value=mock.Mock(st_size=1),
        ), mock.patch.object(inject, "parse_json", return_value=1):
            self.assertIsNone(inject._kimi_state_reason(evidence))

    def test_send_audit_reset_and_rebind_lock_edges(self):
        store = audit.SendAuditStore("/tmp/runtime")
        anchor = ([10], [], 11, "/tmp/runtime")
        with mock.patch.object(store, "_open_anchor", return_value=anchor), mock.patch.object(
            audit,
            "_open_named_file",
            side_effect=((12, True), (13, True)),
        ), mock.patch.object(
            store,
            "_open_existing_runtime",
            return_value=([], [], None),
        ), mock.patch.object(
            audit, "_validate_named_file"
        ), mock.patch.object(
            audit.os, "fsync"
        ), mock.patch.object(
            audit.os, "unlink"
        ), mock.patch.object(
            audit.os, "close"
        ), mock.patch.object(
            audit.fcntl, "flock"
        ):
            self.assertIsNone(store.reset())

        for flock_calls in (
            [OSError(errno.EAGAIN, "busy")],
            [None, OSError(errno.EAGAIN, "busy"), None],
        ):
            with mock.patch.object(store, "_open_anchor", return_value=anchor), mock.patch.object(
                audit,
                "_open_named_file",
                side_effect=((12, False), (13, False)),
            ), mock.patch.object(
                audit.fcntl, "flock", side_effect=flock_calls
            ), mock.patch.object(
                audit.os, "close"
            ):
                with self.assertRaises(audit.AuditError) as raised:
                    store.reset()
                self.assertEqual("audit_busy", raised.exception.code)

        with mock.patch.object(store, "_open_anchor", return_value=anchor), mock.patch.object(
            audit,
            "_open_named_file",
            return_value=(None, False),
        ), mock.patch.object(audit.os, "close"):
            with self.assertRaises(audit.AuditError) as raised:
                store.rebind()
            self.assertEqual("audit_corrupt", raised.exception.code)

        with mock.patch.object(store, "_open_anchor", return_value=anchor), mock.patch.object(
            audit,
            "_open_named_file",
            return_value=(12, False),
        ), mock.patch.object(
            audit.fcntl,
            "flock",
            side_effect=OSError(errno.EAGAIN, "busy"),
        ), mock.patch.object(
            audit.os, "close"
        ):
            with self.assertRaises(audit.AuditError) as raised:
                store.rebind()
            self.assertEqual("audit_busy", raised.exception.code)

