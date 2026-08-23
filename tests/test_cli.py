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
from sidecar.inject import DEFAULT_SEND_TIMEOUT_SECONDS, SendError, SendResult
from sidecar.model import Event, Session, Status
from sidecar.presentation import format_age_seconds, row_age, row_value
from sidecar.remote import RemoteAggregate, RemoteFailure, RemoteInventoryError
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

    def test_global_version_is_exact_v030(self):
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])

        self.assertEqual(0, raised.exception.code)
        self.assertEqual("0.3.0", sidecar.__version__)
        self.assertEqual("agent-sidecar 0.3.0\n", stdout.getvalue())

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

    def test_remote_help_selection_and_host_without_remote_conflict(self):
        parser = build_parser()
        command_parsers = next(
            action.choices
            for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )
        parsed = parser.parse_args(
            ["status", "--remote", "--host", "Edge", "--host", "SECOND"]
        )

        self.assertTrue(parsed.remote)
        self.assertEqual(["Edge", "SECOND"], parsed.host)
        for command in ("list", "status"):
            help_text = command_parsers[command].format_help()
            self.assertIn("--remote", help_text)
            self.assertIn("--host ALIAS", help_text)

            stdout = io.StringIO()
            stderr = io.StringIO()
            code = main(
                [command, "--host", "edge"],
                scanner=FakeScanner(),
                client=OfflineClient(),
                stdout=stdout,
                stderr=stderr,
                remote_aggregator=lambda *args, **kwargs: self.fail(
                    "remote aggregator must not run"
                ),
            )
            self.assertEqual(2, code)
            self.assertEqual("", stdout.getvalue())
            self.assertEqual(
                "{}: --host requires --remote\n".format(command),
                stderr.getvalue(),
            )

    def test_default_local_json_never_calls_remote_or_adds_host(self):
        session = make_session("strictly-local")
        stdout = io.StringIO()

        code = main(
            ["list", "--json"],
            scanner=FakeScanner([session]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            remote_aggregator=lambda *args, **kwargs: self.fail(
                "default list must stay local"
            ),
        )

        self.assertEqual(0, code)
        self.assertEqual([session.to_dict()], json.loads(stdout.getvalue()))
        self.assertNotIn("host", json.loads(stdout.getvalue())[0])

    def test_remote_control_errors_preserve_local_json_and_human_views(self):
        cases = (
            (
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    RemoteInventoryError()
                ),
                "remote: inventory\n",
            ),
            (
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    ValueError(
                        "selected remote host is not eligible: "
                        "typo\x1b]0;pwned\x07"
                    )
                ),
                "remote: selected remote host is not eligible: typo\n",
            ),
        )
        now = time.time()
        for command in ("list", "status"):
            local = make_session(
                "{}-local".format(command),
                Status.WORKING,
                updated_at=now,
            )
            local_older = make_session(
                "{}-local-older".format(command),
                Status.WORKING,
                updated_at=now - 10,
            )
            filtered = make_session(
                "{}-filtered".format(command),
                Status.IDLE if command == "status" else Status.WORKING,
                updated_at=(
                    now + 100
                    if command == "status"
                    else now - RECENT_SECONDS - 1
                ),
            )
            for as_json in (False, True):
                for aggregator, expected_error in cases:
                    with self.subTest(
                        command=command,
                        as_json=as_json,
                        expected_error=expected_error,
                    ):
                        scanner = FakeScanner([make_session("not-scanned")])
                        client = FakeClient(
                            [
                                filtered.to_dict(),
                                local_older.to_dict(),
                                local.to_dict(),
                            ]
                        )
                        stdout = io.StringIO()
                        stderr = io.StringIO()
                        argv = [
                            command,
                            "--remote",
                            "--host",
                            "typo",
                        ]
                        if as_json:
                            argv.append("--json")

                        code = main(
                            argv,
                            scanner=scanner,
                            client=client,
                            stdout=stdout,
                            stderr=stderr,
                            remote_aggregator=aggregator,
                        )

                        self.assertEqual(2, code)
                        self.assertEqual(expected_error, stderr.getvalue())
                        self.assertEqual(1, client.status_calls)
                        self.assertEqual([], scanner.recent_calls)
                        if as_json:
                            self.assertEqual(
                                [
                                    dict(local.to_dict(), host="local"),
                                    dict(local_older.to_dict(), host="local"),
                                ],
                                json.loads(stdout.getvalue()),
                            )
                        else:
                            lines = stdout.getvalue().splitlines()
                            self.assertEqual(
                                [
                                    "HOST",
                                    "AGENT",
                                    "STATUS",
                                    "SESSION",
                                    "AGE",
                                    "UPDATED",
                                    "TITLE",
                                ],
                                lines[0].split(),
                            )
                            self.assertIn("local", lines[1])
                            self.assertIn(local.session_id, lines[1])
                            self.assertIn(local_older.session_id, lines[2])
                            self.assertNotIn(
                                filtered.session_id,
                                stdout.getvalue(),
                            )

    def test_remote_control_error_scanner_fallback_runs_once(self):
        local = make_session(
            "scanner-local",
            Status.WAITING,
            updated_at=time.time(),
        )
        scanner = FakeScanner([local])
        stdout = io.StringIO()

        code = main(
            ["list", "--remote", "--json"],
            scanner=scanner,
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            remote_aggregator=lambda *args, **kwargs: (_ for _ in ()).throw(
                RemoteInventoryError()
            ),
        )

        self.assertEqual(2, code)
        self.assertEqual([None], scanner.recent_calls)
        self.assertEqual(
            [dict(local.to_dict(), host="local")],
            json.loads(stdout.getvalue()),
        )

    def test_remote_setup_oserror_preserves_local_json_and_human_views(self):
        for command in ("list", "status"):
            for as_json in (False, True):
                with self.subTest(command=command, as_json=as_json):
                    local = make_session(
                        "{}-setup-local".format(command),
                        Status.WORKING,
                        updated_at=time.time(),
                    )
                    client = FakeClient([local.to_dict()])
                    scanner = FakeScanner([make_session("not-scanned")])
                    provider_calls = []

                    def aggregate(operation, selected=None):
                        provider_calls.append((operation, selected))
                        raise OSError("sensitive zipapp setup detail")

                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    argv = [command, "--remote"]
                    if as_json:
                        argv.append("--json")

                    code = main(
                        argv,
                        scanner=scanner,
                        client=client,
                        stdout=stdout,
                        stderr=stderr,
                        remote_aggregator=aggregate,
                    )

                    self.assertEqual(2, code)
                    self.assertEqual("remote: setup\n", stderr.getvalue())
                    self.assertNotIn("sensitive", stderr.getvalue())
                    self.assertEqual([(command, None)], provider_calls)
                    self.assertEqual(1, client.status_calls)
                    self.assertEqual([], scanner.recent_calls)
                    if as_json:
                        self.assertEqual(
                            [dict(local.to_dict(), host="local")],
                            json.loads(stdout.getvalue()),
                        )
                    else:
                        lines = stdout.getvalue().splitlines()
                        self.assertEqual(
                            [
                                "HOST",
                                "AGENT",
                                "STATUS",
                                "SESSION",
                                "AGE",
                                "UPDATED",
                                "TITLE",
                            ],
                            lines[0].split(),
                        )
                        self.assertIn("local", lines[1])
                        self.assertIn(local.session_id, lines[1])

    def test_remote_setup_isolation_does_not_catch_programmer_errors(self):
        client = FakeClient([make_session("local").to_dict()])
        calls = []

        def aggregate(command, selected=None):
            calls.append((command, selected))
            raise RuntimeError("programmer error")

        with self.assertRaisesRegex(RuntimeError, "programmer error"):
            main(
                ["list", "--remote", "--json"],
                scanner=FakeScanner(),
                client=client,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                remote_aggregator=aggregate,
            )

        self.assertEqual([("list", None)], calls)
        self.assertEqual(1, client.status_calls)

    def test_remote_all_old_python_prints_local_rows_and_exits_three(self):
        local = make_session(
            "local-working",
            Status.WORKING,
            updated_at=time.time(),
        )
        result = RemoteAggregate(
            "status",
            failures=(RemoteFailure("old-edge", "python_too_old"),),
            hosts=("old-edge",),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["status", "--remote", "--json"],
            scanner=FakeScanner([local]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
            remote_aggregator=lambda command, selected=None: result,
        )

        self.assertEqual(3, code)
        self.assertEqual(
            [dict(local.to_dict(), host="local")],
            json.loads(stdout.getvalue()),
        )
        self.assertEqual(
            "remote old-edge: python_too_old\n",
            stderr.getvalue(),
        )

    def test_remote_partial_status_is_flat_filtered_and_status_sorted(self):
        now = time.time()
        local = make_session(
            "local-waiting",
            Status.WAITING,
            updated_at=now + 20,
        )
        remote_working = dict(
            make_session(
                "remote-working",
                Status.WORKING,
                updated_at=now,
            ).to_dict(),
            host="Good.Edge",
        )
        remote_idle = dict(
            make_session(
                "remote-idle",
                Status.IDLE,
                updated_at=now + 100,
            ).to_dict(),
            host="Good.Edge",
        )
        result = RemoteAggregate(
            "status",
            rows=(remote_idle, remote_working),
            failures=(RemoteFailure("bad-edge", "auth"),),
            hosts=("bad-edge", "Good.Edge"),
            succeeded=("Good.Edge",),
        )
        calls = []

        def aggregate(command, selected=None):
            calls.append((command, selected))
            return result

        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            ["status", "--remote", "--host", "good.edge", "--json"],
            scanner=FakeScanner([local]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
            remote_aggregator=aggregate,
        )
        rows = json.loads(stdout.getvalue())

        self.assertEqual(0, code)
        self.assertEqual([("status", ["good.edge"])], calls)
        self.assertEqual(
            ["remote-working", "local-waiting"],
            [row["session_id"] for row in rows],
        )
        self.assertEqual(
            ["Good.Edge", "local"],
            [row["host"] for row in rows],
        )
        self.assertEqual("remote bad-edge: auth\n", stderr.getvalue())
        self.assertNotIn("remote-idle", stdout.getvalue())

    def test_remote_list_applies_recent_then_agent_filter_after_merge(self):
        now = time.time()
        local_recent = make_session(
            "local-recent",
            agent="claude",
            updated_at=now + 5,
        )
        local_old = make_session(
            "local-old",
            agent="claude",
            updated_at=now - RECENT_SECONDS - 1,
        )
        remote_recent = dict(
            make_session(
                "remote-recent",
                agent="CLAUDE",
                updated_at=now + 10,
            ).to_dict(),
            host="edge",
        )
        remote_old = dict(
            make_session(
                "remote-old",
                agent="claude",
                updated_at=now - RECENT_SECONDS - 1,
            ).to_dict(),
            host="edge",
        )
        other_agent = dict(
            make_session(
                "remote-codex",
                agent="codex",
                updated_at=now + 20,
            ).to_dict(),
            host="edge",
        )
        result = RemoteAggregate(
            "list",
            rows=(remote_old, other_agent, remote_recent),
            hosts=("edge",),
            succeeded=("edge",),
        )
        scanner = FakeScanner([local_old, local_recent])
        stdout = io.StringIO()

        code = main(
            ["list", "--remote", "--agent", "cLaUdE", "--json"],
            scanner=scanner,
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            remote_aggregator=lambda command, selected=None: result,
        )
        rows = json.loads(stdout.getvalue())

        self.assertEqual(0, code)
        self.assertEqual([None], scanner.recent_calls)
        self.assertEqual(
            ["remote-recent", "local-recent"],
            [row["session_id"] for row in rows],
        )
        self.assertEqual(
            ["edge", "local"],
            [row["host"] for row in rows],
        )

    def test_remote_all_includes_old_rows_and_human_tables_have_host_header(self):
        now = time.time()
        local_parent = make_session(
            "shared-parent",
            Status.WAITING,
            updated_at=now,
        )
        cross_host_child = dict(
            make_session(
                "cross-host-child",
                Status.WAITING,
                updated_at=now + 10,
                parent_id="shared-parent",
            ).to_dict(),
            host="edge",
        )
        remote_old = dict(
            make_session(
                "remote-old",
                Status.WAITING,
                updated_at=now - RECENT_SECONDS - 1,
            ).to_dict(),
            host="edge",
        )

        for command, extra in (("list", ["--all"]), ("status", [])):
            with self.subTest(command=command):
                result = RemoteAggregate(
                    command,
                    rows=(remote_old, cross_host_child),
                    hosts=("edge",),
                    succeeded=("edge",),
                )
                stdout = io.StringIO()
                code = main(
                    [command, "--remote"] + extra,
                    scanner=FakeScanner([local_parent]),
                    client=OfflineClient(),
                    stdout=stdout,
                    stderr=io.StringIO(),
                    remote_aggregator=lambda operation, selected=None, result=result: result,
                )
                lines = stdout.getvalue().splitlines()
                child_line = next(
                    line for line in lines if "cross-host-child" in line
                )

                self.assertEqual(0, code)
                self.assertEqual(
                    ["HOST", "AGENT", "STATUS", "SESSION", "AGE", "UPDATED", "TITLE"],
                    lines[0].split(),
                )
                self.assertFalse(child_line.startswith("↳"))
                self.assertNotIn("↳ edge", child_line)
                if command == "list":
                    self.assertIn("remote-old", stdout.getvalue())

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

    def test_watch_cursor_chat_db_direct_with_from_start(self):
        session = make_session(
            "cursor-chat",
            agent="cursor-cli",
            transcript="/tmp/cursor-chat/store.db",
            extra={"transcript_kind": "cursor-chat-sqlite"},
        )
        event = Event(
            "2026-08-23T04:00:00+08:00",
            "cursor-cli",
            "cursor-chat",
            "assistant",
            "watched from sqlite",
        )
        calls = []

        def watcher(sessions, from_start=False):
            calls.append((sessions, from_start))
            return iter((event,))

        stdout = io.StringIO()
        code = main(
            ["watch", "cursor-chat", "--from-start", "--json"],
            scanner=FakeScanner([session]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=watcher,
        )

        self.assertEqual(0, code)
        self.assertEqual(event.to_dict(), json.loads(stdout.getvalue()))
        self.assertEqual([([session], True)], calls)

    def test_watch_rejects_legacy_cursor_db_without_transcript_kind(self):
        session = make_session(
            "legacy-cursor",
            agent="cursor-cli",
            transcript="/tmp/legacy/store.db",
            extra={"source": "cli", "store": "/tmp/legacy/store.db"},
        )
        calls = []

        code = main(
            ["watch", "legacy-cursor", "--json"],
            scanner=FakeScanner([session]),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            watch_provider=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

        self.assertEqual(2, code)
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

    def test_watch_all_cursor_chat_uses_daemon_subscription(self):
        event = Event(
            "2026-08-23T04:00:00+08:00",
            "cursor-cli",
            "cursor-chat",
            "assistant",
            "daemon sqlite event",
        )
        client = FakeClient(events=[event.to_dict()])
        stdout = io.StringIO()

        code = main(
            ["watch", "--all", "--json"],
            scanner=FakeScanner(),
            client=client,
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=lambda *args, **kwargs: iter(()),
        )

        self.assertEqual(0, code)
        self.assertEqual(event.to_dict(), json.loads(stdout.getvalue()))
        self.assertEqual(1, client.subscribe_calls)

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
        cursor_chat = make_session(
            "cursor-chat",
            agent="cursor-cli",
            transcript="/tmp/cursor-chat/store.db",
            extra={"transcript_kind": "cursor-chat-sqlite"},
        )
        legacy_cursor = make_session(
            "legacy-cursor",
            agent="cursor-cli",
            transcript="/tmp/legacy-cursor/store.db",
            extra={"source": "cli"},
        )
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
            scanner=FakeScanner(
                [jsonl, unsupported, cursor_chat, legacy_cursor, dsh]
            ),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=stderr,
            watch_provider=direct_watcher,
        )

        self.assertEqual(0, code)
        self.assertEqual([([jsonl, cursor_chat, dsh], True)], calls)
        self.assertEqual(
            "watch: skipped 2 unsupported sessions without usable direct event sources\n",
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


class SendCLITests(unittest.TestCase):
    def result(
        self,
        session,
        *,
        outcome="completed",
        response="",
        stderr="",
        error_code=None,
    ):
        return SendResult(
            agent=session.agent,
            session_id=session.session_id,
            outcome=outcome,
            delivery="delivered" if outcome == "completed" else "unknown",
            returncode=0 if outcome == "completed" else 7,
            response=response,
            stderr=stderr,
            error_code=error_code,
        )

    def test_parser_and_help_describe_mutating_local_headless_resume(self):
        parser = build_parser()
        command_parsers = next(
            action.choices
            for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )
        global_help = parser.format_help()
        send_help = command_parsers["send"].format_help()
        args = parser.parse_args(["send", "abc", "hello"])
        hyphen = parser.parse_args(
            ["send", "abc", "--allow-write", "--", "-private"]
        )

        self.assertIn("Observation commands are read-only", global_help)
        self.assertIn("may modify agent state", global_help)
        self.assertNotIn("without modifying", global_help)
        self.assertIn("local headless resume", send_help)
        self.assertIn("may modify agent state", send_help)
        self.assertIn("--allow-write", send_help)
        self.assertIn("--timeout SEC", send_help)
        self.assertIn("--json", send_help)
        self.assertNotIn("--remote", send_help)
        self.assertEqual("abc", args.session_prefix)
        self.assertEqual("hello", args.message)
        self.assertFalse(args.allow_write)
        self.assertEqual(DEFAULT_SEND_TIMEOUT_SECONDS, args.timeout)
        self.assertEqual("-private", hyphen.message)
        self.assertTrue(hyphen.allow_write)

    def test_allow_write_abbreviations_are_parser_errors_before_scanning(self):
        for abbreviation in ("--a", "--allow", "--allow-writ"):
            with self.subTest(abbreviation=abbreviation):
                scanner = FakeScanner([make_session("abc")])
                calls = []
                argparse_stderr = io.StringIO()

                with contextlib.redirect_stderr(argparse_stderr):
                    with self.assertRaises(SystemExit) as raised:
                        main(
                            ["send", "abc", "private", abbreviation],
                            scanner=scanner,
                            stdout=io.StringIO(),
                            stderr=io.StringIO(),
                            send_planner=lambda *args, **kwargs: calls.append(
                                ("plan", args, kwargs)
                            ),
                            send_executor=lambda *args, **kwargs: calls.append(
                                ("execute", args, kwargs)
                            ),
                        )

                self.assertEqual(2, raised.exception.code)
                self.assertIn("unrecognized arguments", argparse_stderr.getvalue())
                self.assertEqual([], scanner.recent_calls)
                self.assertEqual([], calls)

    def test_missing_allow_write_stops_before_scan_plan_or_execute(self):
        message = "DO-NOT-ECHO"
        scanner = FakeScanner([make_session("abc")])
        client = FakeClient([make_session("daemon").to_dict()])
        calls = []
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["send", "abc", message],
            scanner=scanner,
            client=client,
            stdout=stdout,
            stderr=stderr,
            remote_aggregator=lambda *args, **kwargs: self.fail(
                "send must not use remote aggregation"
            ),
            send_planner=lambda *args, **kwargs: calls.append(("plan", args, kwargs)),
            send_executor=lambda *args, **kwargs: calls.append(
                ("execute", args, kwargs)
            ),
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("--allow-write", stderr.getvalue())
        self.assertNotIn(message, stderr.getvalue())
        self.assertEqual([], scanner.recent_calls)
        self.assertEqual(0, client.status_calls)
        self.assertEqual([], calls)

    def test_send_uses_exact_first_and_unique_prefix_resolution(self):
        exact = make_session("abc")
        longer = make_session("abc-extra")
        unique = make_session("unique-session")

        for prefix, expected in (("abc", exact), ("unique", unique)):
            with self.subTest(prefix=prefix):
                scanner = FakeScanner([longer, unique, exact])
                selected = []

                def planner(session, message):
                    selected.append((session, message))
                    return object()

                code = main(
                    ["send", prefix, "private", "--allow-write"],
                    scanner=scanner,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    send_planner=planner,
                    send_executor=lambda plan, **kwargs: self.result(expected),
                )

                self.assertEqual(0, code)
                self.assertEqual([(expected, "private")], selected)
                self.assertEqual([None], scanner.recent_calls)

    def test_send_rejects_ambiguous_and_missing_prefixes_before_planning(self):
        cases = (
            (
                "amb",
                [make_session("amb-one"), make_session("amb-two")],
                "ambiguous session prefix",
            ),
            ("missing", [make_session("other")], "no session matches prefix"),
        )
        for prefix, sessions, expected in cases:
            with self.subTest(prefix=prefix):
                message = "PRIVATE-MESSAGE"
                scanner = FakeScanner(sessions)
                calls = []
                stdout = io.StringIO()
                stderr = io.StringIO()

                code = main(
                    ["send", prefix, message, "--allow-write"],
                    scanner=scanner,
                    stdout=stdout,
                    stderr=stderr,
                    send_planner=lambda *args, **kwargs: calls.append(
                        (args, kwargs)
                    ),
                    send_executor=lambda *args, **kwargs: self.fail(
                        "executor must not run"
                    ),
                )

                self.assertEqual(2, code)
                self.assertEqual("", stdout.getvalue())
                self.assertIn("send: " + expected, stderr.getvalue())
                self.assertNotIn(message, stderr.getvalue())
                self.assertEqual([None], scanner.recent_calls)
                self.assertEqual([], calls)

    def test_supported_send_contract_is_direct_local_and_forwards_timeout(self):
        session = make_session("supported-session", agent="codex")
        scanner = FakeScanner([session])
        client = FakeClient([make_session("daemon").to_dict()])
        plan = object()
        planner_calls = []
        executor_calls = []
        stdout = io.StringIO()

        def planner(selected, message):
            planner_calls.append((selected, message))
            return plan

        def executor(selected_plan, **kwargs):
            executor_calls.append((selected_plan, kwargs))
            return self.result(session, response="done")

        code = main(
            [
                "send",
                "supported",
                "private prompt",
                "--allow-write",
                "--timeout",
                "42.5",
                "--json",
            ],
            scanner=scanner,
            client=client,
            stdout=stdout,
            stderr=io.StringIO(),
            remote_aggregator=lambda *args, **kwargs: self.fail(
                "send must not use remote aggregation"
            ),
            send_planner=planner,
            send_executor=executor,
        )

        self.assertEqual(0, code)
        self.assertEqual([(session, "private prompt")], planner_calls)
        self.assertEqual(
            [(plan, {"allow_write": True, "timeout": 42.5})],
            executor_calls,
        )
        self.assertEqual([None], scanner.recent_calls)
        self.assertEqual(0, client.status_calls)
        self.assertEqual(
            self.result(session, response="done").to_dict(),
            json.loads(stdout.getvalue()),
        )

    def test_json_success_is_exact_and_preserves_raw_result_text(self):
        session = make_session("json-session")
        result = self.result(
            session,
            response="\x1b[31mraw response\r",
            stderr="\x1b]0;raw warning\x07",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["send", "json", "private", "--allow-write", "--json"],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=stderr,
            send_planner=lambda selected, message: object(),
            send_executor=lambda plan, **kwargs: result,
        )

        self.assertEqual(0, code)
        self.assertEqual(result.to_dict(), json.loads(stdout.getvalue()))
        self.assertEqual("", stderr.getvalue())
        self.assertIn("\x1b", json.loads(stdout.getvalue())["response"])

    def test_human_success_sanitizes_response_stderr_and_redacts_message(self):
        session = make_session("human-session")
        message = "TOP-SECRET-MESSAGE"
        result = self.result(
            session,
            response="answer \x1b]0;pwned\x07 " + message,
            stderr="warning \x1b[31mred\x1b[0m " + message,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["send", "human", message, "--allow-write"],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=stderr,
            send_planner=lambda selected, private_message: object(),
            send_executor=lambda plan, **kwargs: result,
        )
        rendered = stdout.getvalue() + stderr.getvalue()

        self.assertEqual(0, code)
        self.assertIn("answer [message redacted]", stdout.getvalue())
        self.assertIn("native stderr: warning red [message redacted]", stderr.getvalue())
        self.assertNotIn(message, rendered)
        self.assertNotIn("pwned", rendered)
        self.assertNotIn("\x1b", rendered)

    def test_human_output_normalizes_lone_surrogates_without_message_echo(self):
        session = make_session("surrogate-human")
        message = "\ud800PRIVATE"
        result = self.result(
            session,
            response="answer " + message,
            stderr="warning \ud801",
        )
        stdout_bytes = io.BytesIO()
        stderr_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(
            stdout_bytes,
            encoding="utf-8",
            errors="strict",
            write_through=True,
        )
        stderr = io.TextIOWrapper(
            stderr_bytes,
            encoding="utf-8",
            errors="strict",
            write_through=True,
        )

        code = main(
            ["send", "surrogate", message, "--allow-write"],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=stderr,
            send_planner=lambda selected, private_message: object(),
            send_executor=lambda plan, **kwargs: result,
        )
        rendered = (stdout_bytes.getvalue() + stderr_bytes.getvalue()).decode("utf-8")

        self.assertEqual(0, code)
        self.assertIn("answer [message redacted]", rendered)
        self.assertIn("native stderr: warning \ufffd", rendered)
        self.assertNotIn("PRIVATE", rendered)
        self.assertNotIn("\\ud800", rendered)
        self.assertNotIn("\\ud801", rendered)

    def test_invalid_unicode_message_cannot_break_preflight_json_diagnostics(self):
        cases = (
            (
                "\ud800PRIVATE",
                "\ud800PRIVATE",
                FakeScanner(),
                "no session matches prefix",
            ),
            (
                "unicode-session",
                "\ud800PRIVATE",
                FakeScanner([make_session("unicode-session")]),
                "message must contain valid Unicode scalars",
            ),
        )
        for prefix, message, scanner, expected in cases:
            with self.subTest(expected=expected):
                stderr_bytes = io.BytesIO()
                stderr = io.TextIOWrapper(
                    stderr_bytes,
                    encoding="utf-8",
                    errors="strict",
                    write_through=True,
                )
                stdout = io.StringIO()
                executor_calls = []

                code = main(
                    [
                        "send",
                        prefix,
                        message,
                        "--allow-write",
                        "--json",
                    ],
                    scanner=scanner,
                    stdout=stdout,
                    stderr=stderr,
                    send_executor=lambda *args, **kwargs: executor_calls.append(
                        (args, kwargs)
                    ),
                )
                diagnostic = stderr_bytes.getvalue().decode("utf-8")

                self.assertEqual(2, code)
                self.assertEqual("", stdout.getvalue())
                self.assertIn(expected, diagnostic)
                self.assertNotIn("PRIVATE", diagnostic)
                self.assertNotIn("\\ud800", diagnostic)
                self.assertEqual([], executor_calls)

    def test_human_success_without_response_prints_delivered_receipt(self):
        session = make_session("empty-response", agent="cursor-cli")
        stdout = io.StringIO()

        code = main(
            ["send", "empty", "private", "--allow-write"],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=io.StringIO(),
            send_planner=lambda selected, message: object(),
            send_executor=lambda plan, **kwargs: self.result(session),
        )

        self.assertEqual(0, code)
        self.assertEqual(
            "delivered to cursor-cli:empty-response\n",
            stdout.getvalue(),
        )

    def test_runtime_failures_emit_receipts_diagnostics_and_exit_one(self):
        session = make_session("runtime-session")
        cases = (
            ("failed", "native_exit"),
            ("timed_out", "timeout"),
            ("overflow", "stdout_overflow"),
        )
        for outcome, error_code in cases:
            for as_json in (False, True):
                with self.subTest(outcome=outcome, as_json=as_json):
                    message = "RUNTIME-SECRET"
                    result = self.result(
                        session,
                        outcome=outcome,
                        stderr="native detail [message redacted]",
                        error_code=error_code,
                    )
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    argv = [
                        "send",
                        "runtime",
                        message,
                        "--allow-write",
                    ]
                    if as_json:
                        argv.append("--json")

                    code = main(
                        argv,
                        scanner=FakeScanner([session]),
                        stdout=stdout,
                        stderr=stderr,
                        send_planner=lambda selected, private_message: object(),
                        send_executor=lambda plan, **kwargs: result,
                    )

                    self.assertEqual(1, code)
                    if as_json:
                        self.assertEqual(
                            result.to_dict(),
                            json.loads(stdout.getvalue()),
                        )
                    else:
                        self.assertEqual(
                            "delivery unknown for claude:runtime-session ({})\n".format(
                                outcome
                            ),
                            stdout.getvalue(),
                        )
                    self.assertIn("delivery status is unknown", stderr.getvalue())
                    self.assertIn(error_code, stderr.getvalue())
                    self.assertNotIn(message, stdout.getvalue() + stderr.getvalue())
                    self.assertNotIn("\x1b", stderr.getvalue())

    def test_send_errors_from_planner_and_executor_are_preflight_exit_two(self):
        session = make_session("error-session")
        cases = (
            (
                lambda selected, message: (_ for _ in ()).throw(
                    SendError("working_session")
                ),
                lambda plan, **kwargs: self.fail("executor must not run"),
                "working sessions cannot be resumed",
            ),
            (
                lambda selected, message: object(),
                lambda plan, **kwargs: (_ for _ in ()).throw(
                    SendError("invalid_timeout")
                ),
                "timeout must be finite",
            ),
        )
        for planner, executor, expected in cases:
            with self.subTest(expected=expected):
                stdout = io.StringIO()
                stderr = io.StringIO()

                code = main(
                    ["send", "error", "PRIVATE", "--allow-write"],
                    scanner=FakeScanner([session]),
                    stdout=stdout,
                    stderr=stderr,
                    send_planner=planner,
                    send_executor=executor,
                )

                self.assertEqual(2, code)
                self.assertEqual("", stdout.getvalue())
                self.assertIn("send: " + expected, stderr.getvalue())
                self.assertNotIn("PRIVATE", stderr.getvalue())

    def test_keyboard_interrupt_reports_delivery_unknown_and_exits_130(self):
        session = make_session("interrupt-session")
        message = "INTERRUPT-SECRET"
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["send", "interrupt", message, "--allow-write"],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=stderr,
            send_planner=lambda selected, private_message: object(),
            send_executor=lambda plan, **kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt
            ),
        )

        self.assertEqual(130, code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("delivery status is unknown", stderr.getvalue())
        self.assertNotIn(message, stderr.getvalue())


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
