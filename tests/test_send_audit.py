import hashlib
import json
import multiprocessing
import os
import queue
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sidecar.send_audit as audit_module
from sidecar.send_audit import (
    AUDIT_FILE_NAME,
    AUDIT_KEY_NAME,
    AUDIT_ROTATED_FILE_NAME,
    AUDIT_ARCHIVE_DIR_NAME,
    MAX_AUDIT_ARCHIVES,
    NAMESPACE_ANCHOR_NAME,
    AuditError,
    SendAuditStore,
    generate_request_id,
    make_audit_identity,
    validate_request_id,
)


def _mode(path):
    return stat.S_IMODE(os.stat(str(path)).st_mode)


def _marker_path(home, runtime):
    return (
        home
        / NAMESPACE_ANCHOR_NAME
        / audit_module._marker_name(str(runtime))
    )


def _reserve_in_process(
    runtime,
    identity,
    request_id,
    ready,
    release,
    output,
    worker_ready=None,
):
    if worker_ready is None:
        ready.wait(5.0)
    else:
        worker_ready.put(os.getpid())
        ready.wait()
    try:
        receipt = SendAuditStore(runtime).reserve(request_id, identity)
        output.put("reserved" if receipt is None else receipt.outcome)
    except BaseException as error:
        output.put("error:" + getattr(error, "code", error.__class__.__name__))
    finally:
        release.set()


def _retained_namespace_worker(
    runtime,
    identity,
    ready,
    go,
    spawned,
    output,
    home_environment=None,
):
    try:
        if home_environment is not None:
            os.environ["HOME"] = home_environment
        with SendAuditStore(runtime).open_namespace() as namespace:
            ready.put(True)
            go.wait(5.0)
            receipt = namespace.reserve("request-runtime-swap", identity)
            if receipt is None:
                with spawned.get_lock():
                    spawned.value += 1
                output.put("spawned")
            else:
                output.put(receipt.outcome)
    except BaseException as error:
        output.put("error:" + getattr(error, "code", error.__class__.__name__))


def _active_namespace_worker(
    runtime,
    ready,
    release,
    output,
    home_environment=None,
):
    try:
        if home_environment is not None:
            os.environ["HOME"] = home_environment
        with SendAuditStore(runtime).open_namespace():
            ready.set()
            release.wait(5.0)
        output.put("closed")
    except BaseException as error:
        output.put("error:" + getattr(error, "code", error.__class__.__name__))


def _long_send_worker(runtime, identity, ready, release, output):
    try:
        store = SendAuditStore(runtime)
        with store.open_reserved(
            "request-long-active",
            identity,
        ) as (namespace, replay):
            if replay is not None:
                output.put("unexpected-replay")
                return
            ready.set()
            release.wait(10.0)
            namespace.append_terminal(
                "request-long-active",
                identity,
                outcome="completed",
                delivery="delivered",
                error=None,
                returncode=0,
            )
        output.put("completed")
    except BaseException as error:
        output.put("error:" + getattr(error, "code", error.__class__.__name__))


