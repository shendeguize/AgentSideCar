import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sidecar
from sidecar.adapters import get_adapter, iter_adapters, registry
from sidecar.adapters.base import Adapter, sanitize_terminal_text, snip
from sidecar.client import SidecarClientError
from sidecar.cli import RECENT_SECONDS, build_parser, main
from sidecar.model import Event, Session, Status
from sidecar.presentation import format_age_seconds, row_age, row_value
from sidecar.scan import ScanError
from sidecar.tail import JSONLFollower, SessionTailer


def make_session(
    session_id,
    status=Status.WAITING,
    agent="claude",
    updated_at=100.0,
    transcript=None,
    parent_id=None,
    extra=None,
):
    return Session(
        agent=agent,
        session_id=session_id,
        project="/tmp/project",
        transcript="/tmp/{}.jsonl".format(session_id) if transcript is None else transcript,
        updated_at=updated_at,
        title="title {}".format(session_id),
        status=status,
        parent_id=parent_id,
        extra={} if extra is None else extra,
    )


class PresentationTests(unittest.TestCase):
    def test_row_value_supports_dict_and_session_rows(self):
        session = make_session("object-row")

        self.assertEqual("dict-row", row_value({"session_id": "dict-row"}, "session_id"))
        self.assertEqual("object-row", row_value(session, "session_id"))
        self.assertEqual("fallback", row_value({}, "missing", "fallback"))
        self.assertEqual("fallback", row_value(session, "missing", "fallback"))

    def test_format_age_seconds_clamps_and_formats_boundaries(self):
        cases = (
            (-1.0, "0s"),
            (0.0, "0s"),
            (59.999, "59s"),
            (60.0, "1m"),
            (3599.0, "59m"),
            (3600.0, "1h"),
            (86399.0, "23h"),
            (86400.0, "1d"),
        )

        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                self.assertEqual(expected, format_age_seconds(seconds))

    def test_row_age_uses_injected_now_for_dict_and_session_rows(self):
        session = make_session("object-age", updated_at=640.0)

        self.assertEqual("1m", row_age({"updated_at": 940.0}, now=1000.0))
        self.assertEqual("6m", row_age(session, now=1000.0))
        self.assertEqual("0s", row_age({"updated_at": 1001.0}, now=1000.0))

    def test_row_age_returns_adapter_default_for_malformed_timestamps(self):
        for updated_at in (None, "not-a-timestamp"):
            with self.subTest(updated_at=updated_at):
                row = {"updated_at": updated_at}
                self.assertEqual("", row_age(row, now=1000.0))
                self.assertEqual("?", row_age(row, now=1000.0, default="?"))


class FakeScanner:
    def __init__(self, sessions=(), errors=()):
        self.sessions = list(sessions)
        self.errors = list(errors)
        self.recent_calls = []

    def scan(self, recent_seconds=None):
        self.recent_calls.append(recent_seconds)
        return list(self.sessions)


class OfflineClient:
    def status(self):
        raise SidecarClientError("offline", code="connection_failed")

    def subscribe(self):
        raise SidecarClientError("offline", code="connection_failed")

    def ping(self):
        raise SidecarClientError("offline", code="connection_failed")


class FakeClient:
    def __init__(
        self,
        sessions=(),
        events=(),
        pid=4321,
        socket_path=None,
        scan_errors=(),
    ):
        self.sessions = list(sessions)
        self.events = list(events)
        self.pid = pid
        self.socket_path = socket_path
        self.scan_errors = list(scan_errors)
        self.status_calls = 0
        self.subscribe_calls = 0

    def status(self):
        self.status_calls += 1
        return list(self.sessions)

    def subscribe(self):
        self.subscribe_calls += 1
        return iter(self.events)

    def ping(self):
        return {"ok": True, "op": "ping", "pid": self.pid}


