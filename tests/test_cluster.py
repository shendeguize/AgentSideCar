import argparse
import io
import json
import unittest
from unittest import mock

import sidecar.cli as cli
from sidecar.adapters.claude import _record_string
from sidecar.adapters.copilot import _metadata_string
from sidecar.cluster import cluster_sessions, merge_cluster_results
from sidecar import semantic
from sidecar.semantic import (
    build_semantic_payload,
    redact_semantic_text,
    select_semantic_groups,
)
from sidecar.json_limits import JSONLimits, validate_json


class ClusterTests(unittest.TestCase):
    def test_model_metadata_helpers_prefer_nested_and_nonempty_values(self):
        self.assertEqual(
            "nested-model",
            _record_string({"message": {"model": "nested-model"}}, "model"),
        )
        self.assertEqual(
            "top-level",
            _record_string(
                {"model": "top-level", "message": {"model": "nested-model"}},
                "model",
            ),
        )
        self.assertEqual("", _record_string({"model": "  "}, "model"))
        self.assertEqual(
            "provider",
            _metadata_string({"provider": " provider "}, "model_provider", "provider"),
        )
        self.assertEqual("", _metadata_string({"provider": None}, "provider"))

    def test_cluster_cli_merges_remote_rows_and_optional_semantic_report(self):
        local = {
            "agent": "dsh",
            "session_id": "local-session",
            "project": "/work/local",
            "updated_at": 100.0,
            "extra": {"model": "deepseek"},
        }
        remote = {
            "cluster_id": "remote-cluster",
            "project": "/work/remote",
            "agent": "codex",
            "model": "gpt-5",
            "model_provider": "openai",
            "time_bucket": 60,
            "count": 1,
            "session_ids": ["remote-session"],
            "hosts": ["pod-a"],
        }
        args = argparse.Namespace(
            all=True,
            recent_seconds=None,
            window_seconds=60,
            remote=True,
            host=["pod-a"],
            remote_python_candidates=None,
            semantic=True,
            semantic_rules=("largest", "recent"),
            semantic_max_groups=100,
            json=True,
        )
        scanner = mock.Mock()
        scanner.scan.return_value = [local]
        scanner.errors = []
        client = mock.Mock()
        client.status.side_effect = cli.SidecarClientError("offline")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "run_headless_report", return_value={
            "ok": True,
            "report": "deterministic summary",
        }) as report:
            code = cli._run_cluster(
                args,
                scanner=scanner,
                client=client,
                stdout=stdout,
                stderr=stderr,
                remote_aggregator=lambda *_, **__: {
                    "rows": [remote],
                    "failures": [],
                    "exit_code": 0,
                },
            )
        self.assertEqual(0, code)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(2, len(payload["clusters"]))
        self.assertEqual("deterministic summary", payload["semantic"]["report"])
        report.assert_called_once()

    def test_cluster_cli_keeps_local_rows_when_remote_fails(self):
        args = argparse.Namespace(
            all=False,
            recent_seconds=None,
            window_seconds=60,
            remote=True,
            host=[],
            remote_python_candidates=None,
            semantic=False,
            semantic_rules=("largest", "recent"),
            semantic_max_groups=100,
            json=False,
        )
        scanner = mock.Mock()
        scanner.scan.return_value = [{
            "agent": "dsh",
            "session_id": "local-session",
            "project": "/work/local",
            "updated_at": 100.0,
        }]
        scanner.errors = []
        client = mock.Mock()
        client.status.side_effect = cli.SidecarClientError("offline")
        stdout, stderr = io.StringIO(), io.StringIO()
        code = cli._run_cluster(
            args,
            scanner=scanner,
            client=client,
            stdout=stdout,
            stderr=stderr,
            remote_aggregator=mock.Mock(side_effect=cli.RemoteInventoryError()),
        )
        self.assertEqual(2, code)
        self.assertIn("remote: inventory", stderr.getvalue())

    def test_cluster_cli_prints_rows_and_reports_semantic_fallback(self):
        args = argparse.Namespace(
            all=True,
            recent_seconds=None,
            window_seconds=60,
            remote=False,
            host=[],
            remote_python_candidates=None,
            semantic=True,
            semantic_rules=("largest", "recent"),
            semantic_max_groups=100,
            json=False,
        )
        scanner = mock.Mock()
        scanner.errors = []
        scanner.scan.return_value = [{
            "agent": "dsh",
            "session_id": "local-session",
            "project": "/work/local",
            "updated_at": 100.0,
        }]
        client = mock.Mock()
        client.status.side_effect = cli.SidecarClientError("offline")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
            cli,
            "run_headless_report",
            return_value={"ok": False, "report": None},
        ):
            code = cli._run_cluster(
                args,
                scanner=scanner,
                client=client,
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(0, code)
        self.assertIn("local", stdout.getvalue())
        self.assertIn("semantic: unavailable", stderr.getvalue())

    def test_cluster_cli_reports_remote_failures_before_local_fallback(self):
        args = argparse.Namespace(
            all=False,
            recent_seconds=None,
            window_seconds=60,
            remote=True,
            host=[],
            remote_python_candidates=None,
            semantic=False,
            semantic_rules=("largest", "recent"),
            semantic_max_groups=100,
            json=True,
        )
        scanner = mock.Mock()
        scanner.errors = []
        scanner.scan.return_value = [{
            "agent": "dsh",
            "session_id": "local-session",
            "project": "/work/local",
            "updated_at": 100.0,
        }]
        client = mock.Mock()
        client.status.side_effect = cli.SidecarClientError("offline")
        stdout, stderr = io.StringIO(), io.StringIO()
        code = cli._run_cluster(
            args,
            scanner=scanner,
            client=client,
            stdout=stdout,
            stderr=stderr,
            remote_aggregator=lambda *_, **__: {
                "rows": [],
                "failures": [{"host": "pod-a", "code": "python_too_old"}],
                "exit_code": "not-an-int",
            },
        )
        self.assertEqual(0, code)
        self.assertIn("remote pod-a: python_too_old", stderr.getvalue())
        self.assertEqual(1, len(json.loads(stdout.getvalue())))

    def test_groups_by_project_agent_model_provider_and_time_bucket(self):
        rows = [
            {
                "agent": "codex",
                "session_id": "b",
                "project": "/work/app",
                "updated_at": 100.0,
                "host": "pod-b",
                "extra": {"model": "gpt-5", "model_provider": "openai"},
            },
            {
                "agent": "codex",
                "session_id": "a",
                "project": "/work/app",
                "updated_at": 101.0,
                "host": "pod-a",
                "extra": {"model": "gpt-5", "model_provider": "openai"},
            },
            {
                "agent": "claude",
                "session_id": "c",
                "project": "/work/app",
                "updated_at": 101.0,
                "host": "pod-a",
                "extra": {"model": "sonnet"},
            },
        ]

        clusters = cluster_sessions(rows, window_seconds=60)

        self.assertEqual(2, len(clusters))
        codex = next(item for item in clusters if item["agent"] == "codex")
        self.assertEqual(2, codex["count"])
        self.assertEqual(["a", "b"], codex["session_ids"])
        self.assertEqual(["pod-a", "pod-b"], codex["hosts"])
        self.assertEqual("gpt-5", codex["model"])

    def test_missing_metadata_is_explicit_and_window_is_bounded(self):
        clusters = cluster_sessions(
            [
                {
                    "agent": "dsh",
                    "session_id": "one",
                    "project": "",
                    "updated_at": 0,
                }
            ],
            window_seconds=60,
        )

        self.assertEqual("unknown", clusters[0]["project"])
        self.assertEqual("unknown", clusters[0]["model"])
        with self.assertRaises(ValueError):
            cluster_sessions([], window_seconds=0)

    def test_merges_per_host_rows_without_losing_provenance(self):
        groups = merge_cluster_results(
            [
                {
                    "cluster_id": "same",
                    "project": "/work",
                    "agent": "codex",
                    "model": "gpt-5",
                    "model_provider": "openai",
                    "time_bucket": 0,
                    "count": 1,
                    "session_ids": ["one"],
                    "hosts": [],
                    "host": "pod-a",
                },
                {
                    "cluster_id": "same",
                    "project": "/work",
                    "agent": "codex",
                    "model": "gpt-5",
                    "model_provider": "openai",
                    "time_bucket": 0,
                    "count": 2,
                    "session_ids": ["two", "three"],
                    "hosts": ["pod-b"],
                    "host": "pod-b",
                },
            ]
        )

        self.assertEqual(1, len(groups))
        self.assertEqual(3, groups[0]["count"])
        self.assertEqual(["pod-a", "pod-b"], groups[0]["hosts"])
        self.assertEqual(["one", "three", "two"], groups[0]["session_ids"])

    def test_semantic_payload_redacts_paths_secrets_and_is_bounded(self):
        payload = build_semantic_payload(
            [{"project": "/home/caros/private", "agent": "dsh", "model": "m",
              "count": 1, "hosts": ["pod"]}],
            snippets=["token sk-secret-123456789 /tmp/private"],
        )
        self.assertNotIn("/home/caros/private", payload)
        self.assertNotIn("sk-secret-123456789", payload)
        self.assertLessEqual(len(payload), 8000)
        self.assertEqual("[path]", redact_semantic_text("/tmp/private"))

    def test_semantic_selection_is_rule_driven_and_bounded(self):
        groups = [
            {"cluster_id": "small", "count": 1, "time_bucket": 20, "agent": "z"},
            {"cluster_id": "large", "count": 9, "time_bucket": 10, "agent": "a"},
            {"cluster_id": "recent", "count": 2, "time_bucket": 30, "agent": "m"},
        ]
        selected = select_semantic_groups(
            groups, rules=("largest", "recent"), max_groups=2
        )
        self.assertEqual(["large", "recent"], [item["cluster_id"] for item in selected])
        with self.assertRaises(ValueError):
            select_semantic_groups(groups, rules=("not-a-rule",))
        with self.assertRaises(ValueError):
            select_semantic_groups(groups, max_groups=0)
        with self.assertRaises(ValueError):
            select_semantic_groups(groups, max_groups="not-an-int")

    def test_semantic_selection_supports_all_ordering_rules(self):
        groups = [
            {
                "cluster_id": "one",
                "count": 1,
                "time_bucket": 2,
                "agent": "z",
                "model": "b",
                "project": "/work/z",
            },
            {
                "cluster_id": "two",
                "count": 1,
                "time_bucket": 2,
                "agent": "a",
                "model": "a",
                "project": "/work/a",
            },
        ]
        selected = select_semantic_groups(
            groups,
            rules=("agent", "model", "workspace", "max-groups"),
            max_groups=10,
        )
        self.assertEqual(["two", "one"], [item["cluster_id"] for item in selected])
        with self.assertRaises(ValueError):
            select_semantic_groups(groups, max_groups=True)
        with self.assertRaises(ValueError):
            select_semantic_groups(groups, max_groups=10_001)
        with self.assertRaises(ValueError):
            select_semantic_groups(groups, rules=("agent", "agent"))
        self.assertEqual(
            '{"clusters":[],"snippets":[]}',
            build_semantic_payload([None], snippets=[""]),
        )
        bounded = build_semantic_payload(
            [None],
            snippets=["", "keep this"],
        )
        self.assertEqual('{"clusters":[],"snippets":["keep this"]}', bounded)

    def test_cluster_boundaries_skip_malformed_rows_and_merge_duplicates(self):
        with self.assertRaises(ValueError):
            cluster_sessions([], window_seconds=True)
        with self.assertRaises(ValueError):
            cluster_sessions([], window_seconds="not-a-number")
        with self.assertRaises(ValueError):
            cluster_sessions([], window_seconds=0)
        with self.assertRaises(ValueError):
            cluster_sessions([], window_seconds=float("inf"))

        rows = [
            None,
            {
                "project": None,
                "agent": None,
                "updated_at": True,
                "session_id": "",
                "host": "",
                "extra": [],
            },
            {
                "project": "/work/app",
                "agent": "dsh",
                "updated_at": -1,
                "session_id": "a",
                "host": "pod",
                "extra": {"model": "m"},
            },
            {
                "project": "/work/app",
                "agent": "dsh",
                "updated_at": float("inf"),
                "session_id": "a",
                "host": "pod",
                "extra": {"model": "m"},
            },
        ]
        groups = cluster_sessions(rows, window_seconds=60)
        self.assertEqual(2, len(groups))
        app_group = next(group for group in groups if group["project"] == "/work/app")
        self.assertEqual(["a", "a"], app_group["session_ids"])
        self.assertEqual(["pod"], app_group["hosts"])

        merged = merge_cluster_results(
            [
                None,
                {},
                {"cluster_id": ""},
                {
                    "cluster_id": "same",
                    "count": -1,
                    "session_ids": [1, "a", "a"],
                    "hosts": [1, "pod"],
                    "host": "pod",
                },
                {
                    "cluster_id": "same",
                    "count": True,
                    "session_ids": ["b"],
                    "hosts": [],
                },
            ]
        )
        self.assertEqual(1, len(merged))
        self.assertEqual(0, merged[0]["count"])
        self.assertEqual(["a", "b"], merged[0]["session_ids"])
        self.assertEqual(["pod"], merged[0]["hosts"])

    def test_json_validation_covers_scalar_and_string_limit_paths(self):
        validate_json(1, JSONLimits(max_integer_bits=8))
        validate_json(1.25, JSONLimits())
        validate_json("ok", JSONLimits(max_string_bytes=8))
        with self.assertRaises(ValueError):
            validate_json(256, JSONLimits(max_integer_bits=8))

    def test_headless_report_is_bounded_and_fails_closed(self):
        with mock.patch.object(semantic.shutil, "which", return_value=None), mock.patch.dict(
            semantic.os.environ, {"DSH_BIN": ""}, clear=False
        ):
            self.assertEqual(
                {"ok": False, "error": "dsh headless unavailable", "report": None},
                semantic.run_headless_report("payload"),
            )
        with mock.patch.object(
            semantic.subprocess,
            "run",
            side_effect=OSError("private detail"),
        ):
            self.assertFalse(semantic.run_headless_report("payload", executable="dsh")["ok"])
        with mock.patch.object(
            semantic.subprocess,
            "run",
            side_effect=semantic.subprocess.TimeoutExpired("dsh", 1),
        ):
            self.assertFalse(semantic.run_headless_report("payload", executable="dsh")["ok"])
        failed = mock.Mock(returncode=1, stdout="", stderr="secret")
        with mock.patch.object(semantic.subprocess, "run", return_value=failed):
            self.assertFalse(semantic.run_headless_report("payload", executable="dsh")["ok"])
        empty = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(semantic.subprocess, "run", return_value=empty):
            self.assertFalse(semantic.run_headless_report("payload", executable="dsh")["ok"])
        success = mock.Mock(
            returncode=0,
            stdout="/home/private report sk-secret-123456789",
            stderr="",
        )
        with mock.patch.object(semantic.subprocess, "run", return_value=success):
            result = semantic.run_headless_report("payload", executable="dsh")
        self.assertTrue(result["ok"])
        self.assertNotIn("/home/private", result["report"])
        self.assertNotIn("sk-secret-123456789", result["report"])


if __name__ == "__main__":
    unittest.main()
