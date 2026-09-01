import unittest
from unittest import mock

from sidecar.cluster import cluster_sessions, merge_cluster_results
from sidecar import semantic
from sidecar.semantic import (
    build_semantic_payload,
    redact_semantic_text,
    select_semantic_groups,
)
from sidecar.json_limits import JSONLimits, validate_json


class ClusterTests(unittest.TestCase):
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