class ReplayAdapter(Adapter):
    name = "dsh"
    agent_names = ("dsh",)

    def __init__(self, records):
        self.records = list(records)
        self.calls = []

    def discover(self, home):
        return []

    def replay(self, session, after_seq=None, max_records=256):
        del session
        self.calls.append(after_seq)
        records = [
            record
            for record in self.records
            if after_seq is None or record["seq"] > after_seq
        ]
        return records[:max_records]

    def normalize(self, record, session):
        return [
            Event(
                "2026-08-23T04:00:00+08:00",
                session.agent,
                session.session_id,
                "event",
                str(record["seq"]),
                {"seq": record["seq"]},
            )
        ]


class TerminalTextTests(unittest.TestCase):
    def test_sanitizer_removes_terminal_sequences_and_preserves_unicode(self):
        payload = (
            "  正常\tline \x1b]0;pwned\x07\x1b[31m红色\x1b[0m\r覆写 "
            "\x1b]2;st-pwned\x1b\\ \x9b2J C1\x8dtext "
            "\x9d0;c1-pwned\x9c emoji 👩‍💻  "
        )

        expected = "正常 line 红色 覆写 C1text emoji 👩‍💻"
        normalized = (
            "正常 line \x1b]0;pwned\x07\x1b[31m红色\x1b[0m 覆写 "
            "\x1b]2;st-pwned\x1b\\ \x9b2J C1\x8dtext "
            "\x9d0;c1-pwned\x9c emoji 👩‍💻"
        )
        self.assertEqual(expected, sanitize_terminal_text(payload))
        self.assertEqual(normalized, snip(payload, 200))
        self.assertEqual(
            "keeps  spacing",
            sanitize_terminal_text(
                "keeps  spacing",
                collapse_whitespace=False,
            ),
        )


