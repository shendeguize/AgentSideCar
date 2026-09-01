"""One birth-time contract across adapters.

Every adapter learns when a session started from a different native shape —
an ISO string in a Claude transcript, epoch milliseconds in a Kimi state
file, a bare number in a DSH index. The board cannot branch per agent, so
each adapter funnels its candidates through ``created_at_extra`` and the
result lands under a single key. These tests pin the key, the parsing, and
the refusal to guess when nothing usable arrived.
"""

import json
import tempfile
import unittest
from pathlib import Path

from sidecar.adapters.base import CREATED_AT_KEY, created_at_extra
from sidecar.adapters.claude import ClaudeAdapter
from sidecar.adapters.codex import CodexAdapter
from sidecar.adapters.copilot import CopilotAdapter


class CreatedAtExtraTests(unittest.TestCase):
    def test_accepts_iso_epoch_seconds_and_epoch_milliseconds_alike(self):
        expected = 1_700_000_000.0
        for label, candidate in (
            ("iso", "2023-11-14T22:13:20+00:00"),
            ("seconds", expected),
            ("milliseconds", expected * 1000),
            ("numeric string", str(expected)),
        ):
            with self.subTest(label):
                self.assertEqual(
                    {CREATED_AT_KEY: expected}, created_at_extra(candidate)
                )

    def test_first_usable_candidate_wins_over_later_ones(self):
        extra = created_at_extra(None, "not a timestamp", 1_700_000_000.0, 1.0)
        self.assertEqual({CREATED_AT_KEY: 1_700_000_000.0}, extra)

    def test_omits_the_key_when_nothing_parses(self):
        # An absent duration is honest; a zero-epoch one would render as a
        # session that started in 1970 and has been running for 50 years.
        for candidate in (None, "", "yesterday", 0, -5, True, {}):
            with self.subTest(repr(candidate)):
                self.assertEqual({}, created_at_extra(candidate))


class AdapterCreatedAtTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_jsonl(self, path, records):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def test_claude_reads_the_birth_time_from_the_first_transcript_record(self):
        self.write_jsonl(
            self.home / ".claude" / "projects" / "project" / "session.jsonl",
            [
                {
                    "type": "user",
                    "sessionId": "session",
                    "cwd": "/work/project",
                    "timestamp": "2023-11-14T22:13:20Z",
                    "message": {"content": "First prompt"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2023-11-14T23:13:20Z",
                    "message": {"content": "Reply"},
                },
            ],
        )

        sessions = list(ClaudeAdapter().discover(self.home))
        self.assertEqual(1, len(sessions))
        self.assertEqual(1_700_000_000.0, sessions[0].extra[CREATED_AT_KEY])

    def test_codex_reads_the_birth_time_from_the_first_rollout_record(self):
        self.write_jsonl(
            self.home
            / ".codex"
            / "sessions"
            / "2023"
            / "11"
            / "14"
            / "rollout-2023-11-14-session.jsonl",
            [
                {
                    "type": "session_meta",
                    "timestamp": "2023-11-14T22:13:20Z",
                    "payload": {"id": "session", "cwd": "/work/project"},
                },
                {"type": "event_msg", "payload": {"type": "agent_message"}},
            ],
        )

        sessions = list(CodexAdapter().discover(self.home))
        self.assertEqual(1, len(sessions))
        self.assertEqual(1_700_000_000.0, sessions[0].extra[CREATED_AT_KEY])

    def test_copilot_accepts_either_metadata_spelling(self):
        for label, key in (("snake", "created_at"), ("camel", "createdAt")):
            with self.subTest(label):
                workspace = (
                    self.home / ".copilot" / "session-state" / label / "workspace.yaml"
                )
                workspace.parent.mkdir(parents=True, exist_ok=True)
                workspace.write_text(
                    f'id: "{label}"\ncwd: "/work/project"\n'
                    f'{key}: "2023-11-14T22:13:20Z"\n',
                    encoding="utf-8",
                )

        sessions = {
            session.session_id: session
            for session in CopilotAdapter().discover(self.home)
        }
        self.assertEqual({"snake", "camel"}, set(sessions))
        for session in sessions.values():
            self.assertEqual(1_700_000_000.0, session.extra[CREATED_AT_KEY])

    def test_a_transcript_without_timestamps_reports_no_birth_time(self):
        self.write_jsonl(
            self.home / ".claude" / "projects" / "project" / "session.jsonl",
            [{"type": "user", "sessionId": "session", "message": {"content": "hi"}}],
        )

        sessions = list(ClaudeAdapter().discover(self.home))
        self.assertNotIn(CREATED_AT_KEY, sessions[0].extra)


if __name__ == "__main__":
    unittest.main()
