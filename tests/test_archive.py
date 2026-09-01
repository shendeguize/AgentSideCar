import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sidecar.archive import (
    ARCHIVE_SCHEMA_VERSION,
    MAX_ARCHIVE_ENTRIES,
    ArchiveError,
    ArchiveStore,
    normalize_statuses,
    normalize_targets,
    parse_duration,
    select_archivable,
    session_target,
)
from sidecar.model import Session, Status

NOW = 1_700_000_000.0


def make_session(agent, session_id, age_seconds, status=Status.IDLE):
    return Session(
        agent=agent,
        session_id=session_id,
        project="/tmp/project",
        transcript="/tmp/project/{}.jsonl".format(session_id),
        updated_at=NOW - age_seconds,
        title=session_id,
        status=status,
    )


class ParseDurationTest(unittest.TestCase):
    def test_accepts_units(self):
        self.assertEqual(parse_duration("90s"), 90.0)
        self.assertEqual(parse_duration("30m"), 1800.0)
        self.assertEqual(parse_duration("2h"), 7200.0)
        self.assertEqual(parse_duration("7d"), 604800.0)
        self.assertEqual(parse_duration(7200), 7200.0)

    def test_bare_number_is_seconds(self):
        self.assertEqual(parse_duration("120"), 120.0)

    def test_rejects_unbounded_and_malformed_values(self):
        for value in ("", "0", "30x", "-5m", "abc", True, None, 1e12):
            with self.assertRaises(ArchiveError):
                parse_duration(value)


class NormalizationTest(unittest.TestCase):
    def test_default_statuses_are_idle_and_dead(self):
        self.assertEqual(normalize_statuses(None), ("idle", "dead"))

    def test_status_filter_rejects_active_states(self):
        with self.assertRaises(ArchiveError):
            normalize_statuses(["working"])
        with self.assertRaises(ArchiveError):
            normalize_statuses([])

    def test_targets_deduplicate_and_preserve_order(self):
        targets = normalize_targets(
            [
                {"agent": "claude", "session_id": "a"},
                ("codex", "b"),
                {"agent": "claude", "session_id": "a"},
            ]
        )
        self.assertEqual(targets, (("claude", "a"), ("codex", "b")))

    def test_targets_reject_missing_identity(self):
        with self.assertRaises(ArchiveError):
            normalize_targets([{"agent": "claude"}])
        with self.assertRaises(ArchiveError):
            session_target({"agent": "", "session_id": "a"})


class SelectArchivableTest(unittest.TestCase):
    def test_selects_only_idle_and_dead_past_the_threshold(self):
        rows = [
            make_session("claude", "old-idle", 10 * 3600),
            make_session("codex", "old-dead", 50 * 3600, Status.DEAD),
            make_session("dsh", "fresh-idle", 60),
            make_session("kimi", "busy", 10 * 3600, Status.WORKING),
        ]
        selected = select_archivable(rows, idle_seconds="2h", now=NOW)
        self.assertEqual(
            [row.session_id for row in selected],
            ["old-idle", "old-dead"],
        )

    def test_status_filter_narrows_the_selection(self):
        rows = [
            make_session("claude", "old-idle", 10 * 3600),
            make_session("codex", "old-dead", 50 * 3600, Status.DEAD),
        ]
        selected = select_archivable(
            rows,
            idle_seconds="2h",
            statuses=["dead"],
            now=NOW,
        )
        self.assertEqual([row.session_id for row in selected], ["old-dead"])

    def test_accepts_mapping_rows(self):
        rows = [
            {"agent": "claude", "session_id": "a", "status": "idle", "updated_at": NOW - 7200},
        ]
        self.assertEqual(len(select_archivable(rows, idle_seconds="1h", now=NOW)), 1)