class CLITests(unittest.TestCase):
    def run_cli(self, argv, scanner):
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            argv,
            scanner=scanner,
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def test_global_version_uses_canonical_package_version(self):
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])

        self.assertEqual(0, raised.exception.code)
        self.assertEqual(
            "agent-sidecar {}\n".format(sidecar.__version__),
            stdout.getvalue(),
        )

    def test_help_documents_version_and_repeatable_agent_filter(self):
        parser = build_parser()
        args = parser.parse_args(
            ["list", "--agent", "cursor-ide", "--agent", "CLAUDE"]
        )
        list_parser = next(
            action.choices["list"]
            for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )
        list_help = list_parser.format_help()

        self.assertIn("--version", parser.format_help())
        self.assertIn("--agent NAME", list_help)
        self.assertEqual(["cursor-ide", "CLAUDE"], args.agent)

    def test_list_json_uses_session_schema_and_48_hour_default(self):
        session = make_session("one")
        scanner = FakeScanner(
            [session],
            [
                ScanError(
                    adapter="broken",
                    stage="discover",
                    message="unreadable",
                    exception_type="OSError",
                )
            ],
        )

        code, stdout, stderr = self.run_cli(["list", "--json"], scanner)

        self.assertEqual(0, code)
        self.assertEqual([session.to_dict()], json.loads(stdout))
        self.assertEqual([RECENT_SECONDS], scanner.recent_calls)
        self.assertIn("scan error: broken discover: unreadable", stderr)
        self.assertNotIn("scan error", stdout)

    def test_list_all_disables_recent_filter(self):
        scanner = FakeScanner()

        code, stdout, stderr = self.run_cli(["list", "--all", "--json"], scanner)

        self.assertEqual(0, code)
        self.assertEqual([], json.loads(stdout))
        self.assertEqual([None], scanner.recent_calls)
        self.assertEqual("", stderr)

    def test_list_human_output_has_clear_header_but_json_does_not(self):
        session = make_session("one")

        code, human, stderr = self.run_cli(["list"], FakeScanner([session]))
        json_code, encoded, json_stderr = self.run_cli(
            ["list", "--json"],
            FakeScanner([session]),
        )

        self.assertEqual(0, code)
        self.assertEqual(
            ["AGENT", "STATUS", "SESSION", "AGE", "UPDATED", "TITLE"],
            human.splitlines()[0].split(),
        )
        self.assertIn("claude", human.splitlines()[1])
        self.assertEqual("", stderr)
        self.assertEqual(0, json_code)
        self.assertEqual([session.to_dict()], json.loads(encoded))
        self.assertFalse(encoded.startswith("AGENT"))
        self.assertEqual("", json_stderr)

    def test_list_agent_filter_is_repeatable_exact_and_case_insensitive_direct(self):
        claude = make_session("claude", agent="claude")
        cursor_ide = make_session("ide", agent="cursor-ide")
        cursor_cli = make_session("cli", agent="cursor-cli")
        scanner = FakeScanner([claude, cursor_ide, cursor_cli])

        code, stdout, stderr = self.run_cli(
            [
                "list",
                "--agent",
                "CURSOR-IDE",
                "--agent",
                "cLaUdE",
                "--json",
            ],
            scanner,
        )

        self.assertEqual(0, code)
        self.assertEqual(
            [claude.to_dict(), cursor_ide.to_dict()],
            json.loads(stdout),
        )
        self.assertEqual([RECENT_SECONDS], scanner.recent_calls)
        self.assertEqual("", stderr)

    def test_list_agent_filter_matches_daemon_rows_without_direct_scan(self):
        now = time.time()
        cursor_ide = make_session("ide", agent="cursor-ide", updated_at=now)
        cursor_cli = make_session("cli", agent="cursor-cli", updated_at=now)
        scanner = FakeScanner([make_session("direct")])
        client = FakeClient([cursor_ide.to_dict(), cursor_cli.to_dict()])
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["list", "--agent", "CURSOR-CLI", "--json"],
            scanner=scanner,
            client=client,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(0, code)
        self.assertEqual([cursor_cli.to_dict()], json.loads(stdout.getvalue()))
        self.assertEqual(1, client.status_calls)
        self.assertEqual([], scanner.recent_calls)
        self.assertEqual("", stderr.getvalue())

    def test_list_unknown_or_inexact_agent_filter_returns_empty_without_crash(self):
        sessions = [
            make_session("ide", agent="cursor-ide"),
            make_session("cli", agent="cursor-cli"),
        ]

        for agent_name in ("unknown", "cursor"):
            with self.subTest(agent_name=agent_name, output="json"):
                code, stdout, stderr = self.run_cli(
                    ["list", "--agent", agent_name, "--json"],
                    FakeScanner(sessions),
                )
                self.assertEqual(0, code)
                self.assertEqual([], json.loads(stdout))
                self.assertEqual("", stderr)

            with self.subTest(agent_name=agent_name, output="human"):
                code, stdout, stderr = self.run_cli(
                    ["list", "--agent", agent_name],
                    FakeScanner(sessions),
                )
                self.assertEqual(0, code)
                self.assertEqual(1, len(stdout.splitlines()))
                self.assertTrue(stdout.startswith("AGENT"))
                self.assertEqual("", stderr)

    def test_status_json_contains_only_working_and_waiting_sessions(self):
        working = make_session("working", Status.WORKING)
        waiting = make_session("waiting", Status.WAITING)
        idle = make_session("idle", Status.IDLE)
        dead = make_session("dead", Status.DEAD)
        scanner = FakeScanner([working, waiting, idle, dead])

        code, stdout, stderr = self.run_cli(["status", "--json"], scanner)

        self.assertEqual(0, code)
        self.assertEqual([working.to_dict(), waiting.to_dict()], json.loads(stdout))
        self.assertEqual([None], scanner.recent_calls)
        self.assertEqual("", stderr)

    def test_status_with_no_active_sessions_has_human_message_and_clean_json(self):
        inactive = [
            make_session("idle", Status.IDLE),
            make_session("dead", Status.DEAD),
        ]

        code, stdout, stderr = self.run_cli(
            ["status"],
            FakeScanner(inactive),
        )
        json_code, encoded, json_stderr = self.run_cli(
            ["status", "--json"],
            FakeScanner(inactive),
        )

        self.assertEqual(0, code)
        self.assertEqual("no active sessions\n", stdout)
        self.assertEqual("", stderr)
        self.assertEqual(0, json_code)
        self.assertEqual([], json.loads(encoded))
        self.assertEqual("[]\n", encoded)
        self.assertEqual("", json_stderr)

    def test_list_and_status_human_output_sanitize_untrusted_titles(self):
        title = (
            "\x1b]0;pwned\x07\x1b[31m红色\x1b[0m\r覆写"
            "\x1b[2J\x1b[999C\x9b2J\x9d0;c1-pwned\x9c"
        )
        session = make_session("unsafe", Status.WAITING)
        session.title = snip(title, 200)

        for command in ("list", "status"):
            with self.subTest(command=command):
                code, stdout, stderr = self.run_cli(
                    [command],
                    FakeScanner([session]),
                )

                self.assertEqual(0, code)
                self.assertEqual("", stderr)
                self.assertIn("红色 覆写", stdout)
                self.assertNotIn("pwned", stdout)
                self.assertNotIn("\x1b", stdout)
                self.assertNotIn("\x9b", stdout)
                self.assertNotIn("\r", stdout)

    def test_list_json_preserves_untrusted_semantic_text(self):
        source = "  \x1b]0;pwned\x07\x1b[31m红色\r覆写\x9b2J  "
        title = "\x1b]0;pwned\x07\x1b[31m红色 覆写\x9b2J"
        session = make_session("unsafe-json")
        session.title = snip(source, 200)

        code, stdout, stderr = self.run_cli(
            ["list", "--json"],
            FakeScanner([session]),
        )

        self.assertEqual(0, code)
        self.assertEqual(title, json.loads(stdout)[0]["title"])
        self.assertEqual("", stderr)

    def test_list_and_status_human_output_render_parent_before_child(self):
        parent = make_session("parent", updated_at=100.0)
        child = make_session(
            "child",
            updated_at=300.0,
            parent_id="parent",
        )

        for command in ("list", "status"):
            with self.subTest(command=command):
                code, stdout, stderr = self.run_cli(
                    [command],
                    FakeScanner([child, parent]),
                )
                lines = stdout.splitlines()

                self.assertEqual(0, code)
                self.assertEqual("", stderr)
                self.assertLess(stdout.index("parent"), stdout.index("child"))
                child_line = next(line for line in lines if "child" in line)
                self.assertTrue(child_line.startswith("  ↳ claude"))

    def test_human_tree_keeps_orphans_cycles_and_agent_identities_as_roots(self):
        orphan = make_session(
            "orphan",
            extra={"sidechain": True},
            parent_id="missing",
        )
        first = make_session("cycle-a", parent_id="cycle-b")
        second = make_session("cycle-b", parent_id="cycle-a")
        claude_parent = make_session("shared", agent="claude")
        cursor_child = make_session(
            "cursor-child",
            agent="cursor-ide",
            parent_id="shared",
        )

        code, stdout, stderr = self.run_cli(
            ["list"],
            FakeScanner([cursor_child, second, orphan, first, claude_parent]),
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertIn("[sidechain] title orphan", stdout)
        for session_id in ("orphan", "cycle-a", "cycle-b", "cursor-child"):
            line = next(
                line for line in stdout.splitlines() if session_id in line
            )
            self.assertFalse(line.startswith("  ↳"))

    def test_status_groups_priority_before_cross_status_parent_link(self):
        waiting_parent = make_session(
            "waiting-parent",
            status=Status.WAITING,
            updated_at=300.0,
        )
        working_child = make_session(
            "working-child",
            status=Status.WORKING,
            updated_at=100.0,
            parent_id="waiting-parent",
        )

        code, stdout, stderr = self.run_cli(
            ["status"],
            FakeScanner([waiting_parent, working_child]),
        )
        lines = stdout.splitlines()
        working_line = next(line for line in lines if "working-child" in line)

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertLess(stdout.index("working-child"), stdout.index("waiting-parent"))
        self.assertFalse(working_line.startswith("  ↳"))

    def test_human_tree_caps_deep_indentation_without_recursion(self):
        sessions = []
        parent_id = None
        for index in range(1200):
            session_id = "deep-{:04d}".format(index)
            sessions.append(
                make_session(
                    session_id,
                    updated_at=float(index),
                    parent_id=parent_id,
                )
            )
            parent_id = session_id

        code, stdout, stderr = self.run_cli(["list"], FakeScanner(sessions))

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(1201, len(stdout.splitlines()))
        branch_offsets = [
            line.index("↳")
            for line in stdout.splitlines()
            if "↳" in line
        ]
        self.assertLessEqual(max(branch_offsets), 12)

    def test_list_json_preserves_flat_input_order_with_parent_metadata(self):
        child = make_session("child", parent_id="parent", updated_at=300.0)
        parent = make_session("parent", updated_at=100.0)

        code, stdout, stderr = self.run_cli(
            ["list", "--json"],
            FakeScanner([child, parent]),
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(
            [child.to_dict(), parent.to_dict()],
            json.loads(stdout),
        )

    def test_list_prefers_daemon_status_without_scanning(self):
        session = make_session("daemon", updated_at=time.time())
        scanner = FakeScanner([make_session("direct")])
        client = FakeClient([session.to_dict()])
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["list", "--json"],
            scanner=scanner,
            client=client,
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(0, code)
        self.assertEqual([session.to_dict()], json.loads(stdout.getvalue()))
        self.assertEqual(1, client.status_calls)
        self.assertEqual([], scanner.recent_calls)
        self.assertEqual("", stderr.getvalue())

    def test_daemon_scan_errors_use_stderr_and_keep_json_stdout_clean(self):
        session = make_session("daemon", updated_at=time.time())
        scan_error = {
            "adapter": "broken",
            "stage": "discover",
            "message": "unreadable",
            "exception_type": "OSError",
            "session_id": "daemon",
        }

        for argv in (["list", "--json"], ["status", "--json"]):
            with self.subTest(command=argv[0]):
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = main(
                    argv,
                    scanner=FakeScanner([make_session("direct")]),
                    client=FakeClient([session.to_dict()], scan_errors=[scan_error]),
                    stdout=stdout,
                    stderr=stderr,
                )

                self.assertEqual(0, code)
                self.assertEqual([session.to_dict()], json.loads(stdout.getvalue()))
                self.assertEqual(
                    "scan error: broken discover session=daemon: unreadable\n",
                    stderr.getvalue(),
                )
                self.assertNotIn("scan error", stdout.getvalue())

    def test_status_silently_falls_back_when_daemon_is_unavailable(self):
        session = make_session("direct", Status.WORKING)
        stdout = io.StringIO()
        stderr = io.StringIO()
        scanner = FakeScanner([session])

        code = main(
            ["status", "--json"],
            scanner=scanner,
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
        )

        self.assertEqual(0, code)
        self.assertEqual([session.to_dict()], json.loads(stdout.getvalue()))
        self.assertEqual([None], scanner.recent_calls)
        self.assertEqual("", stderr.getvalue())

    def test_watch_rejects_ambiguous_prefix_without_starting_tailer(self):
        scanner = FakeScanner([make_session("abc-one"), make_session("abc-two")])
        stdout = io.StringIO()
        stderr = io.StringIO()
        called = []

        def watcher(*args, **kwargs):
            called.append((args, kwargs))
            return iter(())

        code = main(
            ["watch", "abc", "--json"],
            scanner=scanner,
            stdout=stdout,
            stderr=stderr,
            watch_provider=watcher,
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("ambiguous session prefix 'abc'", stderr.getvalue())
        self.assertEqual([], called)

    def test_watch_rejects_selected_session_without_direct_event_source(self):
        session = make_session("cursor-cli-empty", agent="cursor-cli", transcript="")
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []

        def watcher(*args, **kwargs):
            calls.append((args, kwargs))
            return iter(())

        code = main(
            ["watch", "cursor-cli", "--json"],
            scanner=FakeScanner([session]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
            watch_provider=watcher,
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(
            "watch: session cursor-cli:cursor-cli-empty has no usable direct event source\n",
            stderr.getvalue(),
        )
        self.assertEqual([], calls)

    def test_watch_json_uses_event_schema(self):
        session = make_session("unique")
        source = "  \x1b]0;pwned\x07\x1b[31mdone\rreplaced\x9b2J  "
        text = "\x1b]0;pwned\x07\x1b[31mdone replaced\x9b2J"
        event = Event(
            "2026-08-23T04:00:00+08:00",
            "claude",
            "unique",
            "assistant",
            snip(source, 200),
        )

        def watcher(sessions, from_start=False):
            self.assertEqual([session], sessions)
            self.assertTrue(from_start)
            return iter((event,))

        stdout = io.StringIO()
        code = main(
            ["watch", "uni", "--from-start", "--json"],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=watcher,
        )

        self.assertEqual(0, code)
        self.assertEqual(event.to_dict(), json.loads(stdout.getvalue()))
        self.assertEqual(text, json.loads(stdout.getvalue())["text"])

    def test_watch_human_output_sanitizes_event_fields(self):
        session = make_session("unique")
        source = (
            "  \x1b]0;pwned\x07\x1b[31m完成\x1b[0m\r覆写"
            "\x1b[2J\x9b999C\x9d0;c1-pwned\x9c  "
        )
        event = Event(
            "2026-08-23T04:00:00+08:00",
            "claude",
            "unique",
            "assistant",
            snip(source, 200),
        )

        def watcher(sessions, from_start=False):
            self.assertEqual([session], sessions)
            self.assertTrue(from_start)
            return iter((event,))

        stdout = io.StringIO()
        code = main(
            ["watch", "uni", "--from-start"],
            scanner=FakeScanner([session]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=watcher,
        )

        rendered = stdout.getvalue()
        self.assertEqual(0, code)
        self.assertIn("完成 覆写", rendered)
        self.assertNotIn("pwned", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x9b", rendered)
        self.assertNotIn("\r", rendered)

    def test_watch_all_prefers_daemon_subscription_without_scanning(self):
        event = Event(
            "2026-08-23T04:00:00+08:00",
            "claude",
            "daemon-session",
            "assistant",
            "done",
        )
        client = FakeClient(events=[event.to_dict()])
        scanner = FakeScanner([make_session("direct")])
        direct_calls = []

        def direct_watcher(*args, **kwargs):
            direct_calls.append((args, kwargs))
            return iter(())

        stdout = io.StringIO()
        code = main(
            ["watch", "--all", "--json"],
            scanner=scanner,
            client=client,
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=direct_watcher,
        )

        self.assertEqual(0, code)
        self.assertEqual(event.to_dict(), json.loads(stdout.getvalue()))
        self.assertEqual(1, client.subscribe_calls)
        self.assertEqual([], scanner.recent_calls)
        self.assertEqual([], direct_calls)

    def test_watch_all_from_start_bypasses_reachable_daemon(self):
        session = make_session("direct")
        event = Event(
            "2026-08-23T04:00:00+08:00",
            "claude",
            "direct",
            "assistant",
            "replayed",
        )
        client = FakeClient(events=[{"text": "future-only"}])
        scanner = FakeScanner([session])
        direct_calls = []

        def direct_watcher(sessions, from_start=False):
            direct_calls.append((sessions, from_start))
            return iter((event,))

        stdout = io.StringIO()
        code = main(
            ["watch", "--all", "--from-start", "--json"],
            scanner=scanner,
            client=client,
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=direct_watcher,
        )

        self.assertEqual(0, code)
        self.assertEqual(event.to_dict(), json.loads(stdout.getvalue()))
        self.assertEqual(0, client.subscribe_calls)
        self.assertEqual([None], scanner.recent_calls)
        self.assertEqual([([session], True)], direct_calls)

    def test_watch_all_falls_back_to_direct_watcher(self):
        session = make_session("direct")
        event = Event(
            "2026-08-23T04:00:00+08:00",
            "claude",
            "direct",
            "assistant",
            "fallback",
        )
        calls = []

        def direct_watcher(sessions, from_start=False):
            calls.append((sessions, from_start))
            return iter((event,))

        stdout = io.StringIO()
        code = main(
            ["watch", "--all", "--from-start", "--json"],
            scanner=FakeScanner([session]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=direct_watcher,
        )

        self.assertEqual(0, code)
        self.assertEqual(event.to_dict(), json.loads(stdout.getvalue()))
        self.assertEqual([([session], True)], calls)

    def test_watch_all_direct_fallback_skips_unsupported_sessions(self):
        jsonl = make_session("jsonl")
        unsupported = make_session("copilot-empty", agent="copilot", transcript="")
        dsh = make_session(
            "dsh-zstd",
            agent="dsh",
            transcript="/tmp/dsh-zstd/session.jsonl.zstd",
        )
        calls = []

        def direct_watcher(sessions, from_start=False):
            calls.append((sessions, from_start))
            return iter(())

        stderr = io.StringIO()
        code = main(
            ["watch", "--all", "--from-start", "--json"],
            scanner=FakeScanner([jsonl, unsupported, dsh]),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=stderr,
            watch_provider=direct_watcher,
        )

        self.assertEqual(0, code)
        self.assertEqual([([jsonl, dsh], True)], calls)
        self.assertEqual(
            "watch: skipped 1 unsupported session without usable direct event sources\n",
            stderr.getvalue(),
        )

    @mock.patch("sidecar.cli.subprocess.Popen")
    def test_daemon_start_spawns_detached_and_waits_for_ping(self, popen):
        class StartingClient:
            def __init__(self):
                self.calls = 0

            def ping(self):
                self.calls += 1
                if self.calls == 1:
                    raise SidecarClientError("offline", code="connection_failed")
                return {"ok": True, "op": "ping", "pid": 7654}

        popen.return_value.poll.return_value = None
        stdout = io.StringIO()
        code = main(
            ["daemon", "start"],
            client=StartingClient(),
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, code)
        self.assertIn("7654", stdout.getvalue())
        command = popen.call_args.args[0]
        self.assertEqual(["-m", "sidecar", "daemon", "run"], command[1:])
        self.assertEqual(
            str(Path(__file__).resolve().parents[1]),
            popen.call_args.kwargs["cwd"],
        )
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    @mock.patch("sidecar.cli.subprocess.Popen")
    def test_daemon_start_is_idempotent_when_already_running(self, popen):
        stdout = io.StringIO()

        code = main(
            ["daemon", "start"],
            client=FakeClient(pid=99),
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, code)
        self.assertIn("already running", stdout.getvalue())
        popen.assert_not_called()

    @mock.patch("sidecar.cli.os.kill")
    @mock.patch("sidecar.cli.read_pid", return_value=98)
    def test_daemon_stop_refuses_mismatched_pidfile(self, _read_pid, kill):
        stderr = io.StringIO()

        code = main(
            ["daemon", "stop"],
            client=FakeClient(pid=99),
            stdout=io.StringIO(),
            stderr=stderr,
        )

        self.assertEqual(2, code)
        self.assertIn("refusing to signal", stderr.getvalue())
        kill.assert_not_called()

    @mock.patch("sidecar.cli.os.kill")
    def test_daemon_stop_signals_only_verified_pid(self, kill):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            pidfile = runtime / "daemon.pid"
            pidfile.write_text("55\n", encoding="ascii")

            class StoppingClient(FakeClient):
                def __init__(self):
                    super().__init__(pid=55, socket_path=runtime / "daemon.sock")
                    self.pings = 0

                def ping(self):
                    self.pings += 1
                    if self.pings > 1:
                        raise SidecarClientError("offline", code="connection_failed")
                    return super().ping()

            stdout = io.StringIO()
            code = main(
                ["daemon", "stop"],
                client=StoppingClient(),
                stdout=stdout,
                stderr=io.StringIO(),
            )

            self.assertEqual(0, code)
            kill.assert_called_once_with(55, mock.ANY)
            self.assertFalse(pidfile.exists())
            self.assertIn("stopped", stdout.getvalue())

    def test_daemon_status_exit_codes(self):
        running_output = io.StringIO()
        stopped_output = io.StringIO()

        running = main(
            ["daemon", "status"],
            client=FakeClient(pid=44),
            stdout=running_output,
            stderr=io.StringIO(),
        )
        stopped = main(
            ["daemon", "status"],
            client=OfflineClient(),
            stdout=stopped_output,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, running)
        self.assertEqual(1, stopped)
        self.assertIn("44", running_output.getvalue())
        self.assertIn("not running", stopped_output.getvalue())

    def test_tui_command_passes_once_to_runner(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return 7

        stdout = io.StringIO()
        code = main(
            ["tui", "--once"],
            scanner=FakeScanner(),
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            tui_runner=runner,
        )

        self.assertEqual(7, code)
        self.assertTrue(calls[0]["once"])
        self.assertIs(stdout, calls[0]["stdout"])


class RegistryTests(unittest.TestCase):
    def test_registry_contains_all_canonical_agents_and_cursor_alias(self):
        canonical_agents = {
            "cursor-ide",
            "cursor-cli",
            "claude",
            "codex",
            "copilot",
            "dsh",
            "kimi",
        }

        self.assertTrue(canonical_agents.issubset(registry))
        self.assertIs(get_adapter("cursor"), get_adapter("cursor-ide"))
        self.assertIs(get_adapter("cursor"), get_adapter("cursor-cli"))
        self.assertEqual(
            {"cursor", "claude", "codex", "copilot", "dsh", "kimi"},
            {adapter.name for adapter in iter_adapters()},
        )


class FollowerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write_records(self, path, records, mode="w"):
        with path.open(mode, encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")

    def test_jsonl_follower_replays_appends_and_resets_after_truncate(self):
        path = self.root / "events.jsonl"
        self.write_records(path, [{"value": "first"}])
        follower = JSONLFollower(path, from_start=True)

        self.assertEqual([{"value": "first"}], follower.poll())
        self.write_records(path, [{"value": "second"}], mode="a")
        self.assertEqual([{"value": "second"}], follower.poll())
        replacement = {"value": "replacement after truncate that regrew beyond the old offset"}
        self.write_records(path, [replacement])
        self.assertEqual([replacement], follower.poll())

    def test_missing_file_is_followed_when_it_appears(self):
        path = self.root / "later.jsonl"
        follower = JSONLFollower(path)

        self.assertEqual([], follower.poll())
        self.write_records(path, [{"value": "created"}])
        self.assertEqual([{"value": "created"}], follower.poll())

    def test_dsh_replay_from_start_deduplicates_by_sequence(self):
        adapter = ReplayAdapter([{"seq": 1}, {"seq": 2}])
        session = make_session("dsh-session", agent="dsh")
        tailer = SessionTailer(session, adapter=adapter, from_start=True)

        self.assertEqual(["1", "2"], [event.text for event in tailer.poll()])
        self.assertEqual([], tailer.poll())
        self.assertEqual([None, 2], adapter.calls)


if __name__ == "__main__":
    unittest.main()