class SendAuditStoreTests(unittest.TestCase):
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
        self.runtime = self.root / "runtime"
        self.identity = make_audit_identity(
            agent="claude",
            session_id="session-sensitive",
            project="/project/sensitive",
            executable_basename="claude",
            confirmation_mode="allow_write",
            message=b"message-sensitive",
        )

    def tearDown(self):
        self.account_home.stop()
        self.home_environment.stop()
        self.temporary.cleanup()

    def test_request_id_validation_and_generation(self):
        generated = generate_request_id()

        self.assertEqual(generated, validate_request_id(generated))
        self.assertLessEqual(len(generated.encode("ascii")), 128)
        for invalid in (
            "",
            "-leading",
            "_leading",
            "contains space",
            "雪",
            "x" * 129,
            b"bytes",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AuditError) as raised:
                    validate_request_id(invalid)
                self.assertEqual("invalid_request_id", raised.exception.code)
                if invalid:
                    self.assertNotIn(str(invalid), str(raised.exception))

    def test_identity_hashing_covers_scalar_sequences_and_rejects_unsafe_values(self):
        values = [
            None,
            True,
            False,
            42,
            -7,
            1.25,
            "multilingual-雪",
            b"bytes",
            ("nested", [1, 2]),
        ]
        first = hashlib.sha256()
        second = hashlib.sha256()
        for value in values:
            audit_module._hash_value(first, value)
            audit_module._hash_value(second, value)
        self.assertEqual(first.digest(), second.digest())

        for invalid in (object(), "\ud800", [[[[[[[[[[None]]]]]]]]]]):
            with self.subTest(invalid_type=type(invalid).__name__):
                with self.assertRaises(AuditError) as raised:
                    audit_module._hash_value(hashlib.sha256(), invalid)
                self.assertEqual("audit_error", raised.exception.code)

    def test_identity_and_record_depth_validation_fail_closed(self):
        valid = {
            "agent": "claude",
            "session_id": "session",
            "project": "/project",
            "executable_basename": "claude",
            "confirmation_mode": "allow_write",
            "message": b"message",
        }
        invalid_updates = (
            {"agent": "INVALID AGENT"},
            {"session_id": ""},
            {"project": ""},
            {"executable_basename": "../claude"},
            {"confirmation_mode": "implicit"},
            {"message": "not-bytes"},
        )
        for update in invalid_updates:
            with self.subTest(update=update):
                with self.assertRaises(AuditError) as raised:
                    make_audit_identity(**{**valid, **update})
                self.assertEqual("audit_error", raised.exception.code)

        with self.assertRaises(AuditError) as raised:
            audit_module._record_depth({1: "non-string-key"})
        self.assertEqual("audit_corrupt", raised.exception.code)
        with self.assertRaises(AuditError) as raised:
            audit_module._record_depth([[[[[[[[[[[None]]]]]]]]]]])
        self.assertEqual("audit_corrupt", raised.exception.code)

    def test_low_level_encoding_and_filesystem_failures_are_typed(self):
        self.assertEqual(
            "unsafe_audit",
            audit_module._translate_filesystem_error(
                OSError(audit_module.errno.ELOOP, "loop")
            ).code,
        )
        unsafe_lock = RuntimeError("unsafe")
        unsafe_lock.code = "unsafe_lock"
        self.assertEqual(
            "unsafe_audit",
            audit_module._translate_filesystem_error(unsafe_lock).code,
        )
        self.assertEqual(
            "audit_error",
            audit_module._translate_filesystem_error(OSError(5, "io")).code,
        )

        malformed_payloads = (
            b"{}",
            b"\n",
            b"\xff\n",
            b'{"duplicate":1,"duplicate":2}\n',
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(AuditError) as raised:
                    audit_module._parse_file(payload)
                self.assertEqual("audit_corrupt", raised.exception.code)

        with self.assertRaises(AuditError) as raised:
            audit_module._encoded_record({"unsupported": object()})
        self.assertEqual("audit_error", raised.exception.code)
        with self.assertRaises(AuditError) as raised:
            audit_module._encoded_record(
                {"oversized": "x" * audit_module.MAX_AUDIT_LINE_BYTES}
            )
        self.assertEqual("audit_error", raised.exception.code)
        with mock.patch("sidecar.send_audit.os.write", return_value=0):
            with self.assertRaises(OSError):
                audit_module._write_all(1, b"payload")

        path = self.root / "descriptor"
        path.write_bytes(b"payload")
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            with self.assertRaises(AuditError) as raised:
                audit_module._read_descriptor(descriptor, 1)
            self.assertEqual("audit_corrupt", raised.exception.code)
        finally:
            os.close(descriptor)

        helpers = mock.Mock()
        with mock.patch("sidecar.send_audit._secure_helpers", return_value=helpers):
            helpers._validate_lock_file.side_effect = AuditError("unsafe_audit")
            with self.assertRaises(AuditError) as raised:
                audit_module._validate_named_file(1, "audit", 2)
            self.assertEqual("unsafe_audit", raised.exception.code)
            helpers._validate_lock_file.side_effect = OSError(
                audit_module.errno.EIO,
                "validation failed",
            )
            with self.assertRaises(AuditError) as raised:
                audit_module._validate_named_file(1, "audit", 2)
            self.assertEqual("audit_error", raised.exception.code)

        path = self.root / "short-read"
        path.write_bytes(b"payload")
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            with mock.patch(
                "sidecar.send_audit._validate_named_file"
            ), mock.patch("sidecar.send_audit.os.read", return_value=b""):
                with self.assertRaises(AuditError) as raised:
                    audit_module._read_file(1, "audit", descriptor)
                self.assertEqual("audit_corrupt", raised.exception.code)
        finally:
            os.close(descriptor)

    def test_record_and_marker_schema_semantics_reject_inconsistent_states(self):
        with self.assertRaises(ValueError):
            AuditError("not-a-public-code")
        self.assertEqual(
            {"code": "audit_busy"},
            AuditError("audit_busy").to_dict(),
        )
        self.assertEqual(
            {"code": "audit_corrupt", "detail": "namespace_moved"},
            AuditError("audit_corrupt", detail="namespace_moved").to_dict(),
        )

        pending = {
            "schema_version": audit_module.AUDIT_SCHEMA_VERSION,
            "timestamp": 1.0,
            "request_id": "request-1",
            "namespace_epoch": "e_" + "a" * 32,
            "agent": "claude",
            "target_hmac": "b" * 64,
            "request_hmac": "c" * 64,
            "executable_basename": "claude",
            "confirmation_mode": "allow_write",
            "outcome": "pending",
        }
        self.assertEqual(pending, audit_module._validate_record(pending))
        invalid_request = dict(pending, request_id="-invalid")
        with self.assertRaises(AuditError) as raised:
            audit_module._validate_record(invalid_request)
        self.assertEqual("audit_corrupt", raised.exception.code)

        failed = {
            **pending,
            "outcome": "failed",
            "delivery": "unknown",
            "returncode": 1,
            "error": "native_failure",
        }
        self.assertEqual(failed, audit_module._validate_record(failed))
        for update in (
            {"outcome": "completed", "delivery": "unknown", "returncode": 0, "error": None},
            {"outcome": "failed", "delivery": "delivered"},
            {"returncode": True},
            {"error": "../unsafe"},
        ):
            with self.subTest(update=update):
                with self.assertRaises(AuditError) as raised:
                    audit_module._validate_record({**failed, **update})
                self.assertEqual("audit_corrupt", raised.exception.code)

        marker = {
            "schema_version": audit_module.AUDIT_SCHEMA_VERSION,
            "namespace_epoch": "e_" + "d" * 32,
            "key_fingerprint": "e" * 64,
            "runtime_dev": 1,
            "runtime_ino": 2,
            "key_dev": 3,
            "key_ino": 4,
        }
        encoded = json.dumps(marker).encode("ascii") + b"\n"
        self.assertEqual(marker, audit_module._parse_marker(encoded))
        for invalid in (
            b"[]\n",
            json.dumps({**marker, "runtime_dev": -1}).encode("ascii") + b"\n",
            b"\xff\n",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AuditError) as raised:
                    audit_module._parse_marker(invalid)
                self.assertEqual("audit_corrupt", raised.exception.code)

        second_pending = dict(pending, timestamp=2.0)
        with self.assertRaises(AuditError):
            audit_module._validate_histories([pending, second_pending])
        terminal = {**failed, "request_id": pending["request_id"]}
        with self.assertRaises(AuditError):
            audit_module._validate_histories([pending, terminal, pending])

        namespace = audit_module.AuditNamespace(
            mock.Mock(),
            [],
            [],
            -1,
            -1,
            "runtime",
            1,
            "marker",
            2,
            "transaction",
            marker,
            (),
            3,
            b"key",
            (),
            True,
        )
        namespace.closed = True
        with self.assertRaises(AuditError):
            namespace.validate()
        with self.assertRaises(AuditError):
            namespace.downgrade_marker()
        namespace.close()

        namespace.closed = False
        with mock.patch(
            "sidecar.send_audit.fcntl.flock",
            side_effect=OSError(audit_module.errno.EIO, "lock failed"),
        ):
            with self.assertRaises(AuditError) as raised:
                namespace.downgrade_marker()
        self.assertEqual("audit_error", raised.exception.code)

    def test_pending_terminal_and_replay_receipts(self):
        store = SendAuditStore(self.runtime)

        self.assertIsNone(store.reserve("request-1", self.identity))
        pending = store.reserve("request-1", self.identity)
        self.assertEqual("request_pending", pending.outcome)
        self.assertEqual("unknown", pending.delivery)

        stored = store.append_terminal(
            "request-1",
            self.identity,
            outcome="completed",
            delivery="delivered",
            error=None,
            returncode=0,
        )
        replay = store.reserve("request-1", self.identity)

        self.assertEqual(stored, replay)
        self.assertEqual("completed", replay.outcome)
        self.assertEqual(0, replay.returncode)

    def test_kimi_identity_replays_completed_unknown_without_sensitive_values(self):
        kimi = make_audit_identity(
            agent="kimi",
            session_id="kimi-native-session",
            project="/private/kimi/project",
            executable_basename="kimi",
            confirmation_mode="allow_write",
            message=b"KIMI-AUDIT-PRIVATE",
        )
        store = SendAuditStore(self.runtime)
        self.assertIsNone(store.reserve("request-kimi-completed-unknown", kimi))
        store.append_terminal(
            "request-kimi-completed-unknown",
            kimi,
            outcome="failed",
            delivery="unknown",
            error=None,
            returncode=0,
        )
        completed_unknown = store.reserve("request-kimi-completed-unknown", kimi)

        self.assertIsNone(store.reserve("request-kimi-unknown", kimi))
        store.append_terminal(
            "request-kimi-unknown",
            kimi,
            outcome="failed",
            delivery="unknown",
            error="protocol_error",
            returncode=0,
        )
        unknown = store.reserve("request-kimi-unknown", kimi)
        payload = (self.runtime / AUDIT_FILE_NAME).read_bytes()

        self.assertEqual(
            ("kimi", "failed", "unknown", None, 0),
            (
                completed_unknown.agent,
                completed_unknown.outcome,
                completed_unknown.delivery,
                completed_unknown.error,
                completed_unknown.returncode,
            ),
        )
        self.assertEqual(("kimi", "unknown"), (unknown.agent, unknown.delivery))
        for sensitive in (
            b"kimi-native-session",
            b"/private/kimi/project",
            b"KIMI-AUDIT-PRIVATE",
        ):
            self.assertNotIn(sensitive, payload)

    def test_conflicting_target_fails_without_append(self):
        store = SendAuditStore(self.runtime)
        other = make_audit_identity(
            agent="claude",
            session_id="other-session",
            project="/project/sensitive",
            executable_basename="claude",
            confirmation_mode="allow_write",
            message=b"message-sensitive",
        )
        self.assertIsNone(store.reserve("request-conflict", self.identity))
        before = (self.runtime / AUDIT_FILE_NAME).read_bytes()

        with self.assertRaises(AuditError) as raised:
            store.reserve("request-conflict", other)

        self.assertEqual("request_conflict", raised.exception.code)
        self.assertEqual(before, (self.runtime / AUDIT_FILE_NAME).read_bytes())

    def test_changed_message_conflicts_but_same_message_replays(self):
        store = SendAuditStore(self.runtime)
        changed = make_audit_identity(
            agent="claude",
            session_id="session-sensitive",
            project="/project/sensitive",
            executable_basename="other-executable",
            confirmation_mode="allow_write",
            message=b"different-message",
        )
        same = make_audit_identity(
            agent="claude",
            session_id="session-sensitive",
            project="/project/sensitive",
            executable_basename="other-executable",
            confirmation_mode="allow_write",
            message=b"message-sensitive",
        )
        self.assertIsNone(store.reserve("request-message", self.identity))
        self.assertEqual(
            "request_pending",
            store.reserve("request-message", same).outcome,
        )
        with self.assertRaises(AuditError) as raised:
            store.reserve("request-message", changed)
        self.assertEqual("request_conflict", raised.exception.code)

    def test_files_and_directory_are_private_and_symlinks_fail_closed(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-private", self.identity)

        self.assertEqual(0o700, _mode(self.runtime))
        self.assertEqual(0o600, _mode(self.runtime / AUDIT_FILE_NAME))
        self.assertEqual(0o600, _mode(self.runtime / AUDIT_KEY_NAME))
        marker = _marker_path(self.home, self.runtime)
        self.assertEqual(0o600, _mode(marker))
        self.assertEqual(
            hashlib.sha256(str(self.runtime).encode("utf-8")).hexdigest(),
            marker.name,
        )

        victim = self.root / "victim"
        victim.write_text("unchanged", encoding="utf-8")
        audit_path = self.runtime / AUDIT_FILE_NAME
        audit_path.unlink()
        audit_path.symlink_to(victim)
        with self.assertRaises(AuditError) as raised:
            store.reserve("request-symlink", self.identity)
        self.assertEqual("unsafe_audit", raised.exception.code)
        self.assertEqual("unchanged", victim.read_text(encoding="utf-8"))

    def test_home_anchor_mode_and_symlink_fail_closed(self):
        anchor = self.home / NAMESPACE_ANCHOR_NAME
        anchor.mkdir(mode=0o700)
        anchor.chmod(0o755)
        with self.assertRaises(AuditError) as raised:
            SendAuditStore(self.runtime).reserve(
                "request-anchor-mode",
                self.identity,
            )
        self.assertEqual("unsafe_audit", raised.exception.code)

        anchor.chmod(0o700)
        anchor.rmdir()
        victim = self.root / "anchor-victim"
        victim.mkdir(mode=0o700)
        anchor.symlink_to(victim, target_is_directory=True)
        with self.assertRaises(AuditError) as raised:
            SendAuditStore(self.runtime).reserve(
                "request-anchor-symlink",
                self.identity,
            )
        self.assertEqual("unsafe_audit", raised.exception.code)

        anchor.unlink()
        self.home.chmod(0o777)
        with self.assertRaises(AuditError) as raised:
            SendAuditStore(self.runtime).reserve(
                "request-home-mode",
                self.identity,
            )
        self.assertEqual("unsafe_audit", raised.exception.code)
        self.home.chmod(0o700)

    def test_account_home_symlink_owner_and_unavailable_fail_closed(self):
        account_link = self.root / "account-home-link"
        account_link.symlink_to(self.home, target_is_directory=True)
        with mock.patch(
            "sidecar.send_audit.pwd.getpwuid",
            return_value=mock.Mock(pw_dir=str(account_link)),
        ):
            with self.assertRaises(AuditError) as raised:
                SendAuditStore(self.runtime).reserve(
                    "request-account-home-link",
                    self.identity,
                )
        self.assertEqual("unsafe_audit", raised.exception.code)

        real_fstat = os.fstat
        home_details = os.stat(self.home)

        def wrong_home_owner(descriptor):
            details = real_fstat(descriptor)
            if (details.st_dev, details.st_ino) == (
                home_details.st_dev,
                home_details.st_ino,
            ):
                values = list(details)
                values[4] = os.geteuid() + 1
                return os.stat_result(values)
            return details

        with mock.patch(
            "sidecar.send_audit.os.fstat",
            side_effect=wrong_home_owner,
        ):
            with self.assertRaises(AuditError) as raised:
                SendAuditStore(self.runtime).reserve(
                    "request-account-home-owner",
                    self.identity,
                )
        self.assertEqual("unsafe_audit", raised.exception.code)

        with mock.patch.object(audit_module, "pwd", None):
            with self.assertRaises(AuditError) as raised:
                SendAuditStore(self.runtime).reserve(
                    "request-account-home-unavailable",
                    self.identity,
                )
        self.assertEqual("unsafe_audit", raised.exception.code)

    def test_marker_symlink_fails_and_empty_crash_state_recovers(self):
        anchor = self.home / NAMESPACE_ANCHOR_NAME
        anchor.mkdir(mode=0o700)
        marker_name = audit_module._marker_name(str(self.runtime))
        marker = anchor / marker_name
        marker.write_bytes(b"")
        marker.chmod(0o600)
        with SendAuditStore(self.runtime).open_namespace():
            pass
        self.assertTrue(self.runtime.is_dir())
        self.assertTrue((self.runtime / AUDIT_KEY_NAME).is_file())

        marker.unlink()
        SendAuditStore(self.runtime).reserve(
            "request-marker-symlink",
            self.identity,
        )
        victim = self.root / "marker-victim"
        victim.write_bytes(marker.read_bytes())
        victim.chmod(0o600)
        marker.unlink()
        marker.symlink_to(victim)
        with self.assertRaises(AuditError) as raised:
            SendAuditStore(self.runtime).reserve(
                "request-marker-after-symlink",
                self.identity,
            )
        self.assertEqual("unsafe_audit", raised.exception.code)

    def test_marker_mode_fails_closed(self):
        store = SendAuditStore(self.runtime)
        with store.open_namespace():
            pass
        marker = _marker_path(self.home, self.runtime)
        marker.chmod(0o644)
        with self.assertRaises(AuditError) as raised:
            store.reserve("request-marker-mode", self.identity)
        self.assertEqual("unsafe_audit", raised.exception.code)

        marker.chmod(0o600)

    def test_partial_marker_recovers_only_without_records(self):
        recover_runtime = self.root / "partial-recover"
        recover_runtime.mkdir(mode=0o700)
        key = b"k" * 32
        (recover_runtime / AUDIT_KEY_NAME).write_bytes(key)
        (recover_runtime / AUDIT_KEY_NAME).chmod(0o600)
        (recover_runtime / AUDIT_FILE_NAME).write_bytes(b"")
        (recover_runtime / AUDIT_FILE_NAME).chmod(0o600)
        marker = _marker_path(self.home, recover_runtime)
        marker.parent.mkdir(mode=0o700, exist_ok=True)
        marker.write_bytes(b'{"schema_version":')
        marker.chmod(0o600)

        with SendAuditStore(recover_runtime).open_namespace():
            pass
        self.assertEqual(key, (recover_runtime / AUDIT_KEY_NAME).read_bytes())
        self.assertEqual(
            audit_module.AUDIT_SCHEMA_VERSION,
            json.loads(marker.read_text(encoding="ascii"))["schema_version"],
        )

        reject_runtime = self.root / "partial-reject"
        reject_runtime.mkdir(mode=0o700)
        (reject_runtime / AUDIT_KEY_NAME).write_bytes(b"q" * 32)
        (reject_runtime / AUDIT_KEY_NAME).chmod(0o600)
        (reject_runtime / AUDIT_FILE_NAME).write_bytes(b"record\n")
        (reject_runtime / AUDIT_FILE_NAME).chmod(0o600)
        reject_marker = _marker_path(self.home, reject_runtime)
        reject_marker.write_bytes(b"{partial")
        reject_marker.chmod(0o600)
        with self.assertRaises(AuditError) as raised:
            with SendAuditStore(reject_runtime).open_namespace():
                self.fail("record-bearing partial marker must fail")
        self.assertEqual("audit_corrupt", raised.exception.code)

    def test_key_rewrite_and_replacement_fail_current_epoch(self):
        store = SendAuditStore(self.runtime)
        with store.open_namespace() as namespace:
            key_path = self.runtime / AUDIT_KEY_NAME
            key_path.write_bytes(b"r" * 32)
            key_path.chmod(0o600)
            with self.assertRaises(AuditError) as raised:
                namespace.reserve("request-key-rewrite", self.identity)
            self.assertEqual("audit_corrupt", raised.exception.code)
        with self.assertRaises(AuditError) as raised:
            store.reserve("request-key-rewrite-new-open", self.identity)
        self.assertEqual("audit_corrupt", raised.exception.code)

        replacement_runtime = self.root / "key-replacement-runtime"
        replacement_store = SendAuditStore(replacement_runtime)
        with replacement_store.open_namespace() as namespace:
            key_path = replacement_runtime / AUDIT_KEY_NAME
            key_path.rename(replacement_runtime / "old-audit.key")
            key_path.write_bytes(b"n" * 32)
            key_path.chmod(0o600)
            with self.assertRaises(AuditError) as raised:
                namespace.reserve("request-key-replacement", self.identity)
            self.assertEqual("unsafe_audit", raised.exception.code)

    def test_key_symlink_fails_closed_and_rotation_keeps_key(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-key", self.identity)
        key_path = self.runtime / AUDIT_KEY_NAME
        original_key = key_path.read_bytes()
        with mock.patch.object(audit_module, "MAX_AUDIT_BYTES", 1300):
            for index in range(8):
                request_id = "key-rotation-{}".format(index)
                store.reserve(request_id, self.identity)
                store.append_terminal(
                    request_id,
                    self.identity,
                    outcome="completed",
                    delivery="delivered",
                    error=None,
                    returncode=0,
                )
        self.assertEqual(original_key, key_path.read_bytes())
        self.assertEqual(32, len(original_key))
        self.assertEqual(0o600, _mode(key_path))

        victim = self.root / "key-victim"
        victim.write_bytes(b"x" * 32)
        victim.chmod(0o600)
        key_path.unlink()
        key_path.symlink_to(victim)
        with self.assertRaises(AuditError) as raised:
            store.reserve("request-key-symlink", self.identity)
        self.assertEqual("unsafe_audit", raised.exception.code)

    def test_wrong_owner_or_mode_fails_closed(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-mode", self.identity)
        audit_path = self.runtime / AUDIT_FILE_NAME
        audit_path.chmod(0o644)

        with self.assertRaises(AuditError) as raised:
            store.reserve("request-next", self.identity)

        self.assertEqual("unsafe_audit", raised.exception.code)

    def test_corrupt_duplicate_oversize_and_nested_records_fail_closed(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-corrupt", self.identity)
        audit_path = self.runtime / AUDIT_FILE_NAME
        original = audit_path.read_bytes()
        conflicting = json.loads(original)
        conflicting["target_hmac"] = "f" * 64
        cases = (
            b"{bad json}\n",
            original
            + (
                json.dumps(
                    conflicting,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("ascii")
                + b"\n"
            ),
            b'{"schema_version":1,"schema_version":1}\n',
            b'{"nested":{"too":{"deep":{"value":1}}}}\n',
            b"x" * (audit_module.MAX_AUDIT_BYTES + 1),
        )
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                audit_path.write_bytes(payload)
                audit_path.chmod(0o600)
                with self.assertRaises(AuditError) as raised:
                    store.reserve("request-new", self.identity)
                self.assertEqual("audit_corrupt", raised.exception.code)
        audit_path.write_bytes(original)
        audit_path.chmod(0o600)

    def test_rotation_retains_only_current_and_one_rotation(self):
        store = SendAuditStore(self.runtime)
        with mock.patch.object(audit_module, "MAX_AUDIT_BYTES", 1300):
            for index in range(8):
                request_id = "rotation-{}".format(index)
                store.reserve(request_id, self.identity)
                if index < 7:
                    store.append_terminal(
                        request_id,
                        self.identity,
                        outcome="completed",
                        delivery="delivered",
                        error=None,
                        returncode=0,
                    )

            current = self.runtime / AUDIT_FILE_NAME
            rotated = self.runtime / AUDIT_ROTATED_FILE_NAME
            self.assertTrue(current.is_file())
            self.assertTrue(rotated.is_file())
            self.assertLessEqual(current.stat().st_size, 1300)
            self.assertLessEqual(rotated.stat().st_size, 1300)
            self.assertEqual(0o600, _mode(current))
            self.assertEqual(0o600, _mode(rotated))
            self.assertEqual(
                "request_pending",
                store.reserve("rotation-7", self.identity).outcome,
            )

    def test_pending_caps_fail_closed_before_append(self):
        store = SendAuditStore(self.runtime)
        with mock.patch.object(
            audit_module,
            "MAX_ACTIVE_PENDING_RECORDS",
            1,
        ):
            self.assertIsNone(store.reserve("pending-cap-1", self.identity))
            before = (self.runtime / AUDIT_FILE_NAME).read_bytes()
            with self.assertRaises(AuditError) as raised:
                store.reserve("pending-cap-2", self.identity)
            self.assertEqual("audit_error", raised.exception.code)
            self.assertEqual(
                before,
                (self.runtime / AUDIT_FILE_NAME).read_bytes(),
            )
        with mock.patch.object(
            audit_module,
            "MAX_ACTIVE_PENDING_BYTES",
            1,
        ):
            with self.assertRaises(AuditError) as raised:
                store.reserve("pending-byte-cap", self.identity)
            self.assertEqual("audit_error", raised.exception.code)

    def test_partial_rotation_recovers_or_discards_safely(self):
        store = SendAuditStore(self.runtime)
        for index in range(3):
            request_id = "rotation-fill-{}".format(index)
            store.reserve(request_id, self.identity)
            store.append_terminal(
                request_id,
                self.identity,
                outcome="completed",
                delivery="delivered",
                error=None,
                returncode=0,
            )
        store.reserve("rotation-survivor", self.identity)
        current_size = (self.runtime / AUDIT_FILE_NAME).stat().st_size
        with mock.patch.object(
            audit_module,
            "MAX_AUDIT_BYTES",
            current_size + 1,
        ), mock.patch(
            "sidecar.send_audit.os.replace",
            side_effect=OSError("crash before replace"),
        ):
            with self.assertRaises(AuditError):
                store.reserve("rotation-crash-pending", self.identity)
        self.assertTrue(
            (self.runtime / audit_module.AUDIT_NEXT_FILE_NAME).exists()
        )
        replay = store.reserve("rotation-crash-pending", self.identity)
        self.assertEqual("request_pending", replay.outcome)

        next_path = self.runtime / audit_module.AUDIT_NEXT_FILE_NAME
        next_path.write_bytes(b"{partial")
        next_path.chmod(0o600)
        replay = store.reserve("rotation-survivor", self.identity)
        self.assertEqual("request_pending", replay.outcome)
        self.assertFalse(next_path.exists())

    def test_rotation_recovers_after_current_was_moved(self):
        store = SendAuditStore(self.runtime)
        for index in range(3):
            request_id = "rotation-move-fill-{}".format(index)
            store.reserve(request_id, self.identity)
            store.append_terminal(
                request_id,
                self.identity,
                outcome="completed",
                delivery="delivered",
                error=None,
                returncode=0,
            )
        current_size = (self.runtime / AUDIT_FILE_NAME).stat().st_size
        real_replace = os.replace
        replacements = []

        def fail_second_replace(*args, **kwargs):
            replacements.append(True)
            if len(replacements) == 2:
                raise OSError("crash after current move")
            return real_replace(*args, **kwargs)

        with mock.patch.object(
            audit_module,
            "MAX_AUDIT_BYTES",
            current_size + 1,
        ), mock.patch(
            "sidecar.send_audit.os.replace",
            side_effect=fail_second_replace,
        ):
            with self.assertRaises(AuditError):
                store.reserve("rotation-moved-pending", self.identity)
        self.assertFalse((self.runtime / AUDIT_FILE_NAME).exists())
        replay = store.reserve("rotation-moved-pending", self.identity)
        self.assertEqual("request_pending", replay.outcome)

    def test_reservation_fsync_failure_fails_closed(self):
        store = SendAuditStore(self.runtime)
        self.runtime.mkdir(mode=0o700)
        with mock.patch(
            "sidecar.send_audit.os.fsync",
            side_effect=OSError("fsync failed"),
        ):
            with self.assertRaises(AuditError) as raised:
                store.reserve("request-fsync", self.identity)
        self.assertIn(
            raised.exception.code,
            ("audit_error", "unsafe_audit"),
        )

    def test_key_and_audit_parent_fsync_failures_are_typed(self):
        self.runtime.mkdir(mode=0o700)
        runtime_stat = os.stat(self.runtime)
        real_fsync = os.fsync

        def fail_runtime_parent(descriptor):
            details = os.fstat(descriptor)
            if (details.st_dev, details.st_ino) == (
                runtime_stat.st_dev,
                runtime_stat.st_ino,
            ):
                raise OSError("parent fsync failed")
            return real_fsync(descriptor)

        with mock.patch(
            "sidecar.send_audit.os.fsync",
            side_effect=fail_runtime_parent,
        ):
            with self.assertRaises(AuditError) as raised:
                with SendAuditStore(self.runtime).open_namespace():
                    self.fail("namespace must not open")
        self.assertEqual("audit_error", raised.exception.code)

    def test_rotation_parent_fsync_failure_is_typed(self):
        store = SendAuditStore(self.runtime)
        for index in range(3):
            request_id = "completed-before-rotation-{}".format(index)
            store.reserve(request_id, self.identity)
            store.append_terminal(
                request_id,
                self.identity,
                outcome="completed",
                delivery="delivered",
                error=None,
                returncode=0,
            )
        store.reserve("request-before-rotation", self.identity)
        current_size = (self.runtime / AUDIT_FILE_NAME).stat().st_size
        real_fsync = os.fsync
        real_replace = os.replace
        replaced = []

        def note_replace(*args, **kwargs):
            replaced.append(True)
            return real_replace(*args, **kwargs)

        def fail_after_replace(descriptor):
            if replaced:
                raise OSError("rotation parent fsync failed")
            return real_fsync(descriptor)

        with mock.patch.object(
            audit_module,
            "MAX_AUDIT_BYTES",
            current_size + 1,
        ), mock.patch(
            "sidecar.send_audit.os.replace",
            side_effect=note_replace,
        ), mock.patch(
            "sidecar.send_audit.os.fsync",
            side_effect=fail_after_replace,
        ):
            with self.assertRaises(AuditError) as raised:
                store.reserve("request-rotate-fsync", self.identity)
        self.assertTrue(replaced)
        self.assertEqual("audit_error", raised.exception.code)

    def test_audit_bytes_do_not_contain_sensitive_target_values(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-hygiene", self.identity)
        payload = (self.runtime / AUDIT_FILE_NAME).read_bytes()

        self.assertNotIn(b"session-sensitive", payload)
        self.assertNotIn(b"message-sensitive", payload)
        self.assertNotIn(
            hashlib.sha256(b"message-sensitive").hexdigest().encode("ascii"),
            payload,
        )
        self.assertNotIn(
            hashlib.sha256(b"session-sensitive").hexdigest().encode("ascii"),
            payload,
        )
        key = (self.runtime / AUDIT_KEY_NAME).read_bytes()
        self.assertNotIn(key, payload)
        self.assertNotIn(key.hex().encode("ascii"), payload)
        marker_payload = _marker_path(self.home, self.runtime).read_bytes()
        for sensitive in (
            b"request-hygiene",
            b"session-sensitive",
            b"message-sensitive",
            b"/project/sensitive",
        ):
            self.assertNotIn(sensitive, marker_payload)
        record = json.loads(payload)
        self.assertEqual(
            {
                "agent",
                "confirmation_mode",
                "executable_basename",
                "outcome",
                "request_hmac",
                "request_id",
                "schema_version",
                "namespace_epoch",
                "target_hmac",
                "timestamp",
            },
            set(record),
        )

    def test_record_epoch_must_match_marker_epoch(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-epoch", self.identity)
        audit_path = self.runtime / AUDIT_FILE_NAME
        record = json.loads(audit_path.read_text(encoding="utf-8"))
        record["namespace_epoch"] = "e_" + ("x" * 32)
        audit_path.write_text(
            json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        audit_path.chmod(0o600)
        with self.assertRaises(AuditError) as raised:
            store.reserve("request-epoch-next", self.identity)
        self.assertEqual("audit_corrupt", raised.exception.code)

    def test_namespace_closes_all_retained_descriptors(self):
        store = SendAuditStore(self.runtime)
        with store.open_namespace() as namespace:
            descriptors = list(namespace.directory_fds)
            descriptors.extend((namespace.marker_fd, namespace.key_fd))
            self.assertTrue(all(value is not None for value in descriptors))
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_reset_recovers_recreated_missing_and_corrupt_runtime(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-before-reset", self.identity)
        moved = self.root / "reset-moved-runtime"
        self.runtime.rename(moved)
        self.runtime.mkdir(mode=0o700)
        for name, payload in (
            (AUDIT_FILE_NAME, b"corrupt"),
            (AUDIT_ROTATED_FILE_NAME, b"corrupt"),
            (AUDIT_KEY_NAME, b"x" * 32),
        ):
            path = self.runtime / name
            path.write_bytes(payload)
            path.chmod(0o600)
        untouched = self.runtime / "session.lock"
        untouched.write_bytes(b"session-state")
        untouched.chmod(0o600)
        send_locks = self.runtime / "send-locks"
        send_locks.mkdir(mode=0o700)

        archive = store.reset()
        self.assertIsNotNone(archive)
        archive_path = Path(archive)
        self.assertTrue((archive_path / AUDIT_FILE_NAME).exists())
        self.assertTrue((archive_path / AUDIT_KEY_NAME).exists())
        self.assertTrue((archive_path / "namespace.marker").exists())
        for name in (
            AUDIT_FILE_NAME,
            AUDIT_ROTATED_FILE_NAME,
            AUDIT_KEY_NAME,
        ):
            self.assertFalse((self.runtime / name).exists())
        self.assertTrue((moved / AUDIT_FILE_NAME).exists())
        self.assertEqual(b"session-state", untouched.read_bytes())
        self.assertTrue(send_locks.is_dir())
        self.assertFalse(_marker_path(self.home, self.runtime).exists())
        self.assertIsNone(store.reserve("request-after-recreate", self.identity))

        shutil.rmtree(self.runtime)
        store.reset()
        self.assertFalse(self.runtime.exists())
        self.assertIsNone(store.reserve("request-after-missing", self.identity))

        (self.runtime / AUDIT_FILE_NAME).unlink()
        with self.assertRaises(AuditError) as raised:
            store.reserve("request-after-audit-removal", self.identity)
        self.assertEqual("audit_corrupt", raised.exception.code)
        store.reset()
        self.assertIsNone(
            store.reserve("request-after-audit-reset", self.identity)
        )

        (self.runtime / AUDIT_FILE_NAME).write_bytes(b"{bad json}\n")
        (self.runtime / AUDIT_FILE_NAME).chmod(0o600)
        store.reset()
        self.assertIsNone(store.reserve("request-after-corrupt", self.identity))

        marker = _marker_path(self.home, self.runtime)
        marker.write_bytes(b"{corrupt marker}\n")
        marker.chmod(0o600)
        store.reset()
        self.assertIsNone(
            store.reserve("request-after-corrupt-marker", self.identity)
        )

    def test_archive_limit_and_purge_are_explicit_and_bounded(self):
        store = SendAuditStore(self.runtime)
        for index in range(MAX_AUDIT_ARCHIVES):
            store.reserve("request-archive-{}".format(index), self.identity)
            self.assertIsNotNone(store.reset())
        self.assertEqual(
            MAX_AUDIT_ARCHIVES,
            len(list((self.runtime / AUDIT_ARCHIVE_DIR_NAME).iterdir())),
        )
        store.reserve("request-archive-overflow", self.identity)
        with self.assertRaises(AuditError) as raised:
            store.reset()
        self.assertEqual("audit_archive_full", raised.exception.code)
        self.assertTrue((self.runtime / AUDIT_FILE_NAME).exists())
        store.reset(purge=True)
        self.assertEqual([], list((self.runtime / AUDIT_ARCHIVE_DIR_NAME).iterdir()))
        self.assertIsNone(store.reserve("request-after-purge", self.identity))

    def test_empty_reset_and_purge_do_not_create_a_new_lineage(self):
        store = SendAuditStore(self.runtime)
        self.assertIsNone(store.reset())
        self.assertFalse(self.runtime.exists())
        self.assertIsNone(store.reset(purge=True))
        self.assertFalse(self.runtime.exists())

    def test_rebind_preserves_epoch_and_request_id_history_after_move(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-rebind", self.identity)
        replacement = self.root / "replacement-runtime"
        shutil.copytree(self.runtime, replacement)
        old_runtime = self.root / "old-runtime"
        self.runtime.rename(old_runtime)
        replacement.rename(self.runtime)

        with self.assertRaises(AuditError) as raised:
            store.reserve("request-rebind", self.identity)
        self.assertEqual("audit_corrupt", raised.exception.code)
        self.assertEqual("namespace_moved", raised.exception.detail)

        store.rebind()
        replay = store.reserve("request-rebind", self.identity)
        self.assertIsNotNone(replay)
        self.assertEqual("request_pending", replay.outcome)

    def test_rebind_rejects_missing_marker(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-rebind-invalid", self.identity)
        _marker_path(self.home, self.runtime).unlink()
        with self.assertRaises(AuditError) as raised:
            store.rebind()
        self.assertEqual("audit_corrupt", raised.exception.code)

    def test_rebind_rejects_key_replacement_and_bad_history(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-rebind-key", self.identity)
        replacement = self.root / "replacement-key-runtime"
        shutil.copytree(self.runtime, replacement)
        (replacement / AUDIT_KEY_NAME).write_bytes(b"x" * 32)
        (replacement / AUDIT_KEY_NAME).chmod(0o600)
        old_runtime = self.root / "old-key-runtime"
        self.runtime.rename(old_runtime)
        replacement.rename(self.runtime)
        with self.assertRaises(AuditError) as raised:
            store.rebind()
        self.assertEqual("audit_corrupt", raised.exception.code)

        shutil.rmtree(self.runtime)
        shutil.copytree(old_runtime, self.runtime)
        audit_path = self.runtime / AUDIT_FILE_NAME
        audit_path.write_text("{bad json}\n", encoding="utf-8")
        audit_path.chmod(0o600)
        with self.assertRaises(AuditError) as raised:
            store.rebind()
        self.assertEqual("audit_corrupt", raised.exception.code)

        shutil.rmtree(self.runtime)
        shutil.copytree(old_runtime, self.runtime)
        (self.runtime / AUDIT_FILE_NAME).unlink()
        with self.assertRaises(AuditError) as raised:
            store.rebind()
        self.assertEqual("audit_corrupt", raised.exception.code)

        shutil.rmtree(self.runtime)
        with self.assertRaises(AuditError) as raised:
            store.rebind()
        self.assertEqual("audit_corrupt", raised.exception.code)

    def test_purge_refuses_unsafe_archive_entries(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-archive-unsafe", self.identity)
        archive = Path(store.reset())
        unsafe = archive.parent / "unsafe"
        unsafe.symlink_to(archive)
        with self.assertRaises(AuditError) as raised:
            store.reset(purge=True)
        self.assertEqual("unsafe_audit", raised.exception.code)
        self.assertTrue(unsafe.is_symlink())

        unsafe.unlink()
        (archive / "unexpected").write_bytes(b"unsafe")
        (archive / "unexpected").chmod(0o600)
        with self.assertRaises(AuditError) as raised:
            store.reset(purge=True)
        self.assertEqual("unsafe_audit", raised.exception.code)

    def test_archive_helpers_reject_missing_and_unbounded_entries(self):
        helpers = audit_module._secure_helpers()
        directory = self.root / "archive-helper"
        directory.mkdir(mode=0o700)
        directory_fd = os.open(
            str(directory),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            audit_module.SendAuditStore._archive_files(
                directory_fd,
                directory_fd,
                ("missing",),
            )
            for index in range(len(audit_module._ARCHIVE_FILE_NAMES) + 1):
                path = directory / "entry-{}".format(index)
                path.write_bytes(b"x")
                path.chmod(0o600)
            with self.assertRaises(AuditError) as raised:
                audit_module.SendAuditStore._validate_archive_entry(directory_fd)
            self.assertEqual("unsafe_audit", raised.exception.code)

            for path in directory.iterdir():
                path.unlink()
            victim = self.root / "archive-victim"
            victim.write_bytes(b"victim")
            victim.chmod(0o600)
            (directory / "link").symlink_to(victim)
            with self.assertRaises(AuditError) as raised:
                audit_module.SendAuditStore._purge_directory(directory_fd)
            self.assertEqual("unsafe_audit", raised.exception.code)
            (directory / "link").unlink()

            archive_dir = directory / AUDIT_ARCHIVE_DIR_NAME
            archive_dir.mkdir(mode=0o700)
            archive_dir.chmod(0o600)
            with self.assertRaises(AuditError) as raised:
                SendAuditStore(self.runtime)._open_archive_directory(
                    helpers,
                    directory_fd,
                    create=False,
                )
            self.assertEqual("unsafe_audit", raised.exception.code)
        finally:
            os.close(directory_fd)

    def test_reset_refuses_unsafe_symlink_without_partial_mutation(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-before-unsafe-reset", self.identity)
        marker = _marker_path(self.home, self.runtime)
        victim = self.root / "reset-victim"
        victim.write_bytes(b"unchanged")
        victim.chmod(0o600)
        audit_path = self.runtime / AUDIT_FILE_NAME
        audit_path.unlink()
        audit_path.symlink_to(victim)

        with self.assertRaises(AuditError) as raised:
            store.reset()
        self.assertEqual("unsafe_audit", raised.exception.code)
        self.assertTrue(marker.exists())
        self.assertEqual(b"unchanged", victim.read_bytes())

        audit_path.unlink()
        audit_path.write_bytes(b"restored\n")
        audit_path.chmod(0o600)
        marker_victim = self.root / "reset-marker-victim"
        marker_victim.write_bytes(marker.read_bytes())
        marker_victim.chmod(0o600)
        marker.unlink()
        marker.symlink_to(marker_victim)
        with self.assertRaises(AuditError) as raised:
            store.reset()
        self.assertEqual("unsafe_audit", raised.exception.code)
        self.assertTrue((self.runtime / AUDIT_KEY_NAME).exists())

    def test_malformed_schema_types_always_raise_audit_corrupt(self):
        store = SendAuditStore(self.runtime)
        store.reserve("request-types", self.identity)
        record = json.loads(
            (self.runtime / AUDIT_FILE_NAME).read_text(
                encoding="utf-8"
            ).splitlines()[0]
        )
        terminal = dict(record)
        terminal.update(
            {
                "outcome": "failed",
                "delivery": "unknown",
                "error": "native_exit",
                "returncode": 1,
            }
        )
        cases = []
        for field, invalid in (
            ("schema_version", True),
            ("schema_version", 1.0),
            ("timestamp", True),
            ("timestamp", float("nan")),
            ("request_id", []),
            ("agent", {}),
            ("target_hmac", []),
            ("request_hmac", {}),
            ("executable_basename", 7),
            ("confirmation_mode", []),
            ("outcome", []),
        ):
            malformed = dict(record)
            malformed[field] = invalid
            cases.append(malformed)
        for field, invalid in (
            ("delivery", []),
            ("error", []),
            ("returncode", True),
            ("returncode", []),
        ):
            malformed = dict(terminal)
            malformed[field] = invalid
            cases.append(malformed)
        cases.extend(([], set(), b"bytes", "text", 1, True, None))

        for index, malformed in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(AuditError) as raised:
                    audit_module._validate_record(malformed)
                self.assertEqual("audit_corrupt", raised.exception.code)

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork and flock")
    def test_concurrent_processes_reserve_exactly_once(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        output = context.Queue()
        processes = [
            context.Process(
                target=_reserve_in_process,
                args=(
                    self.runtime,
                    self.identity,
                    "request-process",
                    ready,
                    release,
                    output,
                ),
            )
            for _index in range(4)
        ]
        for process in processes:
            process.start()
        ready.set()
        results = [output.get(timeout=5.0) for _process in processes]
        for process in processes:
            process.join(5.0)

        self.assertEqual(1, results.count("reserved"))
        self.assertEqual(3, results.count("request_pending"))
        self.assertTrue(all(process.exitcode == 0 for process in processes))

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork and flock")
    def test_fifty_process_first_use_repetitions_are_bounded(self):
        context = multiprocessing.get_context("fork")
        test_deadline = time.monotonic() + 120.0
        for repetition in range(3):
            runtime = self.root / "first-use-{}".format(repetition)
            ready = context.Event()
            release = context.Event()
            worker_ready = context.Queue()
            output = context.Queue()
            ready_pids = []
            results = []
            processes = [
                context.Process(
                    target=_reserve_in_process,
                    args=(
                        runtime,
                        self.identity,
                        "request-first-use",
                        ready,
                        release,
                        output,
                        worker_ready,
                    ),
                )
                for _index in range(50)
            ]

            def remaining(phase):
                value = test_deadline - time.monotonic()
                if value <= 0:
                    states = [
                        (process.pid, process.exitcode, process.is_alive())
                        for process in processes
                    ]
                    self.fail(
                        "{} exceeded global deadline in repetition {}; "
                        "ready={} results={} states={}".format(
                            phase,
                            repetition,
                            len(ready_pids),
                            len(results),
                            states,
                        )
                    )
                return value

            def receive(channel, phase):
                try:
                    return channel.get(timeout=remaining(phase))
                except queue.Empty:
                    remaining(phase)
                    self.fail("{} ended without a queue result".format(phase))

            try:
                for process in processes:
                    process.start()
                for _process in processes:
                    ready_pids.append(
                        receive(worker_ready, "worker-ready barrier")
                    )
                self.assertEqual(50, len(set(ready_pids)))
                ready.set()
                for _process in processes:
                    results.append(receive(output, "reservation results"))
                for process in processes:
                    process.join(remaining("worker exit"))

                alive = [
                    process.pid for process in processes if process.is_alive()
                ]
                self.assertEqual([], alive)
                self.assertEqual(50, len(results))
                self.assertEqual(1, results.count("reserved"), results)
                self.assertEqual(
                    49,
                    results.count("request_pending"),
                    results,
                )
                self.assertTrue(
                    all(process.exitcode == 0 for process in processes),
                    [
                        (process.pid, process.exitcode)
                        for process in processes
                    ],
                )
            finally:
                ready.set()
                release.set()
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                cleanup_deadline = time.monotonic() + 5.0
                for process in processes:
                    process.join(
                        max(0.0, cleanup_deadline - time.monotonic())
                    )
                for process in processes:
                    if process.is_alive():
                        process.kill()
                kill_deadline = time.monotonic() + 5.0
                for process in processes:
                    process.join(max(0.0, kill_deadline - time.monotonic()))
                survivors = [
                    process.pid for process in processes if process.is_alive()
                ]
                worker_ready.close()
                worker_ready.join_thread()
                output.close()
                output.join_thread()
                for process in processes:
                    if not process.is_alive():
                        process.close()
                if survivors:
                    self.fail(
                        "owned first-use workers survived cleanup: {}".format(
                            survivors
                        )
                    )

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork and flock")
    def test_active_long_send_survives_forced_rotations(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        output = context.Queue()
        with mock.patch.object(audit_module, "MAX_AUDIT_BYTES", 1300):
            process = context.Process(
                target=_long_send_worker,
                args=(
                    self.runtime,
                    self.identity,
                    ready,
                    release,
                    output,
                ),
            )
            process.start()
            self.assertTrue(ready.wait(10.0))
            store = SendAuditStore(self.runtime)
            for index in range(20):
                request_id = "during-long-send-{}".format(index)
                store.reserve(request_id, self.identity)
                store.append_terminal(
                    request_id,
                    self.identity,
                    outcome="completed",
                    delivery="delivered",
                    error=None,
                    returncode=0,
                )
            replay = store.reserve(
                "request-long-active",
                self.identity,
            )
            self.assertEqual("request_pending", replay.outcome)
            conflicting = make_audit_identity(
                agent="claude",
                session_id="session-sensitive",
                project="/project/sensitive",
                executable_basename="claude",
                confirmation_mode="allow_write",
                message=b"different-message",
            )
            with self.assertRaises(AuditError) as raised:
                store.reserve("request-long-active", conflicting)
            self.assertEqual("request_conflict", raised.exception.code)
            self.assertTrue(
                (self.runtime / AUDIT_ROTATED_FILE_NAME).is_file()
            )
            release.set()
            process.join(15.0)
        self.assertEqual("completed", output.get(timeout=5.0))
        replay = SendAuditStore(self.runtime).reserve(
            "request-long-active",
            self.identity,
        )
        self.assertEqual("completed", replay.outcome)

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork and flock")
    def test_retained_namespaces_survive_runtime_replacement_once(self):
        context = multiprocessing.get_context("fork")
        ready = context.Queue()
        go = context.Event()
        spawned = context.Value("i", 0)
        output = context.Queue()
        processes = [
            context.Process(
                target=_retained_namespace_worker,
                args=(
                    self.runtime,
                    self.identity,
                    ready,
                    go,
                    spawned,
                    output,
                ),
            )
            for _index in range(2)
        ]
        for process in processes:
            process.start()
        self.assertTrue(ready.get(timeout=5.0))
        self.assertTrue(ready.get(timeout=5.0))

        moved = self.root / "moved-runtime"
        self.runtime.rename(moved)
        self.runtime.mkdir(mode=0o700)
        go.set()
        results = [output.get(timeout=5.0) for _process in processes]
        for process in processes:
            process.join(5.0)

        self.assertEqual(1, spawned.value)
        self.assertEqual(1, results.count("spawned"))
        self.assertEqual(1, results.count("request_pending"))
        self.assertTrue(all(process.exitcode == 0 for process in processes))

        with self.assertRaises(AuditError):
            SendAuditStore(self.runtime).reserve(
                "request-runtime-swap",
                self.identity,
            )
        replacement = self.runtime
        replacement.rmdir()
        moved.rename(self.runtime)
        replay = SendAuditStore(self.runtime).reserve(
            "request-runtime-swap",
            self.identity,
        )
        self.assertEqual("request_pending", replay.outcome)

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork and flock")
    def test_nested_parent_replacement_uses_home_anchor_once(self):
        context = multiprocessing.get_context("fork")
        outer = self.root / "custom-parent"
        runtime = outer / "nested" / "runtime"
        runtime.parent.mkdir(parents=True, mode=0o700)
        outer.chmod(0o700)
        runtime.parent.chmod(0o700)
        ready = context.Queue()
        go = context.Event()
        spawned = context.Value("i", 0)
        output = context.Queue()
        processes = [
            context.Process(
                target=_retained_namespace_worker,
                args=(
                    runtime,
                    self.identity,
                    ready,
                    go,
                    spawned,
                    output,
                ),
            )
            for _index in range(2)
        ]
        for process in processes:
            process.start()
        self.assertTrue(ready.get(timeout=5.0))
        self.assertTrue(ready.get(timeout=5.0))

        moved = self.root / "moved-custom-parent"
        outer.rename(moved)
        runtime.parent.mkdir(parents=True, mode=0o700)
        outer.chmod(0o700)
        runtime.parent.chmod(0o700)
        runtime.mkdir(mode=0o700)
        go.set()
        results = [output.get(timeout=5.0) for _process in processes]
        for process in processes:
            process.join(5.0)

        self.assertEqual(1, spawned.value)
        self.assertEqual(1, results.count("spawned"))
        self.assertEqual(1, results.count("request_pending"))
        with self.assertRaises(AuditError):
            SendAuditStore(runtime).reserve(
                "request-runtime-swap",
                self.identity,
            )

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork and flock")
    def test_reset_is_busy_while_namespace_is_active(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        output = context.Queue()
        home_a = self.root / "environment-home-a"
        home_b = self.root / "environment-home-b"
        home_a.mkdir(mode=0o700)
        home_b.mkdir(mode=0o700)
        process = context.Process(
            target=_active_namespace_worker,
            args=(self.runtime, ready, release, output, str(home_a)),
        )
        process.start()
        self.assertTrue(ready.wait(5.0))
        marker = _marker_path(self.home, self.runtime)
        before = marker.read_bytes()
        try:
            with mock.patch.dict(os.environ, {"HOME": str(home_b)}):
                with self.assertRaises(AuditError) as raised:
                    SendAuditStore(self.runtime).reset()
            self.assertEqual("audit_busy", raised.exception.code)
            self.assertEqual(before, marker.read_bytes())
        finally:
            release.set()
            process.join(5.0)
        self.assertEqual("closed", output.get(timeout=5.0))

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork and flock")
    def test_home_environment_variants_reserve_exactly_once(self):
        context = multiprocessing.get_context("fork")
        home_a = self.root / "reserve-home-a"
        home_b = self.root / "reserve-home-b"
        home_a.mkdir(mode=0o700)
        home_b.mkdir(mode=0o700)
        ready = context.Queue()
        go = context.Event()
        spawned = context.Value("i", 0)
        output = context.Queue()
        processes = [
            context.Process(
                target=_retained_namespace_worker,
                args=(
                    self.runtime,
                    self.identity,
                    ready,
                    go,
                    spawned,
                    output,
                    str(environment_home),
                ),
            )
            for environment_home in (home_a, home_b)
        ]
        for process in processes:
            process.start()
        self.assertTrue(ready.get(timeout=5.0))
        self.assertTrue(ready.get(timeout=5.0))
        go.set()
        results = [output.get(timeout=5.0) for _process in processes]
        for process in processes:
            process.join(5.0)
        self.assertEqual(1, spawned.value)
        self.assertEqual(1, results.count("spawned"))
        self.assertEqual(1, results.count("request_pending"))


if __name__ == "__main__":
    unittest.main()
