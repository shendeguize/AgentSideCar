import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidecar.adapters.base import timestamp_epoch
from sidecar.adapters.copilot import CopilotAdapter, _WORKSPACE_BYTES
from sidecar.model import Session, Status


class CopilotAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.state_root = self.home / ".copilot" / "session-state"
        self.adapter = CopilotAdapter()

    def tearDown(self):
        self.temporary.cleanup()

    def write_workspace(self, session_dir, content, mtime=1_700_000_000.0):
        path = self.state_root / session_dir / "workspace.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def discover_one(self):
        sessions = list(self.adapter.discover(self.home))
        self.assertEqual(1, len(sessions))
        return sessions[0]

    def test_scalar_metadata_parses_quoted_escaped_and_typed_values(self):
        workspace = self.write_workspace(
            "directory-id",
            "\n".join(
                (
                    'id: "session-\\u2603" # stored identifier',
                    "cwd: '/tmp/it''s:project' # current project",
                    'title: "Fix \\"quotes\\" and\\nescaped values # intact"',
                    "summary_count: 7 # generated count",
                    "enabled: TRUE",
                    "missing: null",
                    "tilde: ~",
                    "url: https://example.test/a:b",
                    "nested:",
                    "  ignored: true",
                    "- ignored-list-item",
                )
            )
            + "\n",
        )
        before = (workspace.read_bytes(), workspace.stat().st_mtime_ns)

        session = self.discover_one()

        self.assertEqual("session-☃", session.session_id)
        self.assertEqual("/tmp/it's:project", session.project)
        self.assertEqual('Fix "quotes" and escaped values # intact', session.title)
        self.assertEqual(7, session.extra["summary_count"])
        self.assertIs(session.extra["enabled"], True)
        self.assertIsNone(session.extra["missing"])
        self.assertIsNone(session.extra["tilde"])
        self.assertEqual("https://example.test/a:b", session.extra["url"])
        self.assertNotIn("ignored", session.extra)
        self.assertEqual("workspace", session.extra["source"])
        self.assertEqual(str(workspace), session.extra["workspace"])
        self.assertEqual(before, (workspace.read_bytes(), workspace.stat().st_mtime_ns))

    def test_timestamp_and_identity_fields_prefer_workspace_metadata(self):
        workspace = self.write_workspace(
            "directory-id",
            "\n".join(
                (
                    "id: stored-id",
                    "cwd: /work/current",
                    "git_root: /work/git",
                    "title: Stored title",
                    "updated_at: 2026-08-23T04:05:06.125Z",
                    "created_at: 2026-08-22T01:02:03Z",
                )
            )
            + "\n",
            mtime=123.0,
        )

        session = self.discover_one()

        self.assertEqual("stored-id", session.session_id)
        self.assertEqual("/work/current", session.project)
        self.assertEqual("Stored title", session.title)
        self.assertEqual(
            timestamp_epoch("2026-08-23T04:05:06.125Z"),
            session.updated_at,
        )
        self.assertEqual(
            "2026-08-23T04:05:06.125Z",
            session.extra["updated_at"],
        )
        self.assertEqual(str(workspace), session.extra["workspace"])

    def test_fallback_metadata_fields_and_created_timestamp_are_used(self):
        self.write_workspace(
            "directory-id",
            "\n".join(
                (
                    "id: 7",
                    "session_id: alternate-id",
                    "cwd: null",
                    "git_root: '/work/fallback'",
                    "title: false",
                    "name: Alternate title",
                    "updated_at: not-a-timestamp",
                    "created_at: 1787429063221",
                )
            )
            + "\n",
            mtime=321.0,
        )

        session = self.discover_one()

        self.assertEqual("alternate-id", session.session_id)
        self.assertEqual("/work/fallback", session.project)
        self.assertEqual("Alternate title", session.title)
        self.assertEqual(1787429063.221, session.updated_at)

    def test_invalid_timestamps_fall_back_to_file_mtime_and_directory_id(self):
        self.write_workspace(
            "directory-id",
            "id: false\nupdated_at: invalid\ncreated_at: also-invalid\n",
            mtime=456.0,
        )

        session = self.discover_one()

        self.assertEqual("directory-id", session.session_id)
        self.assertEqual(456.0, session.updated_at)
        self.assertEqual("", session.project)
        self.assertEqual("", session.title)

    def test_malformed_and_invalid_utf8_lines_do_not_hide_valid_metadata(self):
        self.write_workspace(
            "directory-id",
            (
                b"\xff malformed\n"
                b"no-separator\n"
                b": missing-key\n"
                b"  nested: ignored\n"
                b"id: recovered-id\n"
                b"cwd: /work/recovered\n"
                b'title: "invalid\\q escape"\n'
            ),
        )

        session = self.discover_one()

        self.assertEqual("recovered-id", session.session_id)
        self.assertEqual("/work/recovered", session.project)
        self.assertEqual("invalid\\q escape", session.title)

    def test_oversized_truncated_scalar_is_not_used_as_complete_metadata(self):
        prefix = b"id: stable-id\ncwd: /work/stable\ntitle: "
        self.write_workspace(
            "directory-id",
            prefix + (b"x" * _WORKSPACE_BYTES) + b"\n",
        )

        session = self.discover_one()

        self.assertEqual("stable-id", session.session_id)
        self.assertEqual("/work/stable", session.project)
        self.assertEqual("", session.title)
        self.assertNotIn("title", session.extra)

    def test_permission_error_read_degrades_to_directory_metadata(self):
        workspace = self.write_workspace(
            "directory-id",
            "id: stored-id\ncwd: /work/project\ntitle: Stored title\n",
            mtime=789.0,
        )

        with mock.patch("pathlib.Path.open", side_effect=PermissionError("denied")):
            session = self.discover_one()

        self.assertEqual("directory-id", session.session_id)
        self.assertEqual("", session.project)
        self.assertEqual("", session.title)
        self.assertEqual(789.0, session.updated_at)
        self.assertEqual(str(workspace), session.extra["workspace"])

    def test_permission_error_stat_skips_only_unstatable_workspace(self):
        workspace = self.write_workspace(
            "directory-id",
            "id: stored-id\n",
        )
        original_stat = Path.stat

        def stat_with_denial(path, *args, **kwargs):
            if path == workspace:
                raise PermissionError("denied")
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", stat_with_denial):
            sessions = list(self.adapter.discover(self.home))

        self.assertEqual([], sessions)

    def test_normalize_returns_no_events_and_status_is_idle(self):
        session = Session(
            agent="copilot",
            session_id="session-id",
            project="/work/project",
            transcript="",
            updated_at=1_700_000_000.0,
        )

        self.assertEqual([], list(self.adapter.normalize({"type": "message"}, session)))
        self.assertEqual(Status.IDLE, self.adapter.infer_status(session))


if __name__ == "__main__":
    unittest.main()