class ArchiveStoreTest(unittest.TestCase):
    def setUp(self):
        self.runtime = Path(tempfile.mkdtemp())
        self.store = ArchiveStore(self.runtime)

    def test_archive_is_idempotent(self):
        session = make_session("claude", "a", 10 * 3600)
        self.assertEqual(len(self.store.archive([session], now=NOW)), 1)
        self.assertEqual(self.store.archive([session], now=NOW), ())
        self.assertEqual(len(self.store.entries()), 1)

    def test_registry_file_is_private(self):
        self.store.archive([make_session("claude", "a", 10 * 3600)], now=NOW)
        mode = (self.runtime / "archive.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_partition_hides_archived_sessions(self):
        archived = make_session("claude", "a", 10 * 3600)
        visible = make_session("dsh", "b", 10)
        self.store.archive([archived], now=NOW)
        view = self.store.partition([archived, visible], now=NOW)
        self.assertEqual([row.session_id for row in view.visible], ["b"])
        self.assertEqual([row["session_id"] for row in view.archived], ["a"])
        self.assertEqual(view.archived[0]["archive_reason"], "manual")

    def test_new_activity_releases_the_entry(self):
        session = make_session("claude", "a", 10 * 3600)
        self.store.archive([session], now=NOW)
        revived = make_session("claude", "a", -60)
        view = self.store.partition([revived], now=NOW)
        self.assertEqual([row.session_id for row in view.visible], ["a"])
        self.assertEqual(view.released, (("claude", "a"),))
        self.assertEqual(self.store.entries(), ())

    def test_unarchive_reports_only_present_targets(self):
        self.store.archive([make_session("claude", "a", 10 * 3600)], now=NOW)
        self.assertEqual(
            self.store.unarchive([("claude", "a"), ("codex", "b")]),
            (("claude", "a"),),
        )
        self.assertEqual(self.store.unarchive([("claude", "a")]), ())

    def test_unarchive_all_clears_the_registry(self):
        self.store.archive(
            [make_session("claude", "a", 10 * 3600), make_session("codex", "b", 10 * 3600)],
            now=NOW,
        )
        self.assertEqual(len(self.store.unarchive_all()), 2)
        self.assertEqual(self.store.entries(), ())

    def test_corrupt_registry_is_reported_not_ignored(self):
        (self.runtime / "archive.json").write_text("{", encoding="utf-8")
        with self.assertRaises(ArchiveError) as caught:
            self.store.entries()
        self.assertEqual(caught.exception.code, "archive_corrupt")

    def test_unknown_schema_version_is_corrupt(self):
        (self.runtime / "archive.json").write_text(
            json.dumps({"schema_version": 99, "entries": []}),
            encoding="utf-8",
        )
        with self.assertRaises(ArchiveError):
            self.store.entries()

    def test_entries_are_bounded(self):
        sessions = [
            make_session("claude", "s{}".format(index), 10 * 3600)
            for index in range(MAX_ARCHIVE_ENTRIES + 5)
        ]
        self.store.archive(sessions, now=NOW)
        self.assertEqual(len(self.store.entries()), MAX_ARCHIVE_ENTRIES)

    def test_missing_registry_reads_as_empty(self):
        self.assertEqual(ArchiveStore(Path(tempfile.mkdtemp())).entries(), ())

    def test_schema_version_is_persisted(self):
        self.store.archive([make_session("claude", "a", 10 * 3600)], now=NOW)
        document = json.loads((self.runtime / "archive.json").read_text("utf-8"))
        self.assertEqual(document["schema_version"], ARCHIVE_SCHEMA_VERSION)


class RegistryFailureTest(unittest.TestCase):
    """The registry's refusal paths: a bad registry must never be guessed at."""

    def setUp(self):
        self.runtime = Path(tempfile.mkdtemp())
        self.store = ArchiveStore(self.runtime)
        self.registry = self.runtime / "archive.json"

    def write_registry(self, document):
        self.registry.write_text(json.dumps(document), encoding="utf-8")

    def test_an_unknown_error_code_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            ArchiveError("no_such_code")

    def test_an_unreadable_registry_is_an_error_not_an_empty_one(self):
        registry = self.runtime / "as-a-directory"
        registry.mkdir()
        store = ArchiveStore(path=registry)
        with self.assertRaises(ArchiveError) as caught:
            store.entries()
        self.assertEqual(caught.exception.code, "archive_error")

    def test_an_empty_file_reads_as_an_empty_registry(self):
        self.registry.write_text("   \n", encoding="utf-8")
        self.assertEqual(self.store.entries(), ())

    def test_malformed_entry_collections_are_corrupt(self):
        for entries in ("not-a-list", ["not-a-mapping"], [{"agent": "claude"}]):
            with self.subTest(entries=entries):
                self.write_registry(
                    {"schema_version": ARCHIVE_SCHEMA_VERSION, "entries": entries}
                )
                with self.assertRaises(ArchiveError) as caught:
                    self.store.entries()
                self.assertEqual(caught.exception.code, "archive_corrupt")

    def test_an_entry_with_an_unknown_reason_or_negative_time_is_corrupt(self):
        for entry in (
            {
                "agent": "claude",
                "session_id": "a",
                "archived_at": NOW,
                "reason": "invented",
            },
            {
                "agent": "claude",
                "session_id": "a",
                "archived_at": -1,
                "reason": "manual",
            },
        ):
            with self.subTest(reason=entry["reason"]):
                self.write_registry(
                    {"schema_version": ARCHIVE_SCHEMA_VERSION, "entries": [entry]}
                )
                with self.assertRaises(ArchiveError):
                    self.store.entries()

    def test_a_duplicated_target_is_corrupt_rather_than_deduplicated(self):
        entry = {
            "agent": "claude",
            "session_id": "a",
            "archived_at": NOW,
            "reason": "manual",
        }
        self.write_registry(
            {"schema_version": ARCHIVE_SCHEMA_VERSION, "entries": [entry, dict(entry)]}
        )
        with self.assertRaises(ArchiveError):
            self.store.entries()

    def test_an_oversized_registry_file_is_corrupt(self):
        entries = [
            {
                "agent": "claude",
                "session_id": "s{}".format(index),
                "archived_at": NOW,
                "reason": "manual",
            }
            for index in range(MAX_ARCHIVE_ENTRIES + 1)
        ]
        self.write_registry(
            {"schema_version": ARCHIVE_SCHEMA_VERSION, "entries": entries}
        )
        with self.assertRaises(ArchiveError):
            self.store.entries()

    def test_a_failed_replace_leaves_no_temporary_file_behind(self):
        session = make_session("claude", "a", 10 * 3600)
        with mock.patch("os.replace", side_effect=OSError("nope")):
            with self.assertRaises(ArchiveError) as caught:
                self.store.archive([session], now=NOW)
        self.assertEqual(caught.exception.code, "archive_error")
        self.assertEqual(
            [path.name for path in self.runtime.iterdir()],
            [],
        )

    def test_a_failed_cleanup_still_reports_the_write_failure(self):
        session = make_session("claude", "a", 10 * 3600)
        with mock.patch("os.replace", side_effect=OSError("nope")):
            with mock.patch("os.unlink", side_effect=OSError("also nope")):
                with self.assertRaises(ArchiveError):
                    self.store.archive([session], now=NOW)


class RegistryEdgeCaseTest(unittest.TestCase):
    """Small refusals and no-ops that keep callers from writing guard code."""

    def setUp(self):
        self.runtime = Path(tempfile.mkdtemp())
        self.store = ArchiveStore(self.runtime)

    def test_an_explicit_path_overrides_the_runtime_directory(self):
        registry = self.runtime / "elsewhere" / "custom.json"
        store = ArchiveStore(self.runtime, path=registry)
        store.archive([make_session("claude", "a", 10 * 3600)], now=NOW)
        self.assertTrue(registry.is_file())
        self.assertEqual(self.store.entries(), ())

    def test_is_archived_answers_per_target(self):
        self.store.archive([make_session("claude", "a", 10 * 3600)], now=NOW)
        self.assertTrue(self.store.is_archived("claude", "a"))
        self.assertFalse(self.store.is_archived("codex", "a"))

    def test_an_invented_reason_is_refused(self):
        with self.assertRaises(ArchiveError) as caught:
            self.store.archive(
                [make_session("claude", "a", 10 * 3600)],
                reason="because",
            )
        self.assertEqual(caught.exception.code, "archive_error")

    def test_empty_requests_are_no_ops_rather_than_errors(self):
        self.assertEqual(self.store.archive([]), ())
        self.assertEqual(self.store.unarchive([]), ())
        self.assertEqual(self.store.unarchive_all(), ())

    def test_a_duplicate_status_filter_collapses(self):
        self.assertEqual(normalize_statuses(["idle", "IDLE"]), ("idle",))

    def test_non_string_identities_are_refused(self):
        with self.assertRaises(ArchiveError):
            normalize_statuses([object()])
        with self.assertRaises(ArchiveError):
            session_target({"agent": 1, "session_id": "a"})
        with self.assertRaises(ArchiveError):
            normalize_targets([(1, "a")])
        with self.assertRaises(ArchiveError):
            normalize_targets([("claude", "")])

    def test_an_unreadable_timestamp_counts_as_the_epoch(self):
        rows = [
            {
                "agent": "claude",
                "session_id": "a",
                "status": "idle",
                "updated_at": "whenever",
            }
        ]
        self.assertEqual(len(select_archivable(rows, idle_seconds="1h", now=NOW)), 1)

    def test_partition_passes_through_rows_without_an_identity(self):
        self.store.archive([make_session("claude", "a", 10 * 3600)], now=NOW)
        anonymous = {"status": "idle", "updated_at": NOW}
        view = self.store.partition([anonymous], now=NOW)
        self.assertEqual(view.visible, [anonymous])
        self.assertEqual(view.archived, [])

    def test_archived_rows_keep_whatever_shape_the_scanner_produced(self):
        self.store.archive([("claude", "a"), ("codex", "b")], now=NOW)

        class Opaque:
            agent = "codex"
            session_id = "b"
            status = "idle"
            updated_at = NOW - 10 * 3600

        view = self.store.partition(
            [
                {
                    "agent": "claude",
                    "session_id": "a",
                    "status": "idle",
                    "updated_at": NOW - 10 * 3600,
                },
                Opaque(),
            ],
            now=NOW,
        )
        self.assertEqual(
            [row["session_id"] for row in view.archived],
            ["a", "b"],
        )
        self.assertEqual(
            [row["archive_reason"] for row in view.archived],
            ["manual", "manual"],
        )


class FakeScanner:
    errors = ()
    failed_agent_names = ()

    def __init__(self, sessions):
        self.sessions = list(sessions)

    def scan(self, **kwargs):
        return list(self.sessions)


def make_live_session(agent, session_id, age_seconds, status=Status.IDLE):
    """Build a session aged against the wall clock the daemon reads."""

    session = make_session(agent, session_id, age_seconds, status)
    session.updated_at = time.time() - age_seconds
    return session


class DaemonArchiveOpsTest(unittest.TestCase):
    def setUp(self):
        from sidecar.daemon import SidecarDaemon

        self.runtime = Path(tempfile.mkdtemp())
        self.sessions = [
            make_live_session("claude", "s-idle", 10 * 3600),
            make_live_session("codex", "s-dead", 50 * 3600, Status.DEAD),
            make_live_session("dsh", "s-live", 5, Status.WORKING),
        ]
        self.daemon = SidecarDaemon(
            scanner=FakeScanner(self.sessions),
            runtime_dir=self.runtime,
        )
        self.daemon.scan_once()

    def visible(self):
        return [row["session_id"] for row in self.daemon.sessions]

    def test_preview_selects_idle_and_dead(self):
        response = self.daemon._archive_preview_response({"idle_seconds": "2h"})
        self.assertTrue(response["ok"])
        self.assertEqual(
            [row["session_id"] for row in response["candidates"]],
            ["s-idle", "s-dead"],
        )
        self.assertTrue(response["token"])

    def test_preview_rejects_invalid_threshold(self):
        response = self.daemon._archive_preview_response({"idle_seconds": "nope"})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_request")

    def test_apply_requires_a_live_token(self):
        response = self.daemon._archive_apply_response(
            {
                "targets": [{"agent": "claude", "session_id": "s-idle"}],
                "token": "forged",
            }
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_token")
        self.assertIn("s-idle", self.visible())

    def test_apply_accepts_a_previewed_subset(self):
        preview = self.daemon._archive_preview_response({"idle_seconds": "2h"})
        response = self.daemon._archive_apply_response(
            {
                "targets": [{"agent": "claude", "session_id": "s-idle"}],
                "token": preview["token"],
            }
        )
        self.assertTrue(response["ok"])
        self.assertEqual(response["count"], 1)
        self.assertEqual(self.visible(), ["s-live", "s-dead"])
        self.assertEqual(
            [row["session_id"] for row in self.daemon.archived_sessions],
            ["s-idle"],
        )

    def test_apply_rejects_targets_outside_the_preview(self):
        preview = self.daemon._archive_preview_response(
            {"idle_seconds": "2h", "statuses": ["dead"]}
        )
        response = self.daemon._archive_apply_response(
            {
                "targets": [{"agent": "dsh", "session_id": "s-live"}],
                "token": preview["token"],
            }
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_request")

    def test_tokens_are_single_use(self):
        preview = self.daemon._archive_preview_response({"idle_seconds": "2h"})
        target = {"targets": [{"agent": "claude", "session_id": "s-idle"}]}
        first = dict(target, token=preview["token"])
        self.assertTrue(self.daemon._archive_apply_response(first)["ok"])
        second = dict(target, token=preview["token"])
        self.assertFalse(self.daemon._archive_apply_response(second)["ok"])

    def test_unarchive_restores_the_session(self):
        preview = self.daemon._archive_preview_response({"idle_seconds": "2h"})
        self.daemon._archive_apply_response(
            {
                "targets": [{"agent": "claude", "session_id": "s-idle"}],
                "token": preview["token"],
            }
        )
        response = self.daemon._unarchive_response(
            {"targets": [{"agent": "claude", "session_id": "s-idle"}]}
        )
        self.assertEqual(response["count"], 1)
        self.assertIn("s-idle", self.visible())

    def test_unarchive_requires_targets_or_all(self):
        response = self.daemon._unarchive_response({})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "invalid_request")

    def test_status_reports_the_archive_policy(self):
        policy = self.daemon._status_response()["archive_policy"]
        self.assertFalse(policy["auto"])
        self.assertEqual(policy["auto_after_seconds"], 86400.0)

    def test_corrupt_registry_degrades_to_showing_everything(self):
        (self.runtime / "archive.json").write_text("{", encoding="utf-8")
        self.daemon.scan_once()
        self.assertEqual(len(self.daemon.sessions), 3)
        diagnostics = self.daemon._status_response()["diagnostics"]
        self.assertIn(
            "archive_corrupt",
            [entry.get("code") for entry in diagnostics],
        )

    def test_apply_refuses_a_request_without_usable_targets(self):
        for targets in (None, [], "s-idle"):
            with self.subTest(targets=targets):
                response = self.daemon._archive_apply_response({"targets": targets})
                self.assertEqual(response["error"]["code"], "invalid_request")

    def test_apply_refuses_targets_that_carry_no_identity(self):
        response = self.daemon._archive_apply_response(
            {"targets": [{"agent": "claude"}], "token": "unused"}
        )
        self.assertEqual(response["error"]["code"], "invalid_request")

    def test_apply_rejects_a_missing_or_malformed_token(self):
        for token in (None, "", 7):
            with self.subTest(token=token):
                response = self.daemon._archive_apply_response(
                    {
                        "targets": [{"agent": "claude", "session_id": "s-idle"}],
                        "token": token,
                    }
                )
                self.assertEqual(response["error"]["code"], "invalid_token")

    def test_apply_reports_a_registry_failure_instead_of_claiming_success(self):
        preview = self.daemon._archive_preview_response({"idle_seconds": "2h"})
        (self.runtime / "archive.json").write_text("{", encoding="utf-8")
        response = self.daemon._archive_apply_response(
            {
                "targets": [{"agent": "claude", "session_id": "s-idle"}],
                "token": preview["token"],
            }
        )
        self.assertEqual(response["error"]["code"], "archive_corrupt")
        self.assertIn("s-idle", self.visible())

    def test_unarchive_all_releases_every_entry(self):
        preview = self.daemon._archive_preview_response({"idle_seconds": "2h"})
        self.daemon._archive_apply_response(
            {
                "targets": [
                    {"agent": "claude", "session_id": "s-idle"},
                    {"agent": "codex", "session_id": "s-dead"},
                ],
                "token": preview["token"],
            }
        )
        response = self.daemon._unarchive_response({"all": True})
        self.assertEqual(response["count"], 2)
        self.assertEqual(sorted(self.visible()), ["s-dead", "s-idle", "s-live"])

    def test_unarchive_reports_a_registry_failure(self):
        (self.runtime / "archive.json").write_text("{", encoding="utf-8")
        response = self.daemon._unarchive_response({"all": True})
        self.assertEqual(response["error"]["code"], "archive_corrupt")

    def test_archive_list_returns_the_entries_and_their_sessions(self):
        preview = self.daemon._archive_preview_response({"idle_seconds": "2h"})
        self.daemon._archive_apply_response(
            {
                "targets": [{"agent": "claude", "session_id": "s-idle"}],
                "token": preview["token"],
            }
        )
        response = self.daemon._archive_list_response()
        self.assertTrue(response["ok"])
        self.assertEqual(response["count"], 1)
        self.assertEqual(
            [entry["session_id"] for entry in response["entries"]],
            ["s-idle"],
        )
        self.assertEqual(
            [row["session_id"] for row in response["sessions"]],
            ["s-idle"],
        )

    def test_archive_list_reports_a_registry_failure(self):
        (self.runtime / "archive.json").write_text("{", encoding="utf-8")
        response = self.daemon._archive_list_response()
        self.assertEqual(response["error"]["code"], "archive_corrupt")

    def test_preview_tokens_are_bounded(self):
        from sidecar.daemon import MAX_ARCHIVE_TOKENS

        for _ in range(MAX_ARCHIVE_TOKENS + 2):
            self.daemon._archive_preview_response({"idle_seconds": "2h"})
        self.assertLessEqual(
            len(self.daemon._archive_tokens),
            MAX_ARCHIVE_TOKENS,
        )


class DaemonAutoArchiveTest(unittest.TestCase):
    def build(self, **kwargs):
        from sidecar.daemon import SidecarDaemon

        sessions = [
            make_live_session("claude", "s-day-old", 30 * 3600),
            make_live_session("codex", "s-hour-old", 3 * 3600),
        ]
        return SidecarDaemon(
            scanner=FakeScanner(sessions),
            runtime_dir=Path(tempfile.mkdtemp()),
            **kwargs,
        )

    def test_auto_archive_is_off_by_default(self):
        daemon = self.build()
        daemon.scan_once()
        self.assertEqual(len(daemon.sessions), 2)

    def test_auto_archive_hides_sessions_past_the_policy_threshold(self):
        daemon = self.build(auto_archive=True)
        daemon.scan_once()
        self.assertEqual(
            [row["session_id"] for row in daemon.sessions],
            ["s-hour-old"],
        )
        self.assertEqual(
            [row["archive_reason"] for row in daemon.archived_sessions],
            ["auto"],
        )

    def test_auto_archive_threshold_is_configurable(self):
        daemon = self.build(auto_archive=True, auto_archive_after="1h")
        daemon.scan_once()
        self.assertEqual(daemon.sessions, [])


class ArchiveNeverTouchesVendorStateTest(unittest.TestCase):
    def test_transcript_survives_archiving(self):
        runtime = Path(tempfile.mkdtemp())
        project = Path(tempfile.mkdtemp())
        transcript = project / "session.jsonl"
        transcript.write_text('{"kind":"user"}\n', encoding="utf-8")
        session = Session(
            agent="claude",
            session_id="a",
            project=str(project),
            transcript=str(transcript),
            updated_at=time.time() - 10 * 3600,
            status=Status.IDLE,
        )
        ArchiveStore(runtime).archive([session])
        self.assertTrue(transcript.exists())
        self.assertEqual(transcript.read_text("utf-8"), '{"kind":"user"}\n')


if __name__ == "__main__":
    unittest.main()
