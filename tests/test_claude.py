import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidecar.adapters.claude import ClaudeAdapter


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


class ClaudeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.project = self.home / ".claude" / "projects" / "project"

    def tearDown(self):
        self.temporary.cleanup()

    def discover_one(self, path, records):
        write_jsonl(path, records)
        sessions = list(ClaudeAdapter().discover(self.home))
        self.assertEqual(1, len(sessions))
        return sessions[0]

    def test_ai_title_takes_precedence_over_first_user_prompt(self):
        session = self.discover_one(
            self.project / "session.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": "session",
                    "cwd": "/work/project",
                    "message": {"content": "First prompt"},
                },
                {"type": "ai-title", "aiTitle": "Generated title"},
            ],
        )

        self.assertEqual("Generated title", session.title)
        self.assertEqual("/work/project", session.project)

    def test_first_text_prompt_is_used_without_ai_title(self):
        session = self.discover_one(
            self.project / "session.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": "session",
                    "message": {
                        "content": [
                            {"type": "image", "source": "ignored"},
                            {"type": "text", "text": "First text prompt"},
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {"content": "Later prompt"},
                },
            ],
        )

        self.assertEqual("First text prompt", session.title)

    def test_subagent_path_is_authoritative_parent_association(self):
        session = self.discover_one(
            self.project / "parent-session" / "subagents" / "agent-child.jsonl",
            [
                {
                    "type": "user",
                    "isSidechain": True,
                    "sessionId": "conflicting-record-parent",
                    "parentSessionId": "another-parent",
                    "message": {"content": "Child prompt"},
                }
            ],
        )

        self.assertEqual("agent-child", session.session_id)
        self.assertEqual("parent-session", session.parent_id)
        self.assertTrue(session.extra["sidechain"])

    def test_sidechain_explicit_parent_and_orphan_are_not_conflated(self):
        explicit = self.project / "agent-explicit.jsonl"
        conventional = self.project / "agent-conventional.jsonl"
        orphan = self.project / "agent-orphan.jsonl"
        write_jsonl(
            explicit,
            [
                {
                    "type": "user",
                    "isSidechain": True,
                    "sessionId": "agent-explicit",
                    "parentSessionId": "explicit-parent",
                    "message": {"content": "Explicit child"},
                }
            ],
        )
        write_jsonl(
            conventional,
            [
                {
                    "type": "user",
                    "isSidechain": True,
                    "sessionId": "conventional-parent",
                    "message": {"content": "Conventional child"},
                }
            ],
        )
        write_jsonl(
            orphan,
            [
                {
                    "type": "user",
                    "isSidechain": True,
                    "sessionId": "agent-orphan",
                    "parentUuid": "message-not-session",
                    "message": {"content": "Orphan child"},
                }
            ],
        )

        sessions = {
            session.session_id: session
            for session in ClaudeAdapter().discover(self.home)
        }

        self.assertEqual("explicit-parent", sessions["agent-explicit"].parent_id)
        self.assertEqual(
            "conventional-parent",
            sessions["agent-conventional"].parent_id,
        )
        self.assertIsNone(sessions["agent-orphan"].parent_id)
        self.assertTrue(sessions["agent-orphan"].extra["sidechain"])

    def test_invalid_explicit_parent_identifier_is_ignored(self):
        session = self.discover_one(
            self.project / "agent-invalid.jsonl",
            [
                {
                    "type": "user",
                    "isSidechain": True,
                    "sessionId": "agent-invalid",
                    "parentSessionId": "../not-a-session",
                    "message": {"content": "Orphan child"},
                }
            ],
        )

        self.assertIsNone(session.parent_id)

    def test_malformed_and_truncated_rows_degrade_to_valid_metadata(self):
        transcript = self.project / "recovered.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_bytes(
            b"not-json\n"
            + json.dumps(
                {
                    "type": "user",
                    "sessionId": "../invalid",
                    "cwd": 42,
                    "message": {"content": [{"type": "image"}]},
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(
                {
                    "type": "user",
                    "sessionId": "stored-session",
                    "cwd": "/work/recovered",
                    "message": {"content": "Recovered prompt"},
                }
            ).encode("utf-8")
            + b"\n"
            + b'{"type":"ai-title","aiTitle":"truncated'
        )

        sessions = list(ClaudeAdapter().discover(self.home))

        self.assertEqual(1, len(sessions))
        session = sessions[0]
        self.assertEqual("stored-session", session.session_id)
        self.assertEqual("/work/recovered", session.project)
        self.assertEqual("Recovered prompt", session.title)
        self.assertIsNone(session.parent_id)

    def test_permission_error_read_degrades_to_path_metadata(self):
        transcript = self.project / "unreadable.jsonl"
        write_jsonl(
            transcript,
            [
                {
                    "type": "user",
                    "sessionId": "stored-session",
                    "cwd": "/work/project",
                    "message": {"content": "Stored prompt"},
                }
            ],
        )

        with mock.patch(
            "sidecar.adapters.base.open",
            side_effect=PermissionError("denied"),
            create=True,
        ):
            sessions = list(ClaudeAdapter().discover(self.home))

        self.assertEqual(1, len(sessions))
        session = sessions[0]
        self.assertEqual("unreadable", session.session_id)
        self.assertEqual("project", session.project)
        self.assertEqual("", session.title)
        self.assertIsNone(session.parent_id)
        self.assertEqual(
            {"source": "transcript", "sidechain": False},
            session.extra,
        )


if __name__ == "__main__":
    unittest.main()
