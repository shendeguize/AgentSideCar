import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sidecar.kimi_identity as identity
import sidecar.adapters.kimi as kimi_adapter_module
from sidecar.adapters.kimi import KimiAdapter
from sidecar.kimi_identity import (
    KimiIdentityError,
    capture_kimi_identity,
    read_kimi_index_metadata,
    revalidate_kimi_identity,
    revalidate_kimi_identity_after_kimi_start,
)
from sidecar.model import Session


class KimiIdentityFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.kimi_home = self.home / ".kimi-code"
        self.project = self.root / "project"
        self.session_id = "session_native_47"
        self.session_dir = (
            self.kimi_home / "sessions" / "workspace-1" / self.session_id
        )
        self.main_dir = self.session_dir / "agents" / "main"
        for directory in (
            self.home,
            self.kimi_home,
            self.project,
            self.main_dir,
        ):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        self.index_row = {
            "sessionId": self.session_id,
            "sessionDir": str(self.session_dir),
            "workDir": str(self.project),
        }
        self.state = {
            "id": self.session_id,
            "cwd": str(self.project),
            "agents": {"main": {"homedir": str(self.main_dir)}},
            "lastTurnReason": "completed",
            "updatedAt": 1700000000000,
        }
        self.workspace = {
            "workspaces": {
                "workspace-1": {
                    "root": str(self.project),
                }
            }
        }
        self._write_fixture()
        self.environment = mock.patch.dict(
            os.environ,
            {"KIMI_CODE_HOME": str(self.kimi_home)},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    @property
    def index_path(self):
        return self.kimi_home / "session_index.jsonl"

    @property
    def state_path(self):
        return self.session_dir / "state.json"

    @property
    def workspace_path(self):
        return self.kimi_home / "workspaces.json"

    @property
    def wire_path(self):
        return self.main_dir / "wire.jsonl"

    def _write_json(self, path, value):
        path.write_text(
            json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _write_jsonl(self, path, values):
        path.write_text(
            "".join(
                json.dumps(
                    value,
                    ensure_ascii=True,
                    allow_nan=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
                for value in values
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _write_fixture(self):
        self._write_jsonl(self.index_path, [self.index_row])
        self._write_json(self.workspace_path, self.workspace)
        self._write_json(self.state_path, self.state)
        self._write_jsonl(
            self.wire_path,
            [
                {
                    "type": "turn.ended",
                    "agentId": "main",
                    "turnId": 1,
                    "reason": "completed",
                    "time": 1700000000000,
                }
            ],
        )

    def make_session(self, **changes):
        values = {
            "agent": "kimi",
            "session_id": self.session_id,
            "project": str(self.project),
            "transcript": str(self.wire_path),
            "updated_at": 1700000000.0,
            "extra": {
                "source": "session_index",
                "agent_id": "main",
            },
        }
        values.update(changes)
        return Session(**values)

    def replace_state(self, value):
        replacement = self.state_path.with_name("state.next")
        self._write_json(replacement, value)
        os.replace(str(replacement), str(self.state_path))


class KimiIdentityValidTests(KimiIdentityFixture):
    def test_valid_root_captures_agreeing_repr_safe_anchored_evidence(self):
        session = self.make_session()

        evidence = capture_kimi_identity(session, home=self.home)
        try:
            self.assertTrue(evidence.native_root)
            self.assertTrue(evidence.ids_agree)
            self.assertTrue(evidence.projects_agree)
            self.assertEqual("main", evidence.agent_id)
            self.assertEqual("environment", evidence.home_origin)
            self.assertEqual(
                {"adapter", "index", "state", "workspace"},
                {source.kind for source in evidence.project_sources},
            )
            rendered = repr(evidence)
            self.assertNotIn(self.session_id, rendered)
            self.assertNotIn(str(self.home), rendered)
            self.assertNotIn(str(self.project), rendered)
            self.assertNotIn(self.session_id, repr(evidence.project))
        finally:
            evidence.close()
            evidence.close()
        self.assertTrue(evidence.closed)

    def test_adapter_does_not_publish_a_mutation_verification_marker(self):
        sessions = list(KimiAdapter().discover(self.home))

        self.assertEqual(1, len(sessions))
        self.assertEqual(self.session_id, sessions[0].session_id)
        self.assertNotIn("kimi_mutation", sessions[0].extra)

    def test_default_and_environment_home_origins_are_bound(self):
        session = self.make_session()
        with capture_kimi_identity(session, home=self.home) as evidence:
            self.assertEqual("environment", evidence.home_origin)
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(KimiIdentityError) as raised:
                    revalidate_kimi_identity(evidence, session, home=self.home)
        self.assertEqual("session_changed", raised.exception.code)

        with mock.patch.dict(os.environ, {}, clear=True):
            with capture_kimi_identity(session, home=self.home) as evidence:
                self.assertEqual("default", evidence.home_origin)

    def test_permissions_above_kimi_home_are_outside_the_trust_boundary(self):
        self.home.chmod(0o777)

        with capture_kimi_identity(self.make_session(), home=self.home) as evidence:
            self.assertTrue(evidence.native_root)

    def test_project_aliases_agree_by_filesystem_identity(self):
        alias = self.root / "project-alias"
        alias.symlink_to(self.project, target_is_directory=True)
        self.index_row["workDir"] = str(alias)
        self.state["cwd"] = str(self.project)
        self.workspace["workspaces"]["workspace-1"]["root"] = str(alias)
        self._write_fixture()

        with capture_kimi_identity(self.make_session(), home=self.home) as evidence:
            identities = {
                (source.identity.dev, source.identity.ino)
                for source in evidence.project_sources
            }
            self.assertEqual(1, len(identities))

    def test_controlled_kimi_generation_update_rebinds_state_and_appends(self):
        session = self.make_session()
        evidence = capture_kimi_identity(session, home=self.home)
        changed_state = dict(self.state)
        changed_state["updatedAt"] = self.state["updatedAt"] + 1
        self.replace_state(changed_state)
        with self.index_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.index_row, separators=(",", ":")) + "\n")
        with self.wire_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "type": "config_option_update",
                        "agentId": "main",
                        "time": 1700000000001,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )

        refreshed = revalidate_kimi_identity_after_kimi_start(
            evidence,
            session,
            home=self.home,
        )
        try:
            self.assertNotEqual(evidence.state_file.ino, refreshed.state_file.ino)
            self.assertGreater(
                refreshed.root_wire_generation.size,
                evidence.root_wire_generation.size,
            )
        finally:
            refreshed.close()
            evidence.close()

    def test_optional_sources_empty_wire_and_evidence_methods_are_supported(self):
        self.workspace_path.unlink()
        self.index_row.pop("workDir")
        self.state.pop("cwd")
        self._write_jsonl(self.index_path, [self.index_row])
        self._write_json(self.state_path, self.state)
        self.wire_path.write_bytes(b"")
        self.wire_path.chmod(0o600)
        session = self.make_session()

        evidence = capture_kimi_identity(session, home=self.home)
        exact = evidence.revalidate(session, home=self.home)
        refreshed = evidence.revalidate_after_kimi_start(
            session,
            home=self.home,
        )
        try:
            self.assertIsNone(evidence.workspaces_file)
            self.assertEqual(
                ["adapter"],
                [source.kind for source in evidence.project_sources],
            )
            self.assertEqual(evidence, exact)
            self.assertEqual(evidence, refreshed)
        finally:
            exact.close()
            refreshed.close()
            evidence.close()

        with self.assertRaises(KimiIdentityError):
            with evidence:
                self.fail("closed evidence must not be reusable")

    def test_empty_optional_project_values_are_not_identity_sources(self):
        self.index_row["workDir"] = ""
        self.state["cwd"] = None
        self.workspace["workspaces"]["workspace-1"]["root"] = ""
        self._write_fixture()

        with capture_kimi_identity(self.make_session(), home=self.home) as evidence:
            self.assertEqual(
                ["adapter"],
                [source.kind for source in evidence.project_sources],
            )

    def test_adapter_index_and_marker_work_is_linear_for_many_sessions(self):
        rows = []
        for number in range(24):
            session_id = "session_scale_{:02d}".format(number)
            session_dir = (
                self.kimi_home / "sessions" / "workspace-1" / session_id
            )
            main_dir = session_dir / "agents" / "main"
            main_dir.mkdir(mode=0o700, parents=True)
            state = {
                "id": session_id,
                "cwd": str(self.project),
                "agents": {"main": {"homedir": str(main_dir)}},
                "lastTurnReason": "completed",
                "updatedAt": 1700000000000 + number,
            }
            self._write_json(session_dir / "state.json", state)
            self._write_jsonl(main_dir / "wire.jsonl", [])
            rows.append(
                {
                    "sessionId": session_id,
                    "sessionDir": str(session_dir),
                    "workDir": str(self.project),
                }
            )
        self._write_jsonl(self.index_path, rows)
        original_reader = kimi_adapter_module.read_kimi_index_metadata

        with mock.patch(
            "sidecar.adapters.kimi.read_kimi_index_metadata",
            wraps=original_reader,
        ) as index_reader, mock.patch(
            "sidecar.kimi_identity._strict_jsonl",
            side_effect=AssertionError("adapter must not fully parse wires"),
        ):
            sessions = list(KimiAdapter().discover(self.home))

        self.assertEqual(24, len(sessions))
        self.assertEqual(1, index_reader.call_count)
        self.assertTrue(
            all("kimi_mutation" not in session.extra for session in sessions)
        )

    def test_oversized_index_keeps_recent_complete_session_authority(self):
        current_row = self.index_path.read_bytes()
        oversized_old_row = (
            b'{"padding":"'
            + (b"x" * (4 * 1024 * 1024 + 128))
            + b'","sessionId":"session_old"}\n'
        )
        self.index_path.write_bytes(oversized_old_row + current_row)
        self.index_path.chmod(0o600)

        sessions = list(KimiAdapter().discover(self.home))

        self.assertEqual([self.session_id], [session.session_id for session in sessions])
        self.assertNotIn("kimi_mutation", sessions[0].extra)
        with capture_kimi_identity(self.make_session(), home=self.home) as evidence:
            self.assertGreater(evidence.index_content_offset, 0)
            self.assertLessEqual(evidence.index_content_size, 4 * 1024 * 1024)

    def test_index_tail_retains_latest_8192_complete_records_in_order(self):
        old_rows = [
            {"sessionId": "session_old_{:04d}".format(number)}
            for number in range(8192)
        ]
        self._write_jsonl(self.index_path, old_rows + [self.index_row])

        records, valid = read_kimi_index_metadata(self.index_path)
        session_ids = [record["sessionId"] for record in records]

        self.assertTrue(valid)
        self.assertEqual(8192, len(records))
        self.assertEqual("session_old_0001", session_ids[0])
        self.assertEqual(self.session_id, session_ids[-1])
        self.assertNotIn("session_old_0000", session_ids)
        with capture_kimi_identity(self.make_session(), home=self.home):
            pass


class KimiIdentityRejectionTests(KimiIdentityFixture):
    def assert_invalid(self, session=None, code="invalid_session"):
        with self.assertRaises(KimiIdentityError) as raised:
            capture_kimi_identity(session or self.make_session(), home=self.home)
        self.assertEqual(code, raised.exception.code)
        self.assertNotIn(self.session_id, repr(raised.exception))

    def test_missing_state_id_preserves_observation_but_marker_is_false(self):
        del self.state["id"]
        self._write_json(self.state_path, self.state)

        sessions = list(KimiAdapter().discover(self.home))

        self.assertEqual(1, len(sessions))
        self.assertEqual(self.session_id, sessions[0].session_id)
        self.assertNotIn("kimi_mutation", sessions[0].extra)
        self.assert_invalid()

    def test_index_state_and_directory_ids_must_all_agree(self):
        cases = ("state", "directory", "index")
        for case in cases:
            with self.subTest(case=case):
                if case == "state":
                    self.state["id"] = "session_other"
                    self._write_json(self.state_path, self.state)
                elif case == "directory":
                    self.index_row["sessionDir"] = str(self.session_dir.parent)
                    self._write_jsonl(self.index_path, [self.index_row])
                else:
                    self.index_row["sessionId"] = "session_other"
                    self._write_jsonl(self.index_path, [self.index_row])
                self.assert_invalid()
                self.state["id"] = self.session_id
                self.index_row["sessionId"] = self.session_id
                self.index_row["sessionDir"] = str(self.session_dir)
                self._write_fixture()

    def test_all_present_project_sources_must_agree(self):
        other = self.root / "other-project"
        other.mkdir(mode=0o700)
        self.state["cwd"] = str(other)
        self._write_json(self.state_path, self.state)

        self.assert_invalid()
        observed = list(KimiAdapter().discover(self.home))
        self.assertNotIn("kimi_mutation", observed[0].extra)

    def test_child_synthetic_and_non_main_sessions_fail_closed(self):
        cases = (
            self.make_session(parent_id=self.session_id),
            self.make_session(
                session_id=self.session_id + ":worker",
                extra={"source": "session_index", "agent_id": "main"},
            ),
            self.make_session(
                extra={
                    "source": "session_index",
                    "agent_id": "worker",
                    "subagent": True,
                }
            ),
            self.make_session(
                extra={
                    "source": "session_index",
                    "agent_id": "main",
                    "sidechain": True,
                }
            ),
        )
        for session in cases:
            with self.subTest(session=session.session_id):
                self.assert_invalid(session, "child_session")

    def test_remote_markers_fail_closed(self):
        cases = (
            {"source": "remote", "agent_id": "main"},
            {"source": "session_index", "agent_id": "main", "remote": True},
            {"source": "session_index", "agent_id": "main", "host": "remote"},
        )
        for extra in cases:
            with self.subTest(extra=extra):
                self.assert_invalid(
                    self.make_session(extra=extra),
                    "remote_session",
                )

    def test_home_and_identity_file_symlink_leaves_are_rejected(self):
        real_state = self.state_path.with_name("state.real")
        self.state_path.rename(real_state)
        self.state_path.symlink_to(real_state)
        self.assert_invalid()

        self.state_path.unlink()
        real_state.rename(self.state_path)
        alias_home = self.root / "kimi-home-alias"
        alias_home.symlink_to(self.kimi_home, target_is_directory=True)
        with mock.patch.dict(
            os.environ,
            {"KIMI_CODE_HOME": str(alias_home)},
        ):
            self.assert_invalid()

    def test_wrong_owner_and_writable_modes_are_rejected(self):
        actual_uid = os.geteuid()
        with mock.patch(
            "sidecar.kimi_identity.os.geteuid",
            return_value=actual_uid + 1,
        ):
            self.assert_invalid()

        self.state_path.chmod(0o666)
        self.assert_invalid()
        self.state_path.chmod(0o600)
        self.main_dir.chmod(0o777)
        self.assert_invalid()

    def test_malformed_duplicate_nonfinite_deep_and_item_heavy_json_rejects(self):
        valid_tail = (
            ',"cwd":'
            + json.dumps(str(self.project))
            + ',"agents":{"main":{}}}'
        )
        payloads = (
            (
                '{"id":'
                + json.dumps(self.session_id)
                + ',"id":'
                + json.dumps(self.session_id)
                + valid_tail
            ),
            (
                '{"id":'
                + json.dumps(self.session_id)
                + valid_tail[:-1]
                + ',"number":NaN}'
            ),
            json.dumps(
                {
                    **self.state,
                    "deep": [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]],
                }
            ),
            json.dumps({**self.state, "items": list(range(9000))}),
        )
        for payload in payloads:
            with self.subTest(size=len(payload)):
                self.state_path.write_text(payload, encoding="utf-8")
                self.state_path.chmod(0o600)
                self.assert_invalid()

    def test_oversized_and_malformed_jsonl_tail_fail_closed(self):
        self.state_path.write_bytes(b" " * (512 * 1024 + 1))
        self.state_path.chmod(0o600)
        self.assert_invalid()

        self._write_json(self.state_path, self.state)
        with self.index_path.open("ab") as stream:
            stream.write(b'{"sessionId":')
        self.assert_invalid()
        sessions = list(KimiAdapter().discover(self.home))
        self.assertEqual(1, len(sessions))
        self.assertNotIn("kimi_mutation", sessions[0].extra)

    def test_regular_files_and_main_root_wire_are_required(self):
        self.wire_path.unlink()
        self.wire_path.mkdir(mode=0o700)
        self.assert_invalid()

        self.wire_path.rmdir()
        alternate = self.session_dir / "agents" / "worker"
        alternate.mkdir(mode=0o700)
        self._write_jsonl(alternate / "wire.jsonl", [])
        self.state["agents"]["main"] = {"homedir": str(alternate)}
        self._write_json(self.state_path, self.state)
        self.assert_invalid()

    def test_strict_revalidation_detects_atomic_path_replacement(self):
        session = self.make_session()
        evidence = capture_kimi_identity(session, home=self.home)
        self.replace_state(self.state)
        try:
            with self.assertRaises(KimiIdentityError) as raised:
                revalidate_kimi_identity(evidence, session, home=self.home)
            self.assertEqual("session_changed", raised.exception.code)
        finally:
            evidence.close()

        with self.assertRaises(KimiIdentityError) as closed:
            revalidate_kimi_identity(evidence, session, home=self.home)
        self.assertEqual("session_changed", closed.exception.code)

    def test_controlled_update_still_rejects_index_and_wire_replacement(self):
        session = self.make_session()
        evidence = capture_kimi_identity(session, home=self.home)
        replacement = self.index_path.with_name("index.next")
        self._write_jsonl(replacement, [self.index_row])
        os.replace(str(replacement), str(self.index_path))
        try:
            with self.assertRaises(KimiIdentityError) as raised:
                revalidate_kimi_identity_after_kimi_start(
                    evidence,
                    session,
                    home=self.home,
                )
            self.assertEqual("session_changed", raised.exception.code)
        finally:
            evidence.close()

    def test_invalid_session_shapes_and_native_ids_are_rejected(self):
        cases = (
            (object(), "invalid_session"),
            (self.make_session(agent="claude"), "invalid_session"),
            (self.make_session(extra=[]), "invalid_session"),
            (
                self.make_session(
                    extra={"source": "session_index", "agent_id": "worker"}
                ),
                "child_session",
            ),
            (self.make_session(session_id="会话"), "invalid_session"),
            (self.make_session(session_id="session with spaces"), "invalid_session"),
            (self.make_session(project="relative/project"), "invalid_session"),
        )
        for session, code in cases:
            with self.subTest(session_type=type(session).__name__):
                self.assert_invalid(session, code)

    def test_invalid_home_and_platform_capabilities_fail_closed(self):
        with mock.patch(
            "sidecar.kimi_identity.os.environ.get",
            return_value="unsafe\x00home",
        ):
            self.assert_invalid()
        with mock.patch(
            "sidecar.kimi_identity.os.environ.get",
            return_value="relative-home",
        ):
            self.assert_invalid()
        with mock.patch(
            "sidecar.kimi_identity.os.environ.get",
            return_value="/{}".format("x" * 4096),
        ):
            self.assert_invalid()
        with mock.patch(
            "sidecar.kimi_identity.os.geteuid",
            new=None,
        ):
            self.assert_invalid()
        with mock.patch.object(identity.os, "O_NOFOLLOW", 0):
            self.assert_invalid()
        with mock.patch(
            "sidecar.kimi_identity.os.environ.get",
            return_value=str(self.root / "missing-home"),
        ):
            self.assert_invalid()
        with mock.patch(
            "sidecar.kimi_identity.os.environ.get",
            return_value=None,
        ):
            with self.assertRaises(KimiIdentityError):
                capture_kimi_identity(self.make_session(), home=object())

    def test_workspace_registry_inspection_errors_fail_closed(self):
        real_stat = os.stat

        def guarded_stat(path, *args, **kwargs):
            if path == "workspaces.json":
                raise PermissionError("registry unavailable")
            return real_stat(path, *args, **kwargs)

        with mock.patch("sidecar.kimi_identity.os.stat", side_effect=guarded_stat):
            self.assert_invalid()

    def test_index_identity_rows_are_strict_and_exact(self):
        payloads = (
            b"",
            b"{}\n",
            (
                json.dumps(
                    {
                        "sessionId": "session_other",
                        "sessionDir": str(self.session_dir),
                        "workDir": str(self.project),
                    }
                ).encode("utf-8")
                + b"\n"
            ),
            json.dumps(self.index_row).encode("utf-8") + b"\n\n",
        )
        for payload in payloads:
            with self.subTest(payload_size=len(payload)):
                self.index_path.write_bytes(payload)
                self.index_path.chmod(0o600)
                self.assert_invalid()
                self._write_jsonl(self.index_path, [self.index_row])

        self.index_row["sessionDir"] = "   "
        self._write_jsonl(self.index_path, [self.index_row])
        self.assert_invalid()

    def test_json_token_unicode_and_root_wire_shapes_are_strict(self):
        valid_prefix = (
            '{"id":'
            + json.dumps(self.session_id)
            + ',"cwd":'
            + json.dumps(str(self.project))
            + ',"agents":{"main":{"homedir":'
            + json.dumps(str(self.main_dir))
            + '}},"number":'
        )
        state_payloads = (
            b"",
            b"[]",
            b"\xff",
            (valid_prefix + str(10**200) + "}").encode("utf-8"),
            (valid_prefix + str(2**300) + "}").encode("utf-8"),
            (valid_prefix + "1." + ("0" * 130) + "}").encode("utf-8"),
            (valid_prefix + "1e999}").encode("utf-8"),
        )
        for payload in state_payloads:
            with self.subTest(payload_size=len(payload)):
                self.state_path.write_bytes(payload)
                self.state_path.chmod(0o600)
                self.assert_invalid()
                self._write_json(self.state_path, self.state)

        self.state["finiteFloat"] = 1.5
        self._write_json(self.state_path, self.state)
        with capture_kimi_identity(self.make_session(), home=self.home):
            pass
        self._write_jsonl(self.wire_path, [[]])
        self.assert_invalid()

    def test_revalidation_maps_recapture_failures_and_invalid_evidence(self):
        session = self.make_session()
        evidence = capture_kimi_identity(session, home=self.home)
        self.state_path.unlink()
        try:
            with self.assertRaises(KimiIdentityError) as strict:
                evidence.revalidate(session, home=self.home)
            self.assertEqual("session_changed", strict.exception.code)
            with self.assertRaises(KimiIdentityError) as controlled:
                evidence.revalidate_after_kimi_start(session, home=self.home)
            self.assertEqual("session_changed", controlled.exception.code)
        finally:
            evidence.close()

        with self.assertRaises(KimiIdentityError) as invalid:
            revalidate_kimi_identity_after_kimi_start(
                object(),
                session,
                home=self.home,
            )
        self.assertEqual("session_changed", invalid.exception.code)

    def test_unexpected_capture_errors_are_redacted(self):
        with mock.patch(
            "sidecar.kimi_identity._validate_root_session",
            side_effect=RuntimeError("private path and identifier"),
        ):
            with self.assertRaises(KimiIdentityError) as raised:
                capture_kimi_identity(self.make_session(), home=self.home)
        self.assertEqual("invalid_session", raised.exception.code)
        self.assertNotIn("private", repr(raised.exception))

    def test_tail_read_errors_and_short_reads_fail_closed(self):
        descriptor = os.open(self.index_path, os.O_RDONLY)
        try:
            size = self.index_path.stat().st_size
            with mock.patch.object(identity.os, "pread", return_value=b""):
                with self.assertRaises(KimiIdentityError):
                    identity._read_fd_tail(descriptor, size, size + 1)
            with mock.patch.object(
                identity.os,
                "pread",
                side_effect=OSError("synthetic pread failure"),
            ):
                with self.assertRaises(KimiIdentityError):
                    identity._read_fd_tail(descriptor, size, size + 1)
        finally:
            os.close(descriptor)

        capture = identity._Capture()
        try:
            home_fd, home_identity = identity._open_absolute_directory(
                capture,
                str(self.kimi_home),
                reject_symlink_leaf=True,
            )
            with mock.patch.object(
                identity,
                "_read_fd_tail",
                side_effect=OSError("synthetic tail failure"),
            ):
                with self.assertRaises(KimiIdentityError):
                    identity._open_regular_tail_at(
                        capture,
                        home_fd,
                        home_identity,
                        "session_index.jsonl",
                        4 * 1024 * 1024,
                    )
        finally:
            capture.close()

    def test_index_metadata_errors_are_false_and_anchors_close_once(self):
        self.assertEqual(
            ((), False),
            read_kimi_index_metadata(self.root / "missing-index.jsonl"),
        )
        with mock.patch.object(
            identity,
            "_parse_index_tail",
            side_effect=KimiIdentityError(),
        ):
            self.assertEqual(
                ((), False),
                read_kimi_index_metadata(self.index_path),
            )

        anchors = identity._Anchors((-1,))
        anchors.close()
        anchors.close()
        self.assertTrue(anchors.closed)

    def test_digest_and_parser_error_boundaries_fail_closed(self):
        descriptor = os.open(self.index_path, os.O_RDONLY)
        try:
            for pread in (
                mock.Mock(return_value=b""),
                mock.Mock(side_effect=OSError("synthetic digest read failure")),
            ):
                with self.subTest(pread=repr(pread)):
                    with mock.patch.object(identity.os, "pread", pread):
                        with self.assertRaises(KimiIdentityError) as raised:
                            identity._descriptor_range_digest(descriptor, 0, 1)
                    self.assertEqual("session_changed", raised.exception.code)
        finally:
            os.close(descriptor)

        with self.assertRaises(KimiIdentityError):
            identity._descriptor_range_digest(-1, True, 1)
        with self.assertRaises(KimiIdentityError):
            identity._strict_jsonl(b"{}", allow_empty=False)
        records, valid = identity._parse_index_tail(
            identity._TailRead(
                payload=b"no-complete-record",
                offset=1,
                truncated=True,
                starts_at_boundary=False,
            )
        )
        self.assertEqual((), records)
        self.assertFalse(valid)

        with mock.patch.object(
            identity.os.path,
            "expandvars",
            side_effect=OSError("synthetic expansion failure"),
        ):
            with self.assertRaises(KimiIdentityError):
                identity._configured_home(self.home)
            capture = identity._Capture()
            try:
                with self.assertRaises(KimiIdentityError):
                    identity._project_source(capture, "adapter", str(self.project))
            finally:
                capture.close()

    def test_invalid_error_codes_are_normalized(self):
        error = KimiIdentityError("raw-private-value")
        self.assertEqual("invalid_session", error.code)
        self.assertNotIn("raw-private-value", repr(error))
        with self.assertRaises(KimiIdentityError):
            identity._digest({"not-json"})

    def test_session_directory_is_strictly_contained_without_dotdot(self):
        for value in (
            "../outside/session_native_47",
            str(self.root / "outside" / self.session_id),
        ):
            with self.subTest(value=value):
                self.index_row["sessionDir"] = value
                self._write_jsonl(self.index_path, [self.index_row])
                self.assert_invalid()
        self.index_row["sessionDir"] = str(self.session_dir)
        self._write_jsonl(self.index_path, [self.index_row])

        sessions = self.kimi_home / "sessions"
        real_sessions = self.kimi_home / "sessions-real"
        sessions.rename(real_sessions)
        sessions.symlink_to(real_sessions, target_is_directory=True)
        self.assert_invalid()
        observed = list(KimiAdapter().discover(self.home))
        main = next(
            session for session in observed if session.session_id == self.session_id
        )
        self.assertNotIn("kimi_mutation", main.extra)

    def test_home_internal_parent_mode_is_owner_safe(self):
        workspace_parent = self.session_dir.parent
        workspace_parent.chmod(0o777)

        self.assert_invalid()
        observed = list(KimiAdapter().discover(self.home))
        self.assertEqual(1, len(observed))
        self.assertNotIn("kimi_mutation", observed[0].extra)

        workspace_parent.chmod(0o700)
        self.kimi_home.chmod(0o777)
        self.assert_invalid()
        observed = list(KimiAdapter().discover(self.home))
        self.assertNotIn("kimi_mutation", observed[0].extra)

    def test_main_homedir_must_identify_exact_native_main_directory(self):
        worker = self.session_dir / "agents" / "worker"
        worker.mkdir(mode=0o700)
        self._write_jsonl(worker / "wire.jsonl", [])
        self.state["agents"]["main"]["homedir"] = str(worker)
        self._write_json(self.state_path, self.state)

        self.assert_invalid()
        observed = list(KimiAdapter().discover(self.home))
        main = next(
            session for session in observed if session.session_id == self.session_id
        )
        self.assertNotIn("kimi_mutation", main.extra)

    def test_non_boolean_false_remote_markers_are_remote(self):
        for value in (1, 0, None, "false", ""):
            with self.subTest(value=value):
                session = self.make_session(
                    extra={
                        "source": "session_index",
                        "agent_id": "main",
                        "remote": value,
                    }
                )
                self.assert_invalid(session, "remote_session")

        local = self.make_session(
            extra={
                "source": "session_index",
                "agent_id": "main",
                "remote": False,
            }
        )
        with capture_kimi_identity(local, home=self.home):
            pass

    def test_same_size_rewrite_and_truncate_reappend_are_not_append_only(self):
        session = self.make_session()
        for path in (self.index_path, self.wire_path):
            with self.subTest(path=path.name):
                evidence = capture_kimi_identity(session, home=self.home)
                original = path.read_bytes()
                path.write_bytes(original)
                path.chmod(0o600)
                try:
                    with self.assertRaises(KimiIdentityError) as raised:
                        revalidate_kimi_identity_after_kimi_start(
                            evidence,
                            session,
                            home=self.home,
                        )
                    self.assertEqual("session_changed", raised.exception.code)
                finally:
                    evidence.close()
                self._write_fixture()

    def test_growing_index_and_wire_must_preserve_exact_old_prefix(self):
        session = self.make_session()

        evidence = capture_kimi_identity(session, home=self.home)
        original_index = self.index_path.read_bytes()
        changed_index = original_index.replace(
            self.session_id.encode("ascii"),
            b"session_native_48",
            1,
        )
        self.index_path.write_bytes(changed_index + original_index)
        self.index_path.chmod(0o600)
        try:
            with self.assertRaises(KimiIdentityError):
                revalidate_kimi_identity_after_kimi_start(
                    evidence,
                    session,
                    home=self.home,
                )
        finally:
            evidence.close()
        self._write_fixture()

        evidence = capture_kimi_identity(session, home=self.home)
        original_wire = self.wire_path.read_bytes()
        changed_wire = original_wire.replace(b"completed", b"cancelled", 1)
        self.wire_path.write_bytes(changed_wire + original_wire)
        self.wire_path.chmod(0o600)
        try:
            with self.assertRaises(KimiIdentityError):
                revalidate_kimi_identity_after_kimi_start(
                    evidence,
                    session,
                    home=self.home,
                )
        finally:
            evidence.close()

    def test_foreign_session_transcript_is_not_the_bound_root_wire(self):
        foreign = self.main_dir / "foreign-wire.jsonl"
        self._write_jsonl(foreign, [])
        session = self.make_session(transcript=str(foreign))

        self.assert_invalid(session)

    def test_index_target_outside_bounded_tail_authority_is_rejected(self):
        target_row = self.index_path.read_bytes()
        padding = (
            b'{"padding":"'
            + (b"x" * (4 * 1024 * 1024 + 128))
            + b'","sessionId":"session_padding"}\n'
        )
        recent_row = json.dumps(
            {
                "sessionId": "session_recent",
                "sessionDir": str(
                    self.kimi_home
                    / "sessions"
                    / "workspace-1"
                    / "session_recent"
                ),
                "workDir": str(self.project),
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        self.index_path.write_bytes(target_row + padding + recent_row)
        self.index_path.chmod(0o600)

        self.assert_invalid()

    def test_duplicate_tail_row_is_observed_without_mutation_claim(self):
        duplicate = (
            b'{"sessionId":"duplicate","sessionId":"duplicate",'
            b'"sessionDir":"ignored","workDir":"ignored"}\n'
        )
        with self.index_path.open("ab") as stream:
            stream.write(duplicate)

        sessions = list(KimiAdapter().discover(self.home))

        self.assertEqual(1, len(sessions))
        self.assertNotIn("kimi_mutation", sessions[0].extra)
        self.assert_invalid()

    def test_controlled_failure_paths_do_not_leak_fresh_descriptors(self):
        session = self.make_session()
        evidence = capture_kimi_identity(session, home=self.home)
        before = len(os.listdir("/dev/fd"))
        try:
            with mock.patch(
                "sidecar.kimi_identity._descriptor_range_digest",
                side_effect=RuntimeError("synthetic prefix failure"),
            ):
                for _ in range(100):
                    with self.assertRaises(KimiIdentityError) as raised:
                        revalidate_kimi_identity_after_kimi_start(
                            evidence,
                            session,
                            home=self.home,
                        )
                    self.assertEqual("session_changed", raised.exception.code)
            after = len(os.listdir("/dev/fd"))
            self.assertEqual(before, after)
        finally:
            evidence.close()


if __name__ == "__main__":
    unittest.main()
