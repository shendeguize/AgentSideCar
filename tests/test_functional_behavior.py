import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidecar import cli
from sidecar.cli import build_parser
from sidecar.index import IncrementalIndex
from sidecar.json_limits import (
    JSONLimitError,
    JSONLimits,
    JSONSyntaxError,
    parse_json,
    validate_json,
)
from sidecar.model import Session
from sidecar.remote import aggregate_remote
from sidecar.send_audit import SendAuditStore, make_audit_identity
from sidecar.tail import JSONLFollower
from sidecar.text_utils import normalize_scalar_text


class FunctionalAuditBehaviorTests(unittest.TestCase):
    def test_watch_lifecycle_and_recent_argument_boundaries(self):
        lifecycle = cli._WatchSourceLifecycle("local", configured=True)
        self.assertFalse(lifecycle.delivered)
        lifecycle.events = 1
        self.assertTrue(lifecycle.delivered)
        with self.assertRaises(cli.argparse.ArgumentTypeError):
            cli._recent_seconds_argument("not-a-number")
        with self.assertRaises(cli.argparse.ArgumentTypeError):
            cli._recent_seconds_argument("0")

    def test_module_entrypoint_delegates_to_cli_main(self):
        with mock.patch("sidecar.cli.main", return_value=0):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_module("sidecar.__main__", run_name="__main__")
        self.assertEqual(0, raised.exception.code)

    def test_json_contract_accepts_normal_scalar_values(self):
        validate_json(7, JSONLimits())
        validate_json(1.25, JSONLimits())
        validate_json("functional", JSONLimits())
        self.assertEqual(7, parse_json("7", JSONLimits()))
        self.assertEqual(1.25, parse_json("1.25", JSONLimits()))
        self.assertEqual("functional", parse_json('"functional"', JSONLimits()))
        validate_json(
            {"integer": 7, "real": 1.25, "text": "functional"},
            JSONLimits(),
        )
        self.assertEqual(
            "\ufffd\ufffd",
            normalize_scalar_text("\ud800\udfff", "replace"),
        )

    def test_request_is_idempotent_and_terminal_receipt_is_replayable(self):
        """FU-AUDIT-001: a request has one durable, replayable outcome."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir(mode=0o700)
            runtime = root / "runtime"
            identity = make_audit_identity(
                agent="claude",
                session_id="session-functional",
                project="/project",
                executable_basename="claude",
                confirmation_mode="allow_write",
                message=b"synthetic-functional-message",
            )
            with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch(
                "sidecar.send_audit.pwd.getpwuid",
                return_value=mock.Mock(pw_dir=str(home)),
            ):
                store = SendAuditStore(runtime)
                self.assertIsNone(store.reserve("functional-request", identity))
                pending = store.reserve("functional-request", identity)
                self.assertEqual("request_pending", pending.outcome)
                stored = store.append_terminal(
                    "functional-request",
                    identity,
                    outcome="failed",
                    delivery="unknown",
                    error="executor_error",
                    returncode=1,
                )
                replay = store.reserve("functional-request", identity)
            self.assertEqual(stored, replay)
            self.assertEqual("unknown", replay.delivery)
            self.assertEqual("executor_error", replay.error)


class FunctionalObservationBehaviorTests(unittest.TestCase):
    def test_public_cli_parser_and_empty_remote_result_are_stable(self):
        """FU-CLI-001: public parser and empty remote result stay compatible."""

        arguments = build_parser().parse_args(["list", "--json"])
        self.assertTrue(arguments.json)
        result = aggregate_remote("status", hosts=(), artifact=b"")
        self.assertEqual((), result.hosts)
        self.assertEqual([], result.to_dict()["rows"])

    def test_json_contract_rejects_oversized_scalars(self):
        with self.assertRaises(JSONLimitError):
            validate_json(256, JSONLimits(max_integer_bits=8))
        with self.assertRaises(JSONSyntaxError):
            validate_json(float("inf"), JSONLimits())
        with self.assertRaises(JSONLimitError):
            validate_json("four", JSONLimits(max_string_bytes=3))

    def test_jsonl_follower_reads_new_records_and_restores_checkpoint(self):
        """FU-TAIL-001: observers resume at the exact durable checkpoint."""

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text('{"kind":"first"}\n', encoding="utf-8")
            follower = JSONLFollower(path, from_start=True, max_records=1)
            self.assertEqual([{"kind": "first"}], follower.poll())
            checkpoint = follower.export_checkpoint()
            with path.open("a", encoding="utf-8") as output:
                output.write('{"kind":"second"}\n')
            self.assertEqual([{"kind": "second"}], follower.poll())

            restored = JSONLFollower(path, from_start=True, max_records=1)
            self.assertTrue(restored.restore_checkpoint(checkpoint))
            self.assertEqual([{"kind": "second"}], restored.poll())

    def test_incremental_index_reports_changed_and_removed_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "events.jsonl"
            transcript.write_text('{"kind":"first"}\n', encoding="utf-8")
            session = Session(
                agent="functional",
                session_id="session-functional",
                project=str(root),
                transcript=str(transcript),
                updated_at=1.0,
                title="Functional session",
                extra={},
            )
            index = IncrementalIndex()
            key = ("functional", "session-functional")
            self.assertEqual({key}, index.update([session]).changed)
            self.assertIn(key, index)
            self.assertIn(key, index.entries)
            self.assertEqual({("functional", "session-functional")}, index.update([]).removed)


if __name__ == "__main__":
    unittest.main()
