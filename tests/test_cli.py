import contextlib
import io
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import sidecar
import sidecar.cli as cli_module
from sidecar.adapters import get_adapter, iter_adapters, registry
from sidecar.adapters.base import (
    Adapter,
    sanitize_terminal_text,
    snip,
)
from sidecar.client import SidecarClientError
from sidecar.cli import (
    RECENT_SECONDS,
    TAIL_ERROR_DEDUPE_LIMIT,
    _PendingTailArgumentParser,
    _read_stdin_message,
    build_parser,
    main,
)
from sidecar.inject import (
    DEFAULT_SEND_TIMEOUT_SECONDS,
    MAX_MESSAGE_BYTES,
    SendError,
    SendResult,
    build_send_plan,
    execute_send,
)
from sidecar.launchd import ServiceResult
from sidecar.model import Event, Session, Status
from sidecar.presentation import format_age_seconds, row_age, row_value
from sidecar.remote import (
    RemoteAggregate,
    RemoteFailure,
    RemoteInventoryError,
    RemoteWatchEvent,
    RemoteWatchFailure,
    RemoteWatchReady,
)
from sidecar.release import ReleaseArtifact, ReleaseError
from sidecar.scan import ScanError
from sidecar.send_audit import AuditError, make_audit_identity
from sidecar.tail import JSONLFollower, SessionTailer
from sidecar.text_utils import normalize_scalar_text, redact_message


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
        tail_errors=(),
        http=None,
    ):
        self.sessions = list(sessions)
        self.events = list(events)
        self.pid = pid
        self.socket_path = socket_path
        self.scan_errors = list(scan_errors)
        self.tail_errors = list(tail_errors)
        self.http = http
        self.status_calls = 0
        self.subscribe_calls = 0

    def status(self):
        self.status_calls += 1
        return list(self.sessions)

    def subscribe(self):
        self.subscribe_calls += 1
        return iter(self.events)

    def ping(self):
        response = {"ok": True, "op": "ping", "pid": self.pid}
        if self.http is not None:
            response["http"] = dict(self.http)
        return response


class SequencedPingClient:
    def __init__(self, responses, *, socket_path=None):
        self.responses = list(responses)
        self.socket_path = socket_path

    def ping(self):
        if len(self.responses) > 1:
            value = self.responses.pop(0)
        else:
            value = self.responses[0]
        if isinstance(value, BaseException):
            raise value
        return value


class FakeDaemonProcess:
    def __init__(self, pid=7654, *, poll_error=None):
        self.pid = pid
        self.returncode = None
        self.poll_error = poll_error
        self.wait_calls = []

    def poll(self):
        if self.poll_error is not None:
            raise self.poll_error
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired(["daemon"], timeout)
        return self.returncode


def controlled_process_group(process, *, survive_term=False):
    active = {"value": True}
    signals = []

    def killpg(process_group, signum):
        if process_group != process.pid:
            raise AssertionError("unexpected process group")
        signals.append(signum)
        if signum == 0:
            if not active["value"]:
                raise ProcessLookupError
            return
        if signum == signal.SIGTERM:
            process.returncode = -signal.SIGTERM
            if not survive_term:
                active["value"] = False
            return
        if signum == signal.SIGKILL:
            process.returncode = -signal.SIGKILL
            active["value"] = False
            return
        raise AssertionError("unexpected signal")

    return signals, killpg


class FakeRemoteWatchSession:
    def __init__(self, items=(), hosts=("edge",), all_failed=False):
        self.items = list(items)
        self.hosts = tuple(hosts)
        self.empty = not self.hosts
        self.all_failed = all_failed
        self.closed = False

    def __iter__(self):
        return iter(self.items)

    def close(self):
        self.closed = True


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

    def test_shared_scalar_normalization_and_redaction_are_terminal_neutral(self):
        valid = "中文 😀\n\t\x1b[31m"
        self.assertEqual(valid, normalize_scalar_text(valid))
        self.assertEqual(valid, normalize_scalar_text(valid, errors="replace"))
        with self.assertRaises(UnicodeEncodeError):
            normalize_scalar_text("left\ud800right")
        self.assertEqual(
            "left\ufffdright",
            normalize_scalar_text("left\ud800right", errors="replace"),
        )
        with self.assertRaises(ValueError):
            normalize_scalar_text("text", errors="ignore")

        message = 'private "line"\n雪'
        escaped = json.dumps(message, ensure_ascii=False)[1:-1]
        ascii_escaped = json.dumps(message, ensure_ascii=True)[1:-1]
        redacted = redact_message(
            "{} | {} | {} | \x1b[31m".format(
                message,
                escaped,
                ascii_escaped,
            ),
            message,
        )
        self.assertEqual(3, redacted.count("[message redacted]"))
        self.assertTrue(redacted.endswith("\x1b[31m"))


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

    def test_global_version_matches_current_release(self):
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                main(["--version"])

        self.assertEqual(0, raised.exception.code)
        self.assertRegex(sidecar.__version__, r"^\d+\.\d+\.\d+$")
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

    def test_package_build_is_explicit_and_reports_artifact_identity(self):
        parser = build_parser()
        command_parsers = next(
            action.choices
            for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )
        package_parser = command_parsers["package"]
        package_commands = next(
            action.choices
            for action in package_parser._actions
            if getattr(action, "dest", None) == "package_command"
        )
        parsed_default = parser.parse_args(["package", "build"])
        parsed_custom = parser.parse_args(
            ["package", "build", "--output", "artifacts/custom.pyz"]
        )

        self.assertEqual(
            Path("dist/agent-sidecar.pyz"),
            parsed_default.output,
        )
        self.assertEqual(
            Path("artifacts/custom.pyz"),
            parsed_custom.output,
        )
        self.assertIn("build", package_parser.format_help())
        self.assertIn("--output PATH", package_commands["build"].format_help())

        output = Path("artifacts/custom.pyz")
        artifact = ReleaseArtifact(output, "a" * 64, 1234)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "sidecar.cli.build_release_zipapp",
            return_value=artifact,
        ) as builder:
            code = main(
                ["package", "build", "--output", str(output)],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(0, code)
        builder.assert_called_once_with(output)
        self.assertEqual(
            "path=artifacts/custom.pyz sha256={} size=1234\n".format(
                "a" * 64
            ),
            stdout.getvalue(),
        )
        self.assertEqual("", stderr.getvalue())

    def test_package_build_reports_safe_failure(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "sidecar.cli.build_release_zipapp",
            side_effect=ReleaseError("refusing unsafe output"),
        ):
            code = main(
                ["package", "build"],
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(1, code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(
            "package build: refusing unsafe output\n",
            stderr.getvalue(),
        )

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

        for command in ("list", "status", "watch"):
            self.assertIn(
                "--remote-python PATH",
                command_parsers[command].format_help(),
            )

    def test_remote_python_requires_remote_before_any_provider_runs(self):
        def unexpected(*args, **kwargs):
            self.fail("remote provider must not run: {!r} {!r}".format(args, kwargs))

        cases = (
            ["list", "--remote-python", "/opt/python3.8", "--json"],
            ["status", "--remote-python", "/opt/python3.8", "--json"],
            ["watch", "--all", "--remote-python", "/opt/python3.8", "--json"],
        )
        for argv in cases:
            with self.subTest(command=argv[0]):
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = main(
                    argv,
                    scanner=FakeScanner(),
                    client=OfflineClient(),
                    stdout=stdout,
                    stderr=stderr,
                    remote_aggregator=unexpected,
                    remote_watch_provider=unexpected,
                )

                self.assertEqual(2, code)
                self.assertEqual("", stdout.getvalue())
                self.assertEqual(
                    "{}: --remote-python requires --remote\n".format(argv[0]),
                    stderr.getvalue(),
                )

    def test_invalid_remote_python_cli_and_env_stop_before_remote_setup(self):
        invalid_values = (
            "",
            "python3",
            "relative/python3",
            "/opt/python 3",
            "/opt/python\n3",
            "/opt/python\x003",
            "/opt/'python3",
            "/" + "p" * 1024,
            "/opt/../python3",
        )

        def unexpected(*args, **kwargs):
            self.fail("remote provider must not run: {!r} {!r}".format(args, kwargs))

        for source, command in (("cli", "list"), ("env", "status")):
            for value in invalid_values:
                with self.subTest(source=source, value=repr(value)):
                    argv = [command, "--remote", "--json"]
                    environment = {}
                    if source == "cli":
                        argv.extend(("--remote-python", value))
                    else:
                        environment[cli_module.REMOTE_PYTHON_ENV] = value
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    patched_environment = dict(os.environ)
                    patched_environment.pop(cli_module.REMOTE_PYTHON_ENV, None)
                    patched_environment.update(environment)
                    with mock.patch.object(
                        cli_module.os,
                        "environ",
                        patched_environment,
                    ):
                        code = main(
                            argv,
                            scanner=FakeScanner(),
                            client=OfflineClient(),
                            stdout=stdout,
                            stderr=stderr,
                            remote_aggregator=unexpected,
                            remote_watch_provider=unexpected,
                        )

                    self.assertEqual(2, code)
                    self.assertEqual("", stdout.getvalue())
                    self.assertEqual(
                        "{}: --remote-python/{} must be a valid absolute "
                        "executable path\n".format(
                            command,
                            cli_module.REMOTE_PYTHON_ENV,
                        ),
                        stderr.getvalue(),
                    )

    def test_remote_python_env_is_ignored_without_remote_mode(self):
        scanner = FakeScanner()
        with mock.patch.dict(
            os.environ,
            {cli_module.REMOTE_PYTHON_ENV: "invalid relative path"},
            clear=False,
        ):
            code, stdout, stderr = self.run_cli(["list", "--json"], scanner)

        self.assertEqual(0, code)
        self.assertEqual([], json.loads(stdout))
        self.assertEqual("", stderr)
        self.assertEqual([RECENT_SECONDS], scanner.recent_calls)

    def test_remote_python_precedence_for_list_status_and_watch(self):
        cases = (
            ("default", None, [], None),
            ("env", "/env/python3.8", [], ("/env/python3.8",)),
            (
                "cli",
                "/env/python3.8",
                ["--remote-python", "/cli/python3.9"],
                ("/cli/python3.9",),
            ),
        )
        for command in ("list", "status", "watch"):
            for source, environment_value, extra, expected in cases:
                with self.subTest(command=command, source=source):
                    calls = []

                    def aggregate(operation, **kwargs):
                        calls.append((operation, kwargs))
                        return RemoteAggregate(
                            operation,
                            hosts=("edge",),
                            succeeded=("edge",),
                        )

                    def watch_provider(**kwargs):
                        calls.append(("watch", kwargs))
                        return FakeRemoteWatchSession(
                            (RemoteWatchReady("edge"),),
                            hosts=("edge",),
                        )

                    argv = [command, "--remote", "--json"]
                    if command == "watch":
                        argv.insert(1, "--all")
                        argv.append("--from-start")
                    argv.extend(extra)
                    with mock.patch.dict(os.environ, {}, clear=False):
                        os.environ.pop(cli_module.REMOTE_PYTHON_ENV, None)
                        if environment_value is not None:
                            os.environ[cli_module.REMOTE_PYTHON_ENV] = (
                                environment_value
                            )
                        code = main(
                            argv,
                            scanner=FakeScanner(),
                            client=OfflineClient(),
                            stdout=io.StringIO(),
                            stderr=io.StringIO(),
                            remote_aggregator=aggregate,
                            remote_watch_provider=watch_provider,
                        )

                    self.assertEqual(0, code)
                    self.assertEqual(1, len(calls))
                    if expected is None:
                        self.assertNotIn("python_candidates", calls[0][1])
                    else:
                        self.assertEqual(
                            expected,
                            calls[0][1]["python_candidates"],
                        )

    def test_explicit_remote_python_rejects_legacy_provider_signatures(self):
        snapshot_calls = []

        def legacy_snapshot(command, selected=None):
            snapshot_calls.append((command, selected))
            return RemoteAggregate(command)

        with self.assertRaisesRegex(
            TypeError,
            "remote aggregator does not accept python_candidates",
        ):
            main(
                [
                    "status",
                    "--remote",
                    "--remote-python",
                    "/opt/python3.8",
                    "--json",
                ],
                scanner=FakeScanner(),
                client=OfflineClient(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                remote_aggregator=legacy_snapshot,
            )
        self.assertEqual([], snapshot_calls)

        args = build_parser().parse_args(
            [
                "watch",
                "--all",
                "--remote",
                "--remote-python",
                "/opt/python3.8",
            ]
        )
        args.remote_python_candidates = ("/opt/python3.8",)
        watch_calls = []

        def legacy_watch(selected=None, from_start=False):
            watch_calls.append((selected, from_start))
            return FakeRemoteWatchSession()

        with self.assertRaisesRegex(
            TypeError,
            "remote watch provider does not accept python_candidates",
        ):
            cli_module._open_remote_watch(
                legacy_watch,
                args,
                threading.Event(),
            )
        self.assertEqual([], watch_calls)

    def test_internal_recency_parser_is_bounded_and_mutually_exclusive(self):
        invalid_values = (
            "0",
            "-1",
            "nan",
            "inf",
            str(365 * 24 * 60 * 60 + 1),
        )
        for value in invalid_values:
            with self.subTest(value=value):
                parser_stderr = io.StringIO()
                with contextlib.redirect_stderr(parser_stderr):
                    with self.assertRaises(SystemExit) as raised:
                        main(["list", "--recent-seconds", value, "--json"])
                self.assertEqual(2, raised.exception.code)
                self.assertIn("recent seconds", parser_stderr.getvalue())

        parser_stderr = io.StringIO()
        with contextlib.redirect_stderr(parser_stderr):
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "list",
                        "--all",
                        "--recent-seconds",
                        "172800",
                    ]
                )
        self.assertEqual(2, raised.exception.code)
        self.assertIn("not allowed with argument", parser_stderr.getvalue())

    def test_internal_recency_path_filters_direct_list_without_remote(self):
        scanner = FakeScanner()

        code, stdout, stderr = self.run_cli(
            ["list", "--recent-seconds", "123.5", "--json"],
            scanner,
        )

        self.assertEqual(0, code)
        self.assertEqual([], json.loads(stdout))
        self.assertEqual([123.5], scanner.recent_calls)
        self.assertEqual("", stderr)

    def test_remote_list_default_and_all_forward_distinct_recency(self):
        calls = []

        def aggregate(command, selected=None, recent_seconds=None):
            calls.append((command, selected, recent_seconds))
            return RemoteAggregate(
                command,
                hosts=("edge",),
                succeeded=("edge",),
            )

        for extra in ([], ["--all"]):
            code = main(
                ["list", "--remote", "--json"] + extra,
                scanner=FakeScanner(),
                client=OfflineClient(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                remote_aggregator=aggregate,
            )
            self.assertEqual(0, code)

        self.assertEqual(
            [
                ("list", None, RECENT_SECONDS),
                ("list", None, None),
            ],
            calls,
        )

    def test_legacy_remote_aggregator_shape_is_preserved_without_typeerror_retry(self):
        legacy_calls = []

        def legacy(command, selected=None):
            legacy_calls.append((command, selected))
            return RemoteAggregate(
                command,
                hosts=("edge",),
                succeeded=("edge",),
            )

        code = main(
            ["list", "--remote", "--json"],
            scanner=FakeScanner(),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            remote_aggregator=legacy,
        )

        self.assertEqual(0, code)
        self.assertEqual([("list", None)], legacy_calls)

        def broken(command, selected=None, recent_seconds=None):
            del command, selected, recent_seconds
            raise TypeError("programmer error")

        with self.assertRaisesRegex(TypeError, "programmer error"):
            main(
                ["list", "--remote", "--json"],
                scanner=FakeScanner(),
                client=OfflineClient(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                remote_aggregator=broken,
            )

    def test_default_local_json_never_calls_remote_or_adds_host(self):
        session = make_session("strictly-local", updated_at=time.time())
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
        self.assertEqual([RECENT_SECONDS], scanner.recent_calls)
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
            "remote old-edge: python_too_old\n"
            "remote: no Python >= 3.8 found among bounded candidates on "
            "1 host(s); use --remote-python <absolute-path> or "
            "AGENT_SIDECAR_REMOTE_PYTHON to pin an interpreter\n",
            stderr.getvalue(),
        )

    def test_explicit_remote_python_uses_canonical_aggregate_hint(self):
        result = RemoteAggregate(
            "status",
            failures=(
                RemoteFailure("old-a", "python_too_old"),
                RemoteFailure("old-b", "python_too_old"),
            ),
            hosts=("old-a", "old-b"),
        )
        calls = []
        stderr = io.StringIO()

        def aggregate(command, selected=None, python_candidates=None):
            calls.append((command, selected, python_candidates))
            return result

        code = main(
            [
                "status",
                "--remote",
                "--remote-python",
                "/opt/python3.8",
                "--json",
            ],
            scanner=FakeScanner(),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=stderr,
            remote_aggregator=aggregate,
        )

        self.assertEqual(3, code)
        self.assertEqual(
            [("status", None, ("/opt/python3.8",))],
            calls,
        )
        self.assertEqual(
            "remote old-a: python_too_old\n"
            "remote old-b: python_too_old\n"
            "remote: the interpreter set via "
            "--remote-python/AGENT_SIDECAR_REMOTE_PYTHON is missing or "
            "older than 3.8 on 2 host(s)\n",
            stderr.getvalue(),
        )

    def test_empty_remote_fleet_is_nonfailure_with_local_rows_and_notice(self):
        local = make_session(
            "local-working",
            Status.WORKING,
            updated_at=time.time(),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["status", "--remote", "--json"],
            scanner=FakeScanner([local]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
            remote_aggregator=lambda command, selected=None: RemoteAggregate(
                command
            ),
        )

        self.assertEqual(0, code)
        self.assertEqual(
            [dict(local.to_dict(), host="local")],
            json.loads(stdout.getvalue()),
        )
        self.assertEqual(
            "remote: no eligible hosts; showing local sessions only\n",
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
        self.assertEqual([RECENT_SECONDS], scanner.recent_calls)
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

    def test_local_and_remote_snapshots_share_selection_and_ordering(self):
        now = time.time()
        sessions = [
            make_session(
                "root-a",
                Status.WAITING,
                agent="claude",
                updated_at=now - 30,
            ),
            make_session(
                "child-a",
                Status.WAITING,
                agent="claude",
                updated_at=now + 10,
                parent_id="root-a",
            ),
            make_session(
                "work-b",
                Status.WORKING,
                agent="cursor-cli",
                updated_at=now + 20,
            ),
            make_session(
                "idle-c",
                Status.IDLE,
                agent="codex",
                updated_at=now + 40,
            ),
            make_session(
                "old-d",
                Status.WAITING,
                agent="claude",
                updated_at=now - RECENT_SECONDS - 10,
            ),
        ]
        local_rows = [session.to_dict() for session in sessions]
        remote_rows = tuple(
            dict(session.to_dict(), host="edge")
            for session in reversed(sessions)
        )
        all_ids = {session.session_id for session in sessions}
        cases = (
            (
                "default",
                ["list"],
                ["idle-c", "work-b", "child-a", "root-a"],
                ["idle-c", "work-b", "root-a", "child-a"],
            ),
            (
                "all",
                ["list", "--all"],
                ["idle-c", "work-b", "child-a", "root-a", "old-d"],
                ["idle-c", "work-b", "root-a", "child-a", "old-d"],
            ),
            (
                "agent",
                ["list", "--all", "--agent", "cLaUdE"],
                ["child-a", "root-a", "old-d"],
                ["root-a", "child-a", "old-d"],
            ),
            (
                "status",
                ["status"],
                ["work-b", "child-a", "root-a", "old-d"],
                ["work-b", "root-a", "child-a", "old-d"],
            ),
        )

        def human_signature(rendered):
            signature = []
            for line in rendered.splitlines()[1:]:
                matches = [session_id for session_id in all_ids if session_id in line]
                if matches:
                    self.assertEqual(1, len(matches))
                    signature.append((matches[0], "↳" in line))
            return signature

        for name, base_argv, json_order, human_order in cases:
            for as_json in (False, True):
                with self.subTest(name=name, as_json=as_json):
                    argv = base_argv + (["--json"] if as_json else [])
                    local_stdout = io.StringIO()
                    local_stderr = io.StringIO()
                    local_code = main(
                        argv,
                        scanner=FakeScanner(),
                        client=FakeClient(local_rows),
                        stdout=local_stdout,
                        stderr=local_stderr,
                    )
                    remote_stdout = io.StringIO()
                    remote_stderr = io.StringIO()
                    remote_code = main(
                        argv + ["--remote"],
                        scanner=FakeScanner(),
                        client=FakeClient(),
                        stdout=remote_stdout,
                        stderr=remote_stderr,
                        remote_aggregator=lambda command, selected=None, recent_seconds=None: RemoteAggregate(
                            command,
                            rows=remote_rows,
                            hosts=("edge",),
                            succeeded=("edge",),
                        ),
                    )

                    self.assertEqual(0, local_code)
                    self.assertEqual(local_code, remote_code)
                    self.assertEqual("", local_stderr.getvalue())
                    self.assertEqual("", remote_stderr.getvalue())
                    if as_json:
                        local_payload = json.loads(local_stdout.getvalue())
                        remote_payload = json.loads(remote_stdout.getvalue())
                        self.assertEqual(
                            local_payload,
                            [
                                {
                                    key: value
                                    for key, value in row.items()
                                    if key != "host"
                                }
                                for row in remote_payload
                            ],
                        )
                        self.assertEqual(
                            json_order,
                            [row["session_id"] for row in local_payload],
                        )
                    else:
                        local_signature = human_signature(local_stdout.getvalue())
                        remote_signature = human_signature(remote_stdout.getvalue())
                        self.assertEqual(local_signature, remote_signature)
                        self.assertEqual(
                            human_order,
                            [session_id for session_id, _depth in local_signature],
                        )
                        self.assertTrue(local_stdout.getvalue().startswith("AGENT"))
                        self.assertTrue(remote_stdout.getvalue().startswith("HOST"))

    def test_empty_status_uses_same_local_and_remote_rendering(self):
        idle = make_session(
            "idle-only",
            Status.IDLE,
            updated_at=time.time(),
        )
        remote_idle = dict(idle.to_dict(), host="edge")

        for as_json in (False, True):
            with self.subTest(as_json=as_json):
                argv = ["status"] + (["--json"] if as_json else [])
                outputs = []
                for remote in (False, True):
                    stdout = io.StringIO()
                    code = main(
                        argv + (["--remote"] if remote else []),
                        scanner=FakeScanner(),
                        client=FakeClient([] if remote else [idle.to_dict()]),
                        stdout=stdout,
                        stderr=io.StringIO(),
                        remote_aggregator=(
                            (
                                lambda command, selected=None: RemoteAggregate(
                                    command,
                                    rows=(remote_idle,),
                                    hosts=("edge",),
                                    succeeded=("edge",),
                                )
                            )
                            if remote
                            else None
                        ),
                    )
                    self.assertEqual(0, code)
                    outputs.append(stdout.getvalue())

                self.assertEqual(
                    "[]\n" if as_json else "no active sessions\n",
                    outputs[0],
                )
                self.assertEqual(outputs[0], outputs[1])

    def test_list_json_uses_session_schema_and_48_hour_default(self):
        session = make_session("one", updated_at=time.time())
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
        session = make_session("one", updated_at=time.time())

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
        now = time.time()
        claude = make_session("claude", agent="claude", updated_at=now)
        cursor_ide = make_session("ide", agent="cursor-ide", updated_at=now)
        cursor_cli = make_session("cli", agent="cursor-cli", updated_at=now)
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
        session.updated_at = time.time()
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
        session = make_session("unsafe-json", updated_at=time.time())
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
                    [command] + (["--all"] if command == "list" else []),
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
            ["list", "--all"],
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

        code, stdout, stderr = self.run_cli(
            ["list", "--all"],
            FakeScanner(sessions),
        )

        self.assertEqual(0, code)
        self.assertEqual("", stderr)
        self.assertEqual(1201, len(stdout.splitlines()))
        branch_offsets = [
            line.index("↳")
            for line in stdout.splitlines()
            if "↳" in line
        ]
        self.assertLessEqual(max(branch_offsets), 12)

    def test_list_json_sorts_flat_rows_without_tree_reparenting(self):
        child = make_session("child", parent_id="parent", updated_at=300.0)
        parent = make_session("parent", updated_at=100.0)

        code, stdout, stderr = self.run_cli(
            ["list", "--all", "--json"],
            FakeScanner([parent, child]),
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

    def test_daemon_tail_errors_are_sanitized_deduplicated_and_json_safe(self):
        session = make_session("daemon", updated_at=time.time())
        tail_error = {
            "agent": "cursor-\x1b]0;pwned\x07cli",
            "session_id": "one",
            "code": "CursorChatSourceError",
            "message": "/private/transcript content",
        }
        cases = (
            (["list", "--json"], None),
            (["status", "--json"], None),
            (
                ["list", "--remote", "--json"],
                lambda command, selected=None, recent_seconds=None: RemoteAggregate(
                    command,
                    hosts=("edge",),
                    succeeded=("edge",),
                ),
            ),
        )

        for argv, aggregator in cases:
            with self.subTest(argv=argv):
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = main(
                    argv,
                    scanner=FakeScanner([make_session("direct")]),
                    client=FakeClient(
                        [session.to_dict()],
                        tail_errors=[tail_error, dict(tail_error)],
                    ),
                    stdout=stdout,
                    stderr=stderr,
                    remote_aggregator=aggregator,
                )

                self.assertEqual(0, code)
                self.assertIsInstance(json.loads(stdout.getvalue()), list)
                self.assertEqual(
                    "tail error: cursor-cli session=one "
                    "code=CursorChatSourceError\n",
                    stderr.getvalue(),
                )
                self.assertNotIn("private", stderr.getvalue())
                self.assertNotIn("pwned", stderr.getvalue())
                self.assertNotIn("tail error", stdout.getvalue())

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

    def test_watch_all_warns_and_falls_back_after_live_subscription_drops(self):
        daemon_event = Event(
            "2026-08-23T04:00:00+08:00",
            "claude",
            "daemon-session",
            "assistant",
            "before drop",
        )
        direct_session = make_session("direct")
        direct_event = Event(
            "2026-08-23T04:00:01+08:00",
            "claude",
            "direct",
            "assistant",
            "after fallback",
        )
        tail_error = {
            "agent": "cursor-cli",
            "session_id": "broken-tail",
            "code": "CursorChatSourceError",
        }

        class DroppingClient(FakeClient):
            def subscribe(self):
                self.subscribe_calls += 1

                def events():
                    yield daemon_event.to_dict()
                    raise SidecarClientError(
                        "private daemon transport detail",
                        code="connection_closed",
                    )

                return events()

        direct_calls = []

        def direct_watcher(sessions, from_start=False):
            direct_calls.append((sessions, from_start))
            return iter((direct_event,))

        client = DroppingClient(tail_errors=[tail_error, dict(tail_error)])
        scanner = FakeScanner([direct_session])
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["watch", "--all", "--json"],
            scanner=scanner,
            client=client,
            stdout=stdout,
            stderr=stderr,
            watch_provider=direct_watcher,
        )

        self.assertEqual(0, code)
        self.assertEqual(
            [daemon_event.to_dict(), direct_event.to_dict()],
            [json.loads(line) for line in stdout.getvalue().splitlines()],
        )
        self.assertEqual(1, client.status_calls)
        self.assertEqual(1, client.subscribe_calls)
        self.assertEqual([None], scanner.recent_calls)
        self.assertEqual([([direct_session], False)], direct_calls)
        self.assertEqual(
            "tail error: cursor-cli session=broken-tail "
            "code=CursorChatSourceError\n"
            "watch: daemon subscription lost; switching to direct local "
            "tailing; events during transition may be missed\n",
            stderr.getvalue(),
        )
        self.assertNotIn("private", stderr.getvalue())
        self.assertNotIn("tail error", stdout.getvalue())

    def test_watch_all_daemon_unavailable_before_subscription_falls_back_silently(self):
        session = make_session("direct")
        event = Event(
            "2026-08-23T04:00:00+08:00",
            "claude",
            "direct",
            "assistant",
            "normal fallback",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []

        def direct_watcher(sessions, from_start=False):
            calls.append((sessions, from_start))
            return iter((event,))

        code = main(
            ["watch", "--all", "--json"],
            scanner=FakeScanner([session]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
            watch_provider=direct_watcher,
        )

        self.assertEqual(0, code)
        self.assertEqual(event.to_dict(), json.loads(stdout.getvalue()))
        self.assertEqual([([session], False)], calls)
        self.assertEqual("", stderr.getvalue())

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

    def test_stream_ready_is_hidden_and_rejects_non_internal_combinations(self):
        parser = build_parser()
        watch_parser = next(
            action.choices["watch"]
            for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )
        parsed = parser.parse_args(
            ["watch", "--all", "--json", "--stream-ready"]
        )

        self.assertTrue(parsed.stream_ready)
        self.assertNotIn("stream-ready", watch_parser.format_help())
        invalid = (
            ["watch", "--all", "--stream-ready"],
            ["watch", "--json", "--stream-ready"],
            ["watch", "prefix", "--all", "--json", "--stream-ready"],
            [
                "watch",
                "--all",
                "--json",
                "--remote",
                "--stream-ready",
            ],
        )
        for argv in invalid:
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                code = main(
                    argv,
                    scanner=FakeScanner(),
                    client=OfflineClient(),
                    stdout=io.StringIO(),
                    stderr=stderr,
                )
                self.assertEqual(2, code)
                self.assertEqual(
                    "watch: --stream-ready requires local --all --json\n",
                    stderr.getvalue(),
                )

    def test_stream_ready_waits_for_scan_and_precedes_direct_events(self):
        event = Event("t", "claude", "direct", "assistant", "event")

        class SlowScanner(FakeScanner):
            def __init__(self):
                super().__init__([make_session("direct")])
                self.complete = False

            def scan(self, recent_seconds=None):
                time.sleep(0.01)
                rows = super().scan(recent_seconds=recent_seconds)
                self.complete = True
                return rows

        scanner = SlowScanner()
        callbacks = []

        def provider(sessions, from_start=False, on_ready=None):
            self.assertEqual(["direct"], [row.session_id for row in sessions])
            self.assertFalse(from_start)

            def events():
                self.assertTrue(scanner.complete)
                callbacks.append(on_ready)
                on_ready()
                yield event

            return events()

        stdout = io.StringIO()
        code = main(
            ["watch", "--all", "--json", "--stream-ready"],
            scanner=scanner,
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=provider,
        )
        lines = stdout.getvalue().splitlines()

        self.assertEqual(0, code)
        self.assertEqual('{"type":"ready"}', lines[0])
        self.assertEqual(event.to_dict(), json.loads(lines[1]))
        self.assertEqual(1, len(callbacks))

    def test_stream_ready_empty_or_unsupported_sources_emit_nothing(self):
        cases = (
            FakeScanner(),
            FakeScanner(
                [
                    make_session(
                        "unsupported",
                        agent="copilot",
                        transcript="",
                    )
                ]
            ),
        )
        for scanner in cases:
            with self.subTest(sessions=len(scanner.sessions)):
                stdout = io.StringIO()
                code = main(
                    [
                        "watch",
                        "--all",
                        "--from-start",
                        "--json",
                        "--stream-ready",
                    ],
                    scanner=scanner,
                    client=OfflineClient(),
                    stdout=stdout,
                    stderr=io.StringIO(),
                )
                self.assertEqual(1, code)
                self.assertEqual("", stdout.getvalue())

    def test_stream_ready_tailer_initialization_failure_emits_nothing(self):
        stdout = io.StringIO()
        with mock.patch(
            "sidecar.tail.SessionTailer",
            side_effect=RuntimeError("private initialization detail"),
        ):
            code = main(
                [
                    "watch",
                    "--all",
                    "--from-start",
                    "--json",
                    "--stream-ready",
                ],
                scanner=FakeScanner([make_session("local")]),
                client=OfflineClient(),
                stdout=stdout,
                stderr=io.StringIO(),
            )

        self.assertEqual(1, code)
        self.assertEqual("", stdout.getvalue())

    def test_stream_ready_daemon_fallback_emits_exactly_once(self):
        daemon_event = Event(
            "t1",
            "claude",
            "daemon",
            "assistant",
            "before drop",
        )
        direct_event = Event(
            "t2",
            "claude",
            "direct",
            "assistant",
            "after drop",
        )

        class AckThenDropClient(FakeClient):
            def subscribe(self, on_ready=None):
                self.subscribe_calls += 1

                def events():
                    on_ready()
                    yield daemon_event.to_dict()
                    raise SidecarClientError(
                        "private",
                        code="connection_closed",
                    )

                return events()

        direct_ready = []

        def direct_provider(sessions, from_start=False, on_ready=None):
            del sessions, from_start

            def events():
                direct_ready.append(on_ready)
                on_ready()
                yield direct_event

            return events()

        stdout = io.StringIO()
        code = main(
            ["watch", "--all", "--json", "--stream-ready"],
            scanner=FakeScanner([make_session("direct")]),
            client=AckThenDropClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=direct_provider,
        )
        lines = stdout.getvalue().splitlines()

        self.assertEqual(0, code)
        self.assertEqual(1, lines.count('{"type":"ready"}'))
        self.assertEqual(
            ["daemon", "direct"],
            [json.loads(line)["session_id"] for line in lines[1:]],
        )
        self.assertEqual(1, len(direct_ready))

    def test_stream_ready_pre_ack_failure_defers_to_direct_source(self):
        direct_event = Event(
            "t",
            "claude",
            "direct",
            "assistant",
            "after failed ack",
        )

        class PreAckFailureClient(FakeClient):
            def subscribe(self, on_ready=None):
                del on_ready
                self.subscribe_calls += 1
                raise SidecarClientError("private", code="invalid_response")

        def direct_provider(sessions, from_start=False, on_ready=None):
            del sessions, from_start

            def events():
                on_ready()
                yield direct_event

            return events()

        stdout = io.StringIO()
        code = main(
            ["watch", "--all", "--json", "--stream-ready"],
            scanner=FakeScanner([make_session("direct")]),
            client=PreAckFailureClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=direct_provider,
        )
        lines = stdout.getvalue().splitlines()

        self.assertEqual(0, code)
        self.assertEqual('{"type":"ready"}', lines[0])
        self.assertEqual("direct", json.loads(lines[1])["session_id"])
        self.assertEqual(1, lines.count('{"type":"ready"}'))

    def test_remote_watch_validation_stops_before_setup(self):
        cases = (
            (
                ["watch", "--all", "--host", "edge"],
                "watch: --host requires --remote\n",
            ),
            (
                ["watch", "--remote"],
                "watch: --remote requires --all\n",
            ),
            (
                ["watch", "prefix", "--remote"],
                "watch: --remote conflicts with a session prefix; use --all\n",
            ),
            (
                ["watch", "prefix", "--all", "--remote"],
                "watch: --remote conflicts with a session prefix; use --all\n",
            ),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                calls = []
                stderr = io.StringIO()
                code = main(
                    argv,
                    scanner=FakeScanner(),
                    client=OfflineClient(),
                    stdout=io.StringIO(),
                    stderr=stderr,
                    remote_watch_provider=lambda **kwargs: calls.append(kwargs),
                )
                self.assertEqual(2, code)
                self.assertEqual(expected, stderr.getvalue())
                self.assertEqual([], calls)

    def test_remote_watch_merges_local_and_two_hosts_fairly_from_start(self):
        local_session = make_session("local-direct")
        local_events = [
            Event("t1", "claude", "local-1", "assistant", "one"),
            Event("t3", "claude", "local-2", "assistant", "two"),
        ]
        remote_events = [
            RemoteWatchEvent(
                "edge-a",
                "t2",
                "codex",
                "remote-a",
                "assistant",
                "a",
                {},
            ),
            RemoteWatchEvent(
                "edge-b",
                "t4",
                "cursor-cli",
                "remote-b",
                "assistant",
                "b",
                {},
            ),
        ]
        barrier = threading.Barrier(2)
        direct_calls = []
        remote_calls = []

        def direct(sessions, from_start=False, cancel_event=None):
            direct_calls.append((sessions, from_start, cancel_event))

            def events():
                barrier.wait(timeout=1)
                yield from local_events

            return events()

        class GatedRemote(FakeRemoteWatchSession):
            def __iter__(self):
                barrier.wait(timeout=1)
                return iter(self.items)

        remote_session = GatedRemote(
            remote_events,
            hosts=("edge-a", "edge-b"),
        )

        def remote_provider(selected=None, from_start=False, cancel_event=None):
            remote_calls.append((selected, from_start, cancel_event))
            return remote_session

        stdout = io.StringIO()
        code = main(
            [
                "watch",
                "--all",
                "--remote",
                "--host",
                "edge-a",
                "--host",
                "edge-b",
                "--from-start",
                "--json",
            ],
            scanner=FakeScanner([local_session]),
            client=FakeClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=direct,
            remote_watch_provider=remote_provider,
            local_watch_queue=queue.Queue(maxsize=1),
            remote_watch_queue=queue.Queue(maxsize=1),
        )
        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]

        self.assertEqual(0, code)
        self.assertEqual(
            {"local-1", "local-2", "remote-a", "remote-b"},
            {row["session_id"] for row in rows},
        )
        self.assertEqual(
            {"local", "edge-a", "edge-b"},
            {row["host"] for row in rows},
        )
        self.assertLess(
            abs(
                rows.index(next(row for row in rows if row["host"] == "local"))
                - rows.index(next(row for row in rows if row["host"] != "local"))
            ),
            2,
        )
        self.assertEqual([([local_session], True, direct_calls[0][2])], direct_calls)
        self.assertIsInstance(direct_calls[0][2], threading.Event)
        self.assertEqual(["edge-a", "edge-b"], remote_calls[0][0])
        self.assertTrue(remote_calls[0][1])
        self.assertIs(direct_calls[0][2], remote_calls[0][2])
        self.assertTrue(remote_session.closed)

    def test_remote_watch_partial_failure_keeps_local_and_peer_events(self):
        local = Event("t1", "claude", "local", "assistant", "local event")
        remote = RemoteWatchEvent(
            "good",
            "t2",
            "codex",
            "remote",
            "assistant",
            "remote event",
            {},
        )
        remote_session = FakeRemoteWatchSession(
            (
                RemoteWatchReady("good"),
                RemoteWatchFailure("old", "python_too_old"),
                remote,
            ),
            hosts=("good", "old"),
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["watch", "--all", "--remote", "--json"],
            scanner=FakeScanner(),
            client=FakeClient(events=[local.to_dict()]),
            stdout=stdout,
            stderr=stderr,
            remote_watch_provider=lambda **kwargs: remote_session,
        )

        self.assertEqual(0, code)
        self.assertEqual(
            {"local", "remote"},
            {
                row["session_id"]
                for row in map(json.loads, stdout.getvalue().splitlines())
            },
        )
        self.assertEqual(
            "watch: remote failure host=old code=python_too_old; "
            "events may be missed\n"
            "remote: no Python >= 3.8 found among bounded candidates on "
            "1 host(s); use --remote-python <absolute-path> or "
            "AGENT_SIDECAR_REMOTE_PYTHON to pin an interpreter\n",
            stderr.getvalue(),
        )
        self.assertEqual(
            1,
            stderr.getvalue().count(
                "no Python >= 3.8 found among bounded candidates"
            ),
        )

    def test_watch_remote_python_hint_is_emitted_once_for_multiple_failures(self):
        remote_session = FakeRemoteWatchSession(
            (
                RemoteWatchFailure("old-a", "python_too_old"),
                RemoteWatchFailure("old-b", "python_too_old"),
                RemoteWatchFailure("offline", "unreachable"),
            ),
            hosts=("old-a", "old-b", "offline"),
            all_failed=True,
        )
        stderr = io.StringIO()

        code = main(
            ["watch", "--all", "--remote", "--from-start", "--json"],
            scanner=FakeScanner(),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=stderr,
            remote_watch_provider=lambda **kwargs: remote_session,
        )

        self.assertEqual(3, code)
        self.assertEqual(
            "watch: no sessions found\n"
            "watch: remote failure host=old-a code=python_too_old; "
            "events may be missed\n"
            "watch: remote failure host=old-b code=python_too_old; "
            "events may be missed\n"
            "watch: remote failure host=offline code=unreachable; "
            "events may be missed\n"
            "remote: no Python >= 3.8 found among bounded candidates on "
            "2 host(s); use --remote-python <absolute-path> or "
            "AGENT_SIDECAR_REMOTE_PYTHON to pin an interpreter\n",
            stderr.getvalue(),
        )
        self.assertEqual(
            1,
            stderr.getvalue().count(
                "no Python >= 3.8 found among bounded candidates"
            ),
        )

    def test_watch_teardown_hint_failure_preserves_selected_outcome(self):
        class FailingHintStderr(io.StringIO):
            def __init__(self, error):
                super().__init__()
                self.error = error
                self.hint_attempts = 0

            def write(self, value):
                if value.startswith("remote: no Python >= 3.8"):
                    self.hint_attempts += 1
                    raise self.error
                return super().write(value)

        for error in (
            BrokenPipeError("closed"),
            OSError("closed"),
            ValueError("closed"),
        ):
            with self.subTest(error=type(error).__name__):
                remote_session = FakeRemoteWatchSession(
                    (
                        RemoteWatchFailure("old-a", "python_too_old"),
                        RemoteWatchFailure("old-b", "python_too_old"),
                    ),
                    hosts=("old-a", "old-b"),
                    all_failed=True,
                )
                remote_session.hosts = None
                stderr = FailingHintStderr(error)

                code = main(
                    ["watch", "--all", "--remote", "--from-start", "--json"],
                    scanner=FakeScanner(),
                    client=OfflineClient(),
                    stdout=io.StringIO(),
                    stderr=stderr,
                    remote_watch_provider=lambda **kwargs: remote_session,
                )

                self.assertEqual(3, code)
                self.assertEqual(1, stderr.hint_attempts)
                self.assertTrue(remote_session.closed)
                self.assertIn(
                    "watch: remote failure host=old-a code=python_too_old",
                    stderr.getvalue(),
                )
                self.assertFalse(
                    any(
                        thread.name.startswith("sidecar-watch-")
                        for thread in threading.enumerate()
                    )
                )

    def test_watch_reports_python_hint_after_startup_while_local_source_is_active(self):
        hint_written = threading.Event()
        hint_seen_while_active = threading.Event()
        report_calls = []
        local_event = Event(
            "t",
            "claude",
            "local",
            "assistant",
            "local remains active through remote startup",
        )
        remote_session = FakeRemoteWatchSession(
            (
                RemoteWatchFailure("old", "python_too_old"),
                RemoteWatchReady("good"),
            ),
            hosts=("old", "good"),
        )
        stderr = io.StringIO()
        original_report = cli_module._report_remote_python_hint

        def report_hint(*args, **kwargs):
            original_report(*args, **kwargs)
            report_calls.append((args, kwargs))
            hint_written.set()

        def direct(sessions, from_start=False, cancel_event=None):
            del sessions, from_start, cancel_event

            def events():
                if hint_written.wait(2):
                    hint_seen_while_active.set()
                yield local_event

            return events()

        with mock.patch.object(
            cli_module,
            "_report_remote_python_hint",
            new=report_hint,
        ):
            code = main(
                ["watch", "--all", "--remote", "--from-start", "--json"],
                scanner=FakeScanner([make_session("local")]),
                client=OfflineClient(),
                stdout=io.StringIO(),
                stderr=stderr,
                watch_provider=direct,
                remote_watch_provider=lambda **kwargs: remote_session,
            )

        self.assertEqual(0, code)
        self.assertTrue(hint_seen_while_active.is_set())
        self.assertEqual(1, len(report_calls))
        self.assertEqual(
            "watch: remote failure host=old code=python_too_old; "
            "events may be missed\n"
            "remote: no Python >= 3.8 found among bounded candidates on "
            "1 host(s); use --remote-python <absolute-path> or "
            "AGENT_SIDECAR_REMOTE_PYTHON to pin an interpreter\n",
            stderr.getvalue(),
        )

    def test_remote_watch_daemon_drop_warns_and_cancels_direct_fallback(self):
        direct_session = make_session("direct")
        direct_event = Event(
            "t",
            "claude",
            "direct",
            "assistant",
            "after drop",
        )

        class DroppingClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.cancel_event = None

            def subscribe(self, cancel_event=None):
                self.subscribe_calls += 1
                self.cancel_event = cancel_event
                raise SidecarClientError("private", code="connection_closed")

        client = DroppingClient()
        direct_calls = []

        def direct(sessions, from_start=False, cancel_event=None):
            direct_calls.append((sessions, from_start, cancel_event))
            return iter((direct_event,))

        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            ["watch", "--all", "--remote", "--json"],
            scanner=FakeScanner([direct_session]),
            client=client,
            stdout=stdout,
            stderr=stderr,
            watch_provider=direct,
            remote_watch_provider=lambda **kwargs: FakeRemoteWatchSession(
                hosts=()
            ),
        )

        self.assertEqual(0, code)
        self.assertEqual("direct", json.loads(stdout.getvalue())["session_id"])
        self.assertIn("daemon subscription lost", stderr.getvalue())
        self.assertNotIn("private", stderr.getvalue())
        self.assertIsInstance(client.cancel_event, threading.Event)
        self.assertEqual([direct_session], direct_calls[0][0])
        self.assertFalse(direct_calls[0][1])
        self.assertIs(client.cancel_event, direct_calls[0][2])

    def test_remote_watch_all_failure_without_local_exits_three(self):
        remote_session = FakeRemoteWatchSession(
            (RemoteWatchFailure("old", "python_too_old"),),
            hosts=("old",),
            all_failed=True,
        )
        stderr = io.StringIO()

        code = main(
            ["watch", "--all", "--remote", "--from-start", "--json"],
            scanner=FakeScanner(),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=stderr,
            remote_watch_provider=lambda **kwargs: remote_session,
        )

        self.assertEqual(3, code)
        self.assertIn("watch: no sessions found\n", stderr.getvalue())
        self.assertIn("host=old code=python_too_old", stderr.getvalue())
        self.assertNotIn("traceback", stderr.getvalue().casefold())

    def test_remote_watch_producer_crash_overrides_stale_session_outcome(self):
        class CrashingRemote(FakeRemoteWatchSession):
            def __iter__(self):
                raise RuntimeError("private remote iterator detail")

        remote_session = CrashingRemote(hosts=("edge",), all_failed=False)
        stderr = io.StringIO()

        code = main(
            ["watch", "--all", "--remote", "--from-start", "--json"],
            scanner=FakeScanner(),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=stderr,
            remote_watch_provider=lambda **kwargs: remote_session,
        )

        self.assertEqual(3, code)
        self.assertIn(
            "watch: remote failure code=remote; events may be missed\n",
            stderr.getvalue(),
        )
        self.assertNotIn("private", stderr.getvalue())
        self.assertNotIn("traceback", stderr.getvalue().casefold())
        self.assertTrue(remote_session.closed)

    def test_sole_remote_event_then_failure_is_terminal_failure(self):
        event = RemoteWatchEvent(
            "edge",
            "t",
            "codex",
            "remote-event",
            "assistant",
            "delivered before failure",
            {},
        )
        remote_session = FakeRemoteWatchSession(
            (
                RemoteWatchReady("edge"),
                event,
                RemoteWatchFailure("edge", "remote"),
            ),
            hosts=("edge",),
            all_failed=False,
        )
        stdout = io.StringIO()

        code = main(
            ["watch", "--all", "--remote", "--from-start", "--json"],
            scanner=FakeScanner(),
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            remote_watch_provider=lambda **kwargs: remote_session,
        )

        self.assertEqual(3, code)
        self.assertEqual("remote-event", json.loads(stdout.getvalue())["session_id"])
        self.assertTrue(remote_session.closed)

    def test_local_event_then_crash_with_all_remote_failure_exits_three(self):
        local_event = Event(
            "t",
            "claude",
            "local-event",
            "assistant",
            "delivered before crash",
        )

        def local_provider(*args, **kwargs):
            del args, kwargs

            def events():
                yield local_event
                raise RuntimeError("private local crash")

            return events()

        remote_session = FakeRemoteWatchSession(
            (RemoteWatchFailure("old", "python_too_old"),),
            hosts=("old",),
            all_failed=False,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["watch", "--all", "--remote", "--from-start", "--json"],
            scanner=FakeScanner([make_session("local")]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
            watch_provider=local_provider,
            remote_watch_provider=lambda **kwargs: remote_session,
        )

        self.assertEqual(3, code)
        self.assertEqual("local-event", json.loads(stdout.getvalue())["session_id"])
        self.assertIn("watch: local source failed\n", stderr.getvalue())
        self.assertNotIn("private", stderr.getvalue())
        self.assertTrue(remote_session.closed)

    def test_partial_remote_failure_after_event_keeps_healthy_host_success(self):
        failed_event = RemoteWatchEvent(
            "bad",
            "t1",
            "codex",
            "bad-event",
            "assistant",
            "before failure",
            {},
        )
        remote_session = FakeRemoteWatchSession(
            (
                RemoteWatchReady("bad"),
                failed_event,
                RemoteWatchFailure("bad", "remote"),
                RemoteWatchReady("good"),
            ),
            hosts=("bad", "good"),
            all_failed=True,
        )
        stdout = io.StringIO()

        code = main(
            ["watch", "--all", "--remote", "--from-start", "--json"],
            scanner=FakeScanner(),
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            remote_watch_provider=lambda **kwargs: remote_session,
        )

        self.assertEqual(0, code)
        self.assertEqual("bad-event", json.loads(stdout.getvalue())["session_id"])
        self.assertTrue(remote_session.closed)

    def test_local_only_event_then_crash_exits_one(self):
        event = Event(
            "t",
            "claude",
            "local-event",
            "assistant",
            "delivered before crash",
        )

        def provider(*args, **kwargs):
            del args, kwargs

            def events():
                yield event
                raise RuntimeError("private local crash")

            return events()

        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            ["watch", "--all", "--from-start", "--json"],
            scanner=FakeScanner([make_session("local")]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
            watch_provider=provider,
        )

        self.assertEqual(1, code)
        self.assertEqual("local-event", json.loads(stdout.getvalue())["session_id"])
        self.assertEqual("watch: local source failed\n", stderr.getvalue())
        self.assertNotIn("private", stderr.getvalue())

    def test_local_only_keyboard_interrupt_remains_130(self):
        def provider(*args, **kwargs):
            del args, kwargs
            raise KeyboardInterrupt

        stderr = io.StringIO()
        code = main(
            ["watch", "--all", "--from-start", "--json"],
            scanner=FakeScanner([make_session("local")]),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=stderr,
            watch_provider=provider,
        )

        self.assertEqual(130, code)
        self.assertEqual("", stderr.getvalue())

    def test_local_runtime_failure_does_not_count_as_existing_success(self):
        def broken_local(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("private local iterator detail")

        cases = (
            (
                "all remote failed",
                FakeRemoteWatchSession(
                    (RemoteWatchFailure("old", "python_too_old"),),
                    hosts=("old",),
                    all_failed=False,
                ),
                3,
            ),
            (
                "remote survivor",
                FakeRemoteWatchSession(
                    (
                        RemoteWatchReady("good"),
                        RemoteWatchFailure("old", "python_too_old"),
                    ),
                    hosts=("good", "old"),
                    all_failed=True,
                ),
                0,
            ),
        )
        for name, remote_session, expected in cases:
            with self.subTest(name=name):
                stderr = io.StringIO()
                code = main(
                    [
                        "watch",
                        "--all",
                        "--remote",
                        "--from-start",
                        "--json",
                    ],
                    scanner=FakeScanner([make_session("local")]),
                    client=OfflineClient(),
                    stdout=io.StringIO(),
                    stderr=stderr,
                    watch_provider=broken_local,
                    remote_watch_provider=lambda **kwargs: remote_session,
                )

                self.assertEqual(expected, code)
                self.assertIn("watch: local source failed\n", stderr.getvalue())
                self.assertNotIn("private", stderr.getvalue())
                self.assertNotIn("traceback", stderr.getvalue().casefold())
                self.assertTrue(remote_session.closed)

    def test_local_clean_end_survives_remote_failure_without_events(self):
        remote_session = FakeRemoteWatchSession(
            (RemoteWatchFailure("old", "python_too_old"),),
            hosts=("old",),
            all_failed=True,
        )

        code = main(
            ["watch", "--all", "--remote", "--from-start", "--json"],
            scanner=FakeScanner([make_session("local")]),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            watch_provider=lambda *args, **kwargs: iter(()),
            remote_watch_provider=lambda **kwargs: remote_session,
        )

        self.assertEqual(0, code)
        self.assertTrue(remote_session.closed)

    def test_local_producer_start_failure_is_sanitized_setup_error(self):
        remote_session = FakeRemoteWatchSession(hosts=("edge",))

        def fail_start(thread):
            self.assertEqual("sidecar-watch-local", thread.name)
            raise RuntimeError("private thread startup detail")

        stderr = io.StringIO()
        with mock.patch(
            "sidecar.cli.threading.Thread.start",
            new=fail_start,
        ):
            code = main(
                ["watch", "--all", "--remote", "--from-start", "--json"],
                scanner=FakeScanner([make_session("local")]),
                client=OfflineClient(),
                stdout=io.StringIO(),
                stderr=stderr,
                remote_watch_provider=lambda **kwargs: remote_session,
            )

        self.assertEqual(2, code)
        self.assertEqual(
            "watch: local producer setup failed\n",
            stderr.getvalue(),
        )
        self.assertNotIn("private", stderr.getvalue())
        self.assertTrue(remote_session.closed)
        self.assertFalse(
            any(
                thread.name.startswith("sidecar-watch-")
                for thread in threading.enumerate()
            )
        )

    def test_remote_producer_start_failure_cleans_started_local_source(self):
        real_start = threading.Thread.start
        local_cancel = []

        class BlockingRemote(FakeRemoteWatchSession):
            def __init__(self):
                super().__init__(hosts=("edge",))
                self.stop = threading.Event()

            def __iter__(self):
                self.stop.wait(2)
                return
                yield

            def close(self):
                super().close()
                self.stop.set()

        remote_session = BlockingRemote()

        def local_provider(sessions, from_start=False, cancel_event=None):
            del sessions, from_start
            local_cancel.append(cancel_event)

            def events():
                cancel_event.wait(2)
                return
                yield

            return events()

        def fail_remote_start(thread):
            if thread.name == "sidecar-watch-remote":
                real_start(thread)
                raise RuntimeError("private remote thread startup detail")
            return real_start(thread)

        stderr = io.StringIO()
        with mock.patch(
            "sidecar.cli.threading.Thread.start",
            new=fail_remote_start,
        ):
            code = main(
                ["watch", "--all", "--remote", "--from-start", "--json"],
                scanner=FakeScanner([make_session("local")]),
                client=OfflineClient(),
                stdout=io.StringIO(),
                stderr=stderr,
                watch_provider=local_provider,
                remote_watch_provider=lambda **kwargs: remote_session,
            )

        self.assertEqual(2, code)
        self.assertEqual(
            "watch: remote producer setup failed\n",
            stderr.getvalue(),
        )
        self.assertNotIn("private", stderr.getvalue())
        self.assertTrue(local_cancel[0].is_set())
        self.assertTrue(remote_session.closed)
        self.assertFalse(
            any(
                thread.name.startswith("sidecar-watch-")
                for thread in threading.enumerate()
            )
        )

    def test_remote_watch_empty_fleet_continues_local_or_exits_one(self):
        local = Event("t", "claude", "local", "assistant", "still local")
        for has_local, expected in ((True, 0), (False, 1)):
            with self.subTest(has_local=has_local):
                session = FakeRemoteWatchSession(hosts=())
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = main(
                    [
                        "watch",
                        "--all",
                        "--remote",
                        "--from-start",
                        "--json",
                    ],
                    scanner=FakeScanner(
                        [make_session("local")] if has_local else []
                    ),
                    client=OfflineClient(),
                    stdout=stdout,
                    stderr=stderr,
                    watch_provider=lambda *args, **kwargs: iter((local,)),
                    remote_watch_provider=lambda **kwargs: session,
                )

                self.assertEqual(expected, code)
                self.assertIn(
                    "remote: no eligible hosts; showing local sessions only\n",
                    stderr.getvalue(),
                )
                self.assertEqual(
                    [local.to_dict()] if has_local else [],
                    [
                        {
                            key: value
                            for key, value in json.loads(line).items()
                            if key != "host"
                        }
                        for line in stdout.getvalue().splitlines()
                    ],
                )
                self.assertTrue(session.closed)

    def test_remote_watch_setup_errors_are_typed_and_hide_raw_details(self):
        cases = (
            (RemoteInventoryError(), "watch: remote inventory\n"),
            (OSError("private setup path"), "watch: remote setup\n"),
            (
                ValueError("selected secret-alias is not eligible"),
                "watch: remote selection\n",
            ),
        )
        for error, expected in cases:
            with self.subTest(error=error.__class__.__name__):
                stderr = io.StringIO()

                def fail(**kwargs):
                    del kwargs
                    raise error

                code = main(
                    ["watch", "--all", "--remote", "--host", "secret-alias"],
                    scanner=FakeScanner([make_session("local")]),
                    client=OfflineClient(),
                    stdout=io.StringIO(),
                    stderr=stderr,
                    remote_watch_provider=fail,
                )

                self.assertEqual(2, code)
                self.assertEqual(expected, stderr.getvalue())
                self.assertNotIn("private", stderr.getvalue())
                self.assertNotIn("secret-alias", stderr.getvalue())

    def test_remote_watch_json_and_human_outputs_control_host_rendering(self):
        text = "\x1b]0;pwned\x07\x1b[31m完成\r覆写\x9b2J"
        event = RemoteWatchEvent(
            "very-long-edge-host",
            "2026-08-23T04:00:00+08:00",
            "codex",
            "remote",
            "assistant",
            text,
            {"nested": ["value"]},
        )

        outputs = {}
        for as_json in (False, True):
            stdout = io.StringIO()
            code = main(
                ["watch", "--all", "--remote", "--from-start"]
                + (["--json"] if as_json else []),
                scanner=FakeScanner(),
                client=OfflineClient(),
                stdout=stdout,
                stderr=io.StringIO(),
                remote_watch_provider=lambda **kwargs: FakeRemoteWatchSession(
                    (event,),
                    hosts=("very-long-edge-host",),
                ),
            )
            self.assertEqual(0, code)
            outputs[as_json] = stdout.getvalue()

        payload = json.loads(outputs[True])
        self.assertEqual(
            {
                "ts",
                "agent",
                "session_id",
                "kind",
                "text",
                "extra",
                "host",
            },
            set(payload),
        )
        self.assertEqual(text, payload["text"])
        human = outputs[False]
        self.assertTrue(human.startswith("very-long-edge-h "))
        self.assertIn("完成 覆写", human)
        self.assertNotIn("pwned", human)
        self.assertNotIn("\x1b", human)
        self.assertNotIn("\r", human)

    def test_remote_watch_queue_pressure_preserves_all_events(self):
        count = 80
        local_events = [
            Event("t", "claude", "local-{}".format(index), "assistant", "x")
            for index in range(count)
        ]
        remote_events = [
            RemoteWatchEvent(
                "edge",
                "t",
                "codex",
                "remote-{}".format(index),
                "assistant",
                "x",
                {},
            )
            for index in range(count)
        ]
        stdout = io.StringIO()

        code = main(
            ["watch", "--all", "--remote", "--from-start", "--json"],
            scanner=FakeScanner([make_session("local")]),
            client=OfflineClient(),
            stdout=stdout,
            stderr=io.StringIO(),
            watch_provider=lambda *args, **kwargs: iter(local_events),
            remote_watch_provider=lambda **kwargs: FakeRemoteWatchSession(
                remote_events
            ),
            local_watch_queue=queue.Queue(maxsize=1),
            remote_watch_queue=queue.Queue(maxsize=1),
        )

        self.assertEqual(0, code)
        self.assertEqual(count * 2, len(stdout.getvalue().splitlines()))
        self.assertFalse(
            any(
                thread.name.startswith("sidecar-watch-")
                for thread in threading.enumerate()
            )
        )

    def test_remote_watch_keyboard_interrupt_closes_and_joins_producers(self):
        class BlockingRemote(FakeRemoteWatchSession):
            def __init__(self):
                super().__init__(hosts=("edge",))
                self.stopped = threading.Event()

            def __iter__(self):
                while not self.closed:
                    self.stopped.wait(0.01)
                return
                yield

            def close(self):
                super().close()
                self.stopped.set()

        class InterruptQueue(queue.Queue):
            def get(self, block=True, timeout=None):
                del block, timeout
                raise KeyboardInterrupt

        remote_session = BlockingRemote()
        code = main(
            ["watch", "--all", "--remote", "--from-start", "--json"],
            scanner=FakeScanner(),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            remote_watch_provider=lambda **kwargs: remote_session,
            remote_watch_queue=InterruptQueue(maxsize=1),
        )

        self.assertEqual(130, code)
        self.assertTrue(remote_session.closed)
        self.assertFalse(
            any(
                thread.name.startswith("sidecar-watch-")
                for thread in threading.enumerate()
            )
        )

    def test_remote_watch_interrupt_survives_deferred_hint_write_failure(self):
        class FailingHintStderr(io.StringIO):
            def __init__(self):
                super().__init__()
                self.hint_attempts = 0

            def write(self, value):
                if value.startswith("remote: no Python >= 3.8"):
                    self.hint_attempts += 1
                    raise BrokenPipeError("closed")
                return super().write(value)

        class DeferredInterruptQueue(queue.Queue):
            def __init__(self, maxsize=0):
                super().__init__(maxsize=maxsize)
                self.delivered = False

            def get(self, block=True, timeout=None):
                if self.delivered:
                    raise KeyboardInterrupt
                entry = super().get(block=block, timeout=timeout)
                self.delivered = True
                return entry

        remote_session = FakeRemoteWatchSession(
            (RemoteWatchFailure("old", "python_too_old"),),
            hosts=("old",),
        )
        remote_session.hosts = None
        stderr = FailingHintStderr()

        code = main(
            ["watch", "--all", "--remote", "--from-start", "--json"],
            scanner=FakeScanner(),
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=stderr,
            remote_watch_provider=lambda **kwargs: remote_session,
            remote_watch_queue=DeferredInterruptQueue(maxsize=1),
        )

        self.assertEqual(130, code)
        self.assertEqual(1, stderr.hint_attempts)
        self.assertTrue(remote_session.closed)
        self.assertFalse(
            any(
                thread.name.startswith("sidecar-watch-")
                for thread in threading.enumerate()
            )
        )

    def test_daemon_runtime_ownership_helpers_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            pidfile = runtime / "daemon.pid"
            socket_path = runtime / "daemon.sock"
            client = FakeClient(pid=55, socket_path=socket_path)

            pidfile.write_text("55\n", encoding="ascii")
            self.assertIsNone(
                cli_module._path_identity(pidfile, cli_module.stat.S_IFSOCK)
            )

            with mock.patch("sidecar.cli.read_pid", return_value=55):
                pidfile.unlink()
                pidfile.mkdir()
                self.assertIsNone(
                    cli_module._capture_owned_daemon_paths(client, (55,))
                )
            pidfile.rmdir()

            daemon_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            daemon_socket.bind(str(socket_path))
            daemon_socket.close()
            pidfile.write_text("55\n", encoding="ascii")
            paths = cli_module._capture_owned_daemon_paths(client, (55,))
            self.assertIsNotNone(paths)

            pidfile.write_text("56\n", encoding="ascii")
            cli_module._cleanup_owned_daemon_paths(paths)
            self.assertTrue(socket_path.exists())
            pidfile.write_text("55\n", encoding="ascii")

            socket_path.unlink()
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            replacement.bind(str(socket_path))
            replacement.close()
            cli_module._cleanup_owned_daemon_paths(paths)
            self.assertTrue(socket_path.exists())

            socket_path.unlink()
            daemon_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            daemon_socket.bind(str(socket_path))
            daemon_socket.close()
            paths = cli_module._capture_owned_daemon_paths(client, (55,))
            with (
                mock.patch("sidecar.cli._socket_is_live", return_value=False),
                mock.patch.object(Path, "unlink", side_effect=OSError("busy")),
            ):
                cli_module._cleanup_owned_daemon_paths(paths)
            self.assertTrue(socket_path.exists())
            self.assertTrue(pidfile.exists())

            cli_module._cleanup_verified_pidfile(runtime, 56)
            self.assertTrue(pidfile.exists())
            for error in (PermissionError("hidden"), ValueError("invalid")):
                with self.subTest(error=type(error).__name__):
                    with mock.patch("sidecar.cli.os.kill", side_effect=error):
                        self.assertTrue(cli_module._pid_is_alive(55))

            process = FakeDaemonProcess()
            info = cli_module.PingInfo.from_response(
                {
                    "ok": True,
                    "op": "ping",
                    "pid": process.pid + 1,
                }
            )
            with mock.patch("sidecar.cli.os.getpgid", side_effect=OSError("gone")):
                self.assertFalse(cli_module._ping_belongs_to_child(process, info))
            with mock.patch(
                "sidecar.cli.os.killpg",
                side_effect=PermissionError("hidden"),
            ):
                self.assertTrue(cli_module._child_group_exists(process.pid))

    def test_daemon_process_cleanup_helpers_remain_bounded(self):
        process = FakeDaemonProcess()
        with (
            mock.patch("sidecar.cli.time.monotonic", side_effect=RuntimeError("clock")),
            mock.patch("sidecar.cli.time.time", return_value=10.0),
            mock.patch("sidecar.cli._child_group_exists", return_value=False),
        ):
            self.assertTrue(
                cli_module._wait_for_child_group(process, process.pid, 1.0)
            )

        process = FakeDaemonProcess()
        with (
            mock.patch(
                "sidecar.cli.time.monotonic",
                side_effect=(0.0, 0.0, 2.0),
            ),
            mock.patch("sidecar.cli._child_group_exists", return_value=True),
            mock.patch(
                "sidecar.cli.time.sleep",
                side_effect=KeyboardInterrupt(),
            ),
        ):
            self.assertFalse(
                cli_module._wait_for_child_group(process, process.pid, 1.0)
            )

        exited = FakeDaemonProcess()
        exited.returncode = 0
        exited.wait = mock.Mock(side_effect=OSError("already reaped"))
        cli_module._terminate_and_reap_daemon_child(exited)
        exited.wait.assert_called_once_with(timeout=0)

        unrelated = FakeDaemonProcess()
        with mock.patch(
            "sidecar.cli.os.getpgid",
            return_value=unrelated.pid + 1,
        ):
            cli_module._terminate_and_reap_daemon_child(unrelated)
        self.assertEqual([], unrelated.wait_calls)

        vanished = FakeDaemonProcess()
        vanished.wait = mock.Mock(side_effect=OSError("gone"))
        with mock.patch("sidecar.cli.os.getpgid", side_effect=ProcessLookupError):
            cli_module._terminate_and_reap_daemon_child(vanished)
        vanished.wait.assert_called_once_with(timeout=0)

        stubborn = FakeDaemonProcess()
        stubborn.wait = mock.Mock(side_effect=OSError("not a child"))
        with (
            mock.patch("sidecar.cli.os.getpgid", return_value=stubborn.pid),
            mock.patch("sidecar.cli.os.killpg", side_effect=OSError("gone")) as kill,
            mock.patch(
                "sidecar.cli._wait_for_child_group",
                side_effect=(False, True),
            ) as wait_group,
        ):
            cli_module._terminate_and_reap_daemon_child(stubborn)
        self.assertEqual(
            [
                mock.call(stubborn.pid, signal.SIGTERM),
                mock.call(stubborn.pid, signal.SIGKILL),
            ],
            kill.call_args_list,
        )
        self.assertEqual(2, wait_group.call_count)
        stubborn.wait.assert_called_once_with(timeout=0)

    def test_daemon_start_reports_runtime_and_spawn_failures(self):
        for patched, expected_code, expected_message in (
            (
                mock.patch(
                    "sidecar.cli.resolve_runtime_prefix",
                    side_effect=cli_module.RuntimeCommandError("missing"),
                ),
                2,
                "cannot resolve",
            ),
            (
                mock.patch(
                    "sidecar.cli.subprocess.Popen",
                    side_effect=OSError("spawn failed"),
                ),
                1,
                "spawn failed",
            ),
        ):
            with self.subTest(expected_message=expected_message):
                stderr = io.StringIO()
                with patched:
                    code = main(
                        ["daemon", "start"],
                        client=OfflineClient(),
                        stdout=io.StringIO(),
                        stderr=stderr,
                    )
                self.assertEqual(expected_code, code)
                self.assertIn(expected_message, stderr.getvalue())

    def test_daemon_stop_reports_each_terminal_process_state(self):
        stdout = io.StringIO()
        self.assertEqual(
            1,
            main(
                ["daemon", "stop"],
                client=OfflineClient(),
                stdout=stdout,
                stderr=io.StringIO(),
            ),
        )
        self.assertIn("not running", stdout.getvalue())

        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            pidfile = runtime / "daemon.pid"
            socket_path = runtime / "daemon.sock"

            def new_client(*responses):
                pidfile.write_text("55\n", encoding="ascii")
                return SequencedPingClient(responses, socket_path=socket_path)

            stderr = io.StringIO()
            with mock.patch(
                "sidecar.cli.os.kill",
                side_effect=PermissionError("denied"),
            ):
                code = main(
                    ["daemon", "stop"],
                    client=new_client({"ok": True, "op": "ping", "pid": 55}),
                    stdout=io.StringIO(),
                    stderr=stderr,
                )
            self.assertEqual(1, code)
            self.assertIn("cannot signal", stderr.getvalue())

            with mock.patch(
                "sidecar.cli.os.kill",
                side_effect=(ProcessLookupError(), ProcessLookupError()),
            ):
                code = main(
                    ["daemon", "stop"],
                    client=new_client(
                        {"ok": True, "op": "ping", "pid": 55},
                        SidecarClientError("offline", code="connection_failed"),
                    ),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(0, code)
            self.assertFalse(pidfile.exists())

            with (
                mock.patch("sidecar.cli.os.kill"),
                mock.patch(
                    "sidecar.cli._capture_owned_daemon_paths",
                    return_value=None,
                ),
            ):
                code = main(
                    ["daemon", "stop"],
                    client=new_client(
                        {"ok": True, "op": "ping", "pid": 55},
                        SidecarClientError("offline", code="connection_failed"),
                    ),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertEqual(0, code)
            self.assertFalse(pidfile.exists())

            stdout = io.StringIO()
            with mock.patch("sidecar.cli.os.kill"):
                code = main(
                    ["daemon", "stop"],
                    client=new_client(
                        {"ok": True, "op": "ping", "pid": 55},
                        {"ok": True, "op": "ping", "pid": 66},
                    ),
                    stdout=stdout,
                    stderr=io.StringIO(),
                )
            self.assertEqual(0, code)
            self.assertIn("another daemon", stdout.getvalue())

            stderr = io.StringIO()
            with (
                mock.patch("sidecar.cli.os.kill"),
                mock.patch("sidecar.cli.DAEMON_STOP_TIMEOUT", 0.0),
            ):
                code = main(
                    ["daemon", "stop"],
                    client=new_client({"ok": True, "op": "ping", "pid": 55}),
                    stdout=io.StringIO(),
                    stderr=stderr,
                )
            self.assertEqual(1, code)
            self.assertIn("timed out", stderr.getvalue())

    def test_daemon_run_restores_handlers_after_signals_and_errors(self):
        installed = {}
        previous_handler = object()

        def install_handler(signum, handler):
            if handler is not previous_handler:
                installed[signum] = handler

        def request_signal(**kwargs):
            del kwargs
            installed[signal.SIGTERM](signal.SIGTERM, None)

        with (
            mock.patch("sidecar.cli.signal.getsignal", return_value=previous_handler),
            mock.patch("sidecar.cli.signal.signal", side_effect=install_handler) as setter,
            mock.patch("sidecar.cli.run_foreground", side_effect=request_signal),
        ):
            code = main(
                ["daemon", "run"],
                client=OfflineClient(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(128 + signal.SIGTERM, code)
        self.assertEqual(4, setter.call_count)

        for error, expected_code, expected_message in (
            (KeyboardInterrupt(), 130, ""),
            (cli_module.DaemonError("failed"), 1, "daemon run: failed"),
        ):
            with self.subTest(error=type(error).__name__):
                stderr = io.StringIO()
                with mock.patch("sidecar.cli.run_foreground", side_effect=error):
                    code = main(
                        ["daemon", "run"],
                        client=OfflineClient(),
                        stdout=io.StringIO(),
                        stderr=stderr,
                    )
                self.assertEqual(expected_code, code)
                self.assertIn(expected_message, stderr.getvalue())

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

        popen.return_value.pid = 7654
        popen.return_value.returncode = None
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
        self.assertTrue(Path(command[0]).is_absolute())
        self.assertEqual(["daemon", "run"], command[-2:])
        self.assertNotIn("cwd", popen.call_args.kwargs)
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

    def test_daemon_start_timeout_terminates_reaps_and_cleans_owned_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            socket_path = runtime / "daemon.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.close()
            process = FakeDaemonProcess()
            (runtime / "daemon.pid").write_text(
                "{}\n".format(process.pid),
                encoding="ascii",
            )
            client = SequencedPingClient(
                [SidecarClientError("offline", code="connection_failed")],
                socket_path=socket_path,
            )
            signals, killpg = controlled_process_group(process)

            with (
                mock.patch("sidecar.cli.subprocess.Popen", return_value=process),
                mock.patch("sidecar.cli.DAEMON_START_TIMEOUT", 0.0),
                mock.patch(
                    "sidecar.cli.os.getpgid",
                    return_value=process.pid,
                ),
                mock.patch("sidecar.cli.os.killpg", side_effect=killpg),
            ):
                code = main(
                    ["daemon", "start"],
                    client=client,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )

            self.assertEqual(1, code)
            self.assertIn(signal.SIGTERM, signals)
            self.assertTrue(process.wait_calls)
            self.assertFalse(socket_path.exists())
            self.assertFalse((runtime / "daemon.pid").exists())

    def test_daemon_start_final_ping_preserves_late_ready_child(self):
        process = FakeDaemonProcess()
        client = SequencedPingClient(
            [
                SidecarClientError("offline", code="connection_failed"),
                {"ok": True, "op": "ping", "pid": process.pid},
            ]
        )
        stdout = io.StringIO()

        with (
            mock.patch("sidecar.cli.subprocess.Popen", return_value=process),
            mock.patch("sidecar.cli.DAEMON_START_TIMEOUT", 0.0),
            mock.patch("sidecar.cli.os.killpg") as killpg,
        ):
            code = main(
                ["daemon", "start"],
                client=client,
                stdout=stdout,
                stderr=io.StringIO(),
            )

        self.assertEqual(0, code)
        self.assertIn("started", stdout.getvalue())
        killpg.assert_not_called()
        self.assertEqual([], process.wait_calls)

    def test_daemon_start_http_mismatch_terminates_owned_child(self):
        process = FakeDaemonProcess()
        mismatch = {
            "ok": True,
            "op": "ping",
            "pid": process.pid,
            "http": {"enabled": False},
        }
        client = SequencedPingClient(
            [
                SidecarClientError("offline", code="connection_failed"),
                mismatch,
                mismatch,
            ]
        )
        signals, killpg = controlled_process_group(process)
        stderr = io.StringIO()

        with (
            mock.patch("sidecar.cli.subprocess.Popen", return_value=process),
            mock.patch(
                "sidecar.cli.os.getpgid",
                return_value=process.pid,
            ),
            mock.patch("sidecar.cli.os.killpg", side_effect=killpg),
        ):
            code = main(
                ["daemon", "start", "--http"],
                client=client,
                stdout=io.StringIO(),
                stderr=stderr,
            )

        self.assertEqual(1, code)
        self.assertIn(signal.SIGTERM, signals)
        self.assertIn("HTTP configuration", stderr.getvalue())

    def test_daemon_start_poll_exception_still_terminates_and_reaps(self):
        process = FakeDaemonProcess(poll_error=RuntimeError("poll failed"))
        client = SequencedPingClient(
            [SidecarClientError("offline", code="connection_failed")]
        )
        signals, killpg = controlled_process_group(process)
        stderr = io.StringIO()

        with (
            mock.patch("sidecar.cli.subprocess.Popen", return_value=process),
            mock.patch(
                "sidecar.cli.os.getpgid",
                return_value=process.pid,
            ),
            mock.patch("sidecar.cli.os.killpg", side_effect=killpg),
        ):
            code = main(
                ["daemon", "start"],
                client=client,
                stdout=io.StringIO(),
                stderr=stderr,
            )

        self.assertEqual(1, code)
        self.assertIn(signal.SIGTERM, signals)
        self.assertTrue(process.wait_calls)
        self.assertIn("polling failed", stderr.getvalue())

    def test_daemon_start_keyboard_interrupt_cleans_child_and_returns_130(self):
        process = FakeDaemonProcess()
        client = SequencedPingClient(
            [
                SidecarClientError("offline", code="connection_failed"),
                KeyboardInterrupt(),
                SidecarClientError("offline", code="connection_failed"),
            ]
        )
        signals, killpg = controlled_process_group(process)

        with (
            mock.patch("sidecar.cli.subprocess.Popen", return_value=process),
            mock.patch(
                "sidecar.cli.os.getpgid",
                return_value=process.pid,
            ),
            mock.patch("sidecar.cli.os.killpg", side_effect=killpg),
        ):
            code = main(
                ["daemon", "start"],
                client=client,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(130, code)
        self.assertIn(signal.SIGTERM, signals)
        self.assertTrue(process.wait_calls)

    def test_daemon_start_kills_surviving_descendants_with_group_fallback(self):
        process = FakeDaemonProcess()
        client = SequencedPingClient(
            [SidecarClientError("offline", code="connection_failed")]
        )
        signals, killpg = controlled_process_group(
            process,
            survive_term=True,
        )

        with (
            mock.patch("sidecar.cli.subprocess.Popen", return_value=process),
            mock.patch("sidecar.cli.DAEMON_START_TIMEOUT", 0.0),
            mock.patch("sidecar.cli.DAEMON_CHILD_TERM_TIMEOUT", 0.0),
            mock.patch(
                "sidecar.cli.os.getpgid",
                return_value=process.pid,
            ),
            mock.patch("sidecar.cli.os.killpg", side_effect=killpg),
        ):
            code = main(
                ["daemon", "start"],
                client=client,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

        self.assertEqual(1, code)
        self.assertIn(signal.SIGTERM, signals)
        self.assertIn(signal.SIGKILL, signals)
        self.assertTrue(process.wait_calls)

    def test_daemon_start_timeout_cleans_real_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "pids"
            script = (
                "import os,signal,subprocess,sys,time;"
                "child=subprocess.Popen([sys.executable,'-c',"
                "\"import signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "time.sleep(30)\"]);"
                "open(sys.argv[1],'w').write("
                "str(os.getpid())+' '+str(child.pid));"
                "time.sleep(30)"
            )
            client = SequencedPingClient(
                [SidecarClientError("offline", code="connection_failed")]
            )
            process_group = None
            try:
                with (
                    mock.patch(
                        "sidecar.cli.resolve_runtime_prefix",
                        return_value=(
                            sys.executable,
                            "-c",
                            script,
                            str(pid_path),
                        ),
                    ),
                    mock.patch("sidecar.cli.DAEMON_START_TIMEOUT", 0.2),
                    mock.patch("sidecar.cli.DAEMON_POLL_INTERVAL", 0.01),
                    mock.patch("sidecar.cli.DAEMON_CHILD_TERM_TIMEOUT", 0.1),
                    mock.patch("sidecar.cli.DAEMON_CHILD_KILL_TIMEOUT", 1.0),
                ):
                    code = main(
                        ["daemon", "start"],
                        client=client,
                        stdout=io.StringIO(),
                        stderr=io.StringIO(),
                    )

                self.assertEqual(1, code)
                self.assertTrue(pid_path.exists())
                process_group, descendant = [
                    int(value)
                    for value in pid_path.read_text(encoding="ascii").split()
                ]
                self.assertGreater(descendant, 0)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(process_group, 0)
            finally:
                if process_group is not None:
                    try:
                        os.killpg(process_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_daemon_start_does_not_signal_reused_child_pid(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            socket_path = runtime / "daemon.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(socket_path))
            listener.listen(1)
            process = FakeDaemonProcess()
            (runtime / "daemon.pid").write_text(
                "{}\n".format(process.pid),
                encoding="ascii",
            )

            def exited_poll():
                process.returncode = 0
                return 0

            process.poll = exited_poll
            ready = {"ok": True, "op": "ping", "pid": process.pid}
            client = SequencedPingClient(
                [
                    SidecarClientError("offline", code="connection_failed"),
                    SidecarClientError("offline", code="connection_failed"),
                    ready,
                ],
                socket_path=socket_path,
            )
            stdout = io.StringIO()

            try:
                with (
                    mock.patch(
                        "sidecar.cli.subprocess.Popen",
                        return_value=process,
                    ),
                    mock.patch("sidecar.cli.os.killpg") as killpg,
                ):
                    code = main(
                        ["daemon", "start"],
                        client=client,
                        stdout=stdout,
                        stderr=io.StringIO(),
                    )
            finally:
                listener.close()

            self.assertEqual(0, code)
            self.assertIn("already running", stdout.getvalue())
            killpg.assert_not_called()
            self.assertTrue(process.wait_calls)
            self.assertTrue(socket_path.exists())
            self.assertTrue((runtime / "daemon.pid").exists())

    def test_daemon_start_preserves_unrelated_ready_daemon(self):
        process = FakeDaemonProcess()
        unrelated_pid = process.pid + 1
        ready = {"ok": True, "op": "ping", "pid": unrelated_pid}
        client = SequencedPingClient(
            [
                SidecarClientError("offline", code="connection_failed"),
                ready,
                ready,
            ]
        )
        signals, killpg = controlled_process_group(process)
        stdout = io.StringIO()

        def getpgid(pid):
            return pid

        with (
            mock.patch("sidecar.cli.subprocess.Popen", return_value=process),
            mock.patch("sidecar.cli.os.getpgid", side_effect=getpgid),
            mock.patch("sidecar.cli.os.killpg", side_effect=killpg),
        ):
            code = main(
                ["daemon", "start"],
                client=client,
                stdout=stdout,
                stderr=io.StringIO(),
            )

        self.assertEqual(0, code)
        self.assertIn("already running", stdout.getvalue())
        self.assertIn(signal.SIGTERM, signals)

    def test_daemon_http_parser_defaults_and_port_validation(self):
        default = build_parser().parse_args(["daemon", "start", "--http"])
        selected = build_parser().parse_args(
            ["daemon", "run", "--http", "--http-port", "65535"]
        )

        self.assertTrue(default.http)
        self.assertIsNone(default.http_port)
        self.assertTrue(selected.http)
        self.assertEqual(65535, selected.http_port)
        for value in ("-1", "65536", "invalid"):
            with self.subTest(value=value):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        build_parser().parse_args(
                            ["daemon", "start", "--http", "--http-port", value]
                        )
                self.assertEqual(2, raised.exception.code)

    @mock.patch("sidecar.cli.subprocess.Popen")
    def test_daemon_http_port_requires_http_without_starting(self, popen):
        for command in ("start", "run"):
            with self.subTest(command=command):
                stderr = io.StringIO()
                code = main(
                    ["daemon", command, "--http-port", "0"],
                    client=OfflineClient(),
                    stdout=io.StringIO(),
                    stderr=stderr,
                )
                self.assertEqual(2, code)
                self.assertIn("--http-port requires --http", stderr.getvalue())
        popen.assert_not_called()

    @mock.patch("sidecar.cli.subprocess.Popen")
    def test_daemon_start_forwards_exact_http_flags_and_waits_for_http_ping(
        self,
        popen,
    ):
        class StartingHttpClient:
            socket_path = Path("/tmp/private-runtime/daemon.sock")

            def __init__(self):
                self.calls = 0

            def ping(self):
                self.calls += 1
                if self.calls == 1:
                    raise SidecarClientError("offline", code="connection_failed")
                return {
                    "ok": True,
                    "op": "ping",
                    "pid": 7654,
                    "http": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": 43123,
                    },
                }

        popen.return_value.pid = 7654
        popen.return_value.returncode = None
        popen.return_value.poll.return_value = None
        stdout = io.StringIO()
        code = main(
            ["daemon", "start", "--http", "--http-port", "43123"],
            client=StartingHttpClient(),
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, code)
        self.assertEqual(
            [
                "daemon",
                "run",
                "--http",
                "--http-port",
                "43123",
            ],
            popen.call_args.args[0][-5:],
        )
        self.assertIn("http://127.0.0.1:43123", stdout.getvalue())
        self.assertIn("/tmp/private-runtime/http.token", stdout.getvalue())

    @mock.patch("sidecar.cli.subprocess.Popen")
    def test_daemon_start_http_rejects_existing_off_or_wrong_port(self, popen):
        cases = (
            (None, ["daemon", "start", "--http"]),
            (
                {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 43123,
                },
                [
                    "daemon",
                    "start",
                    "--http",
                    "--http-port",
                    "43124",
                ],
            ),
        )
        for http, argv in cases:
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                code = main(
                    argv,
                    client=FakeClient(pid=99, http=http),
                    stdout=io.StringIO(),
                    stderr=stderr,
                )
                self.assertEqual(1, code)
                self.assertIn("does not match", stderr.getvalue())
        popen.assert_not_called()

    @mock.patch("sidecar.cli.subprocess.Popen")
    def test_daemon_start_http_port_zero_accepts_existing_bound_port(self, popen):
        stdout = io.StringIO()
        code = main(
            ["daemon", "start", "--http"],
            client=FakeClient(
                pid=99,
                http={
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": 43123,
                },
            ),
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, code)
        self.assertIn("already running", stdout.getvalue())
        self.assertIn("43123", stdout.getvalue())
        popen.assert_not_called()

    @mock.patch("sidecar.cli.run_foreground")
    def test_daemon_run_passes_http_configuration_to_foreground(self, run):
        for argv, expected in (
            (["daemon", "run", "--http"], 0),
            (
                ["daemon", "run", "--http", "--http-port", "43123"],
                43123,
            ),
        ):
            with self.subTest(argv=argv):
                run.reset_mock()
                code = main(
                    argv,
                    client=OfflineClient(),
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
                self.assertEqual(0, code)
                self.assertEqual(expected, run.call_args.kwargs["http_port"])

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

    @mock.patch("sidecar.cli._socket_is_live", side_effect=(True, False))
    @mock.patch("sidecar.cli.os.kill")
    def test_daemon_stop_signals_only_verified_pid(self, kill, socket_is_live):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            pidfile = runtime / "daemon.pid"
            socket_path = runtime / "daemon.sock"

            def signal_process(pid, signum):
                self.assertEqual(55, pid)
                if signum == 0:
                    raise ProcessLookupError

            kill.side_effect = signal_process
            daemon_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            daemon_socket.bind(str(socket_path))
            daemon_socket.close()
            pidfile.write_text("55\n", encoding="ascii")

            class StoppingClient(FakeClient):
                def __init__(self):
                    super().__init__(pid=55, socket_path=socket_path)
                    self.pings = 0

                def ping(self):
                    self.pings += 1
                    if self.pings > 1:
                        raise SidecarClientError("offline", code="connection_failed")
                    return super().ping()

            stdout = io.StringIO()
            client = StoppingClient()
            code = main(
                ["daemon", "stop"],
                client=client,
                stdout=stdout,
                stderr=io.StringIO(),
            )

            self.assertEqual(0, code)
            self.assertEqual(3, client.pings)
            self.assertEqual(
                [mock.call(55, signal.SIGTERM), mock.call(55, 0)],
                kill.call_args_list,
            )
            self.assertEqual(2, socket_is_live.call_count)
            self.assertFalse(socket_path.exists())
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

    def test_daemon_status_reports_http_url_and_token_path_not_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            secret = "do-not-print-this-token"
            (runtime / "http.token").write_text(secret, encoding="ascii")
            stdout = io.StringIO()

            code = main(
                ["daemon", "status"],
                client=FakeClient(
                    pid=44,
                    socket_path=runtime / "daemon.sock",
                    http={
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": 43123,
                    },
                ),
                stdout=stdout,
                stderr=io.StringIO(),
            )

        self.assertEqual(0, code)
        self.assertIn("http://127.0.0.1:43123", stdout.getvalue())
        self.assertIn(str(runtime / "http.token"), stdout.getvalue())
        self.assertNotIn(secret, stdout.getvalue())

    def test_service_parser_and_install_forward_exact_options(self):
        parsed = build_parser().parse_args(
            [
                "service",
                "install",
                "--http",
                "--http-port",
                "43123",
                "--force",
            ]
        )
        calls = []
        client = object()

        def install(**kwargs):
            calls.append(kwargs)
            return ServiceResult(0, "service installed and running")

        stdout = io.StringIO()
        code = main(
            [
                "service",
                "install",
                "--http",
                "--http-port",
                "43123",
                "--force",
            ],
            client=client,
            stdout=stdout,
            stderr=io.StringIO(),
            service_installer=install,
        )

        self.assertEqual("install", parsed.service_command)
        self.assertTrue(parsed.http)
        self.assertEqual(43123, parsed.http_port)
        self.assertTrue(parsed.force)
        self.assertEqual(0, code)
        self.assertEqual(
            {
                "http": True,
                "http_port": 43123,
                "force": True,
                "client": client,
            },
            calls[0],
        )
        self.assertIn("installed", stdout.getvalue())

    def test_service_http_port_requires_http_before_install(self):
        install = mock.Mock()
        stderr = io.StringIO()

        code = main(
            ["service", "install", "--http-port", "43123"],
            client=OfflineClient(),
            stdout=io.StringIO(),
            stderr=stderr,
            service_installer=install,
        )

        self.assertEqual(2, code)
        self.assertIn("--http-port requires --http", stderr.getvalue())
        install.assert_not_called()

    def test_service_status_exit_one_is_reported_on_stdout(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["service", "status"],
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
            service_status_provider=lambda **kwargs: ServiceResult(
                1,
                "service is unloaded",
            ),
        )

        self.assertEqual(1, code)
        self.assertEqual("service is unloaded\n", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_service_control_error_is_sanitized_to_stderr(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["service", "uninstall"],
            client=OfflineClient(),
            stdout=stdout,
            stderr=stderr,
            service_uninstaller=lambda **kwargs: ServiceResult(
                2,
                "unsupported\rplatform\x1b[31m",
            ),
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertNotIn("\r", stderr.getvalue())
        self.assertNotIn("\x1b", stderr.getvalue())

    def test_tui_command_passes_once_to_runner(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            kwargs["client"].status()
            kwargs["client"].status()
            return 7

        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            ["tui", "--once"],
            scanner=FakeScanner(),
            client=FakeClient(
                tail_errors=[
                    {
                        "agent": "cursor-cli",
                        "session_id": "tui-tail",
                        "code": "CursorChatSourceError",
                    }
                ]
            ),
            stdout=stdout,
            stderr=stderr,
            tui_runner=runner,
        )

        self.assertEqual(7, code)
        self.assertTrue(calls[0]["once"])
        self.assertIs(stdout, calls[0]["stdout"])
        self.assertEqual(
            "tail error: cursor-cli session=tui-tail "
            "code=CursorChatSourceError\n",
            stderr.getvalue(),
        )

    def test_tui_tail_error_dedupe_rotates_at_bound(self):
        source = FakeClient()

        def tail_error(index):
            return {
                "agent": "cursor-cli",
                "session_id": "tui-tail-{}".format(index),
                "code": "CursorChatSourceError",
            }

        def runner(**kwargs):
            client = kwargs["client"]
            for index in range(TAIL_ERROR_DEDUPE_LIMIT + 1):
                source.tail_errors = [tail_error(index)]
                client.status()
            source.tail_errors = [tail_error(0)]
            client.status()
            client.status()
            return 0

        stderr = io.StringIO()
        code = main(
            ["tui", "--once"],
            scanner=FakeScanner(),
            client=source,
            stdout=io.StringIO(),
            stderr=stderr,
            tui_runner=runner,
        )
        lines = stderr.getvalue().splitlines()

        self.assertEqual(0, code)
        self.assertEqual(TAIL_ERROR_DEDUPE_LIMIT + 2, len(lines))
        self.assertEqual(
            2,
            sum(" session=tui-tail-0 " in line for line in lines),
        )
        self.assertEqual(
            "tail error: cursor-cli session=tui-tail-0 "
            "code=CursorChatSourceError",
            lines[-1],
        )
        self.assertEqual(TAIL_ERROR_DEDUPE_LIMIT + 3, source.status_calls)


class SendCLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name).resolve() / "home"
        self.home.mkdir(mode=0o700)
        self.home_environment = mock.patch.dict(
            os.environ,
            {"HOME": str(self.home)},
        )
        self.home_environment.start()
        self.account_home = mock.patch(
            "sidecar.send_audit.pwd.getpwuid",
            return_value=mock.Mock(pw_dir=str(self.home)),
        )
        self.account_home.start()

    def tearDown(self):
        self.account_home.stop()
        self.home_environment.stop()
        self.temporary.cleanup()

    def result(
        self,
        session,
        *,
        outcome="completed",
        delivery=None,
        response="",
        stderr="",
        error_code=None,
        request_id="",
        replayed=False,
    ):
        return SendResult(
            agent=session.agent,
            session_id=session.session_id,
            outcome=outcome,
            delivery=(
                "delivered" if outcome == "completed" else "unknown"
            )
            if delivery is None
            else delivery,
            returncode=0 if outcome == "completed" else 7,
            response=response,
            stderr=stderr,
            error_code=error_code,
            request_id=request_id,
            replayed=replayed,
        )

    def test_audit_reset_requires_both_confirmations_without_mutation(self):
        cases = (
            ["audit", "reset"],
            ["audit", "reset", "--allow-write"],
            ["audit", "reset", "--confirm", "CLEAR-SEND-AUDIT"],
            [
                "audit",
                "reset",
                "--allow-write",
                "--confirm",
                "clear-send-audit",
            ],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                calls = []
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = main(
                    argv,
                    stdout=stdout,
                    stderr=stderr,
                    audit_resetter=lambda: calls.append(True),
                )
                self.assertEqual(2, code)
                self.assertEqual([], calls)
                self.assertEqual("", stdout.getvalue())
                self.assertIn("requires --allow-write", stderr.getvalue())

    def test_audit_reset_success_and_busy_output(self):
        argv = [
            "audit",
            "reset",
            "--allow-write",
            "--confirm",
            "CLEAR-SEND-AUDIT",
        ]
        calls = []
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            argv,
            stdout=stdout,
            stderr=stderr,
            audit_resetter=lambda: calls.append(True),
        )
        self.assertEqual(0, code)
        self.assertEqual([True], calls)
        self.assertEqual("send audit reset\n", stdout.getvalue())
        self.assertIn("idempotency history has been lost", stderr.getvalue())

        def busy():
            raise AuditError("audit_busy")

        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            argv,
            stdout=stdout,
            stderr=stderr,
            audit_resetter=busy,
        )
        self.assertEqual(1, code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("send audit is active", stderr.getvalue())

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
        explicit = parser.parse_args(
            ["send", "abc", "hello", "--request-id", "request-explicit"]
        )

        self.assertIn("Observation commands are read-only", global_help)
        self.assertIn("may modify agent state", global_help)
        self.assertNotIn("without modifying", global_help)
        self.assertIn("local headless resume", send_help)
        self.assertIn("may modify agent state", send_help)
        self.assertIn("--allow-write", send_help)
        self.assertIn("--agent NAME", send_help)
        self.assertIn("--exact-session", send_help)
        self.assertIn("--timeout SEC", send_help)
        self.assertIn("--request-id ID", send_help)
        self.assertIn("--json", send_help)
        self.assertNotIn("--remote", send_help)
        self.assertEqual("abc", args.session_prefix)
        self.assertEqual("hello", args.message)
        self.assertFalse(args.allow_write)
        self.assertFalse(args.exact_session)
        self.assertEqual(DEFAULT_SEND_TIMEOUT_SECONDS, args.timeout)
        self.assertIsNone(args.request_id)
        self.assertEqual("request-explicit", explicit.request_id)
        self.assertEqual("-private", hyphen.message)
        self.assertTrue(hyphen.allow_write)

    def test_send_agent_and_stdin_options_are_order_independent(self):
        parser = build_parser()
        cases = (
            [
                "send",
                "abc",
                "--agent",
                "CLAUDE",
                "--exact-session",
                "--message-stdin",
                "--allow-write",
                "--request-id",
                "request-order",
                "--json",
            ],
            [
                "send",
                "--json",
                "--request-id",
                "request-order",
                "--allow-write",
                "--message-stdin",
                "--exact-session",
                "--agent",
                "CLAUDE",
                "abc",
            ],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual("abc", args.session_prefix)
                self.assertEqual("CLAUDE", args.agent)
                self.assertTrue(args.exact_session)
                self.assertTrue(args.message_stdin)
                self.assertTrue(args.allow_write)
                self.assertEqual("request-order", args.request_id)
                self.assertTrue(args.json)
                self.assertIsNone(args.message)

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

    def test_request_id_rejects_abbreviation_and_unsafe_values(self):
        parser = build_parser()
        for abbreviation in ("--r", "--request", "--request-i"):
            with self.subTest(abbreviation=abbreviation):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        parser.parse_args(
                            [
                                "send",
                                "abc",
                                "private",
                                abbreviation,
                                "request-safe",
                            ]
                        )
                self.assertEqual(2, raised.exception.code)
        for unsafe in ("-leading", "contains space", "雪", "x" * 129):
            with self.subTest(unsafe=unsafe):
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as raised:
                        parser.parse_args(
                            [
                                "send",
                                "abc",
                                "private",
                                "--request-id",
                                unsafe,
                            ]
                        )
                self.assertEqual(2, raised.exception.code)
                self.assertNotIn(unsafe, errors.getvalue())

    def test_omitted_request_id_is_generated_and_returned(self):
        session = make_session("auto-request")
        captured = []
        stdout = io.StringIO()

        def executor(plan, **kwargs):
            del plan
            request_id = kwargs["request_id"]
            captured.append(request_id)
            return self.result(session, request_id=request_id)

        code = main(
            ["send", "auto", "private", "--allow-write", "--json"],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=io.StringIO(),
            send_planner=lambda selected, message: object(),
            send_executor=executor,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(0, code)
        self.assertEqual(1, len(captured))
        self.assertEqual(captured[0], payload["request_id"])
        self.assertFalse(payload["replayed"])
        self.assertGreaterEqual(len(captured[0]), 32)

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
                    [
                        "send",
                        prefix,
                        "private",
                        "--allow-write",
                        "--request-id",
                        "request-" + prefix,
                    ],
                    scanner=scanner,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                    send_planner=planner,
                    send_executor=lambda plan, **kwargs: self.result(
                        expected,
                        request_id=kwargs["request_id"],
                    ),
                )

                self.assertEqual(0, code)
                self.assertEqual([(expected, "private")], selected)
                self.assertEqual([None], scanner.recent_calls)

    def test_send_agent_filter_is_case_normalized_before_prefix_resolution(self):
        claude = make_session("shared-session", agent="claude")
        codex = make_session("shared-session", agent="codex")
        codex_longer = make_session("shared-session-longer", agent="codex")
        selected = []

        def planner(session, message):
            selected.append((session, message))
            return object()

        def executor(plan, **kwargs):
            del plan
            return self.result(
                codex,
                request_id=kwargs["request_id"],
            )

        code = main(
            [
                "send",
                "shared-session",
                "private",
                "--agent",
                "CoDeX",
                "--exact-session",
                "--allow-write",
                "--request-id",
                "request-agent-bound",
                "--json",
            ],
            scanner=FakeScanner([claude, codex, codex_longer]),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            send_planner=planner,
            send_executor=executor,
        )

        self.assertEqual(0, code)
        self.assertEqual([(codex, "private")], selected)

    def test_send_agent_selection_errors_are_stable_json_without_execution(self):
        cases = (
            (
                "wrong agent",
                ["send", "same-id", "PRIVATE", "--agent", "codex"],
                [make_session("same-id", agent="claude")],
                "target_not_found",
            ),
            (
                "missing target",
                ["send", "missing", "PRIVATE", "--agent", "claude"],
                [make_session("other", agent="claude")],
                "target_not_found",
            ),
            (
                "ambiguous within agent",
                ["send", "amb", "PRIVATE", "--agent", "claude"],
                [
                    make_session("amb-one", agent="claude"),
                    make_session("amb-two", agent="claude"),
                    make_session("amb-only-codex", agent="codex"),
                ],
                "ambiguous_session",
            ),
            (
                "exact target replaced by same-agent longer prefix",
                [
                    "send",
                    "same-id",
                    "PRIVATE",
                    "--agent",
                    "claude",
                    "--exact-session",
                ],
                [make_session("same-id-longer", agent="claude")],
                "target_not_found",
            ),
            (
                "duplicate exact target within agent",
                [
                    "send",
                    "duplicate",
                    "PRIVATE",
                    "--agent",
                    "claude",
                    "--exact-session",
                ],
                [
                    make_session("duplicate", agent="claude"),
                    make_session("duplicate", agent="claude"),
                ],
                "ambiguous_session",
            ),
        )
        for label, base_argv, sessions, expected_code in cases:
            with self.subTest(label=label):
                calls = []
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = main(
                    base_argv + ["--allow-write", "--json"],
                    scanner=FakeScanner(sessions),
                    stdout=stdout,
                    stderr=stderr,
                    send_planner=lambda *args, **kwargs: calls.append(
                        ("plan", args, kwargs)
                    ),
                    send_executor=lambda *args, **kwargs: calls.append(
                        ("execute", args, kwargs)
                    ),
                )

                self.assertEqual(2, code)
                self.assertEqual(
                    {"code": expected_code},
                    json.loads(stdout.getvalue()),
                )
                self.assertNotIn("PRIVATE", stderr.getvalue())
                self.assertEqual([], calls)

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
            refresher = kwargs.pop("refresher")
            self.assertTrue(callable(refresher))
            self.assertEqual([session], refresher())
            executor_calls.append((selected_plan, kwargs))
            return self.result(
                session,
                response="done",
                request_id=kwargs["request_id"],
            )

        code = main(
            [
                "send",
                "supported",
                "private prompt",
                "--allow-write",
                "--timeout",
                "42.5",
                "--request-id",
                "request-contract",
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
            [
                (
                    plan,
                    {
                        "allow_write": True,
                        "timeout": 42.5,
                        "request_id": "request-contract",
                    },
                )
            ],
            executor_calls,
        )
        self.assertEqual([None, None], scanner.recent_calls)
        self.assertEqual(0, client.status_calls)
        self.assertEqual(
            self.result(
                session,
                response="done",
                request_id="request-contract",
            ).to_dict(),
            json.loads(stdout.getvalue()),
        )

    def test_kimi_exact_stdin_json_send_binds_receipt_triple(self):
        message = "KIMI-CLI-PRIVATE"
        session = make_session("kimi-exact-session", agent="kimi")
        scanner = FakeScanner(
            [
                session,
                make_session("kimi-exact-session-longer", agent="kimi"),
                make_session("kimi-exact-session", agent="claude"),
            ]
        )
        planned = object()
        planner_calls = []
        executor_calls = []
        stdout = io.StringIO()
        stderr = io.StringIO()

        def planner(selected, submitted):
            planner_calls.append((selected, submitted))
            return planned

        def executor(plan, **kwargs):
            refreshed = kwargs.pop("refresher")()
            executor_calls.append((plan, kwargs, refreshed))
            return SendResult(
                agent="kimi",
                session_id=session.session_id,
                outcome="completed",
                delivery="unknown",
                returncode=0,
                request_id=kwargs["request_id"],
            )

        argv = [
            "send",
            session.session_id,
            "--agent",
            "kimi",
            "--exact-session",
            "--message-stdin",
            "--allow-write",
            "--request-id",
            "request-kimi-cli",
            "--json",
        ]
        code = main(
            argv,
            scanner=scanner,
            stdin=io.BytesIO(message.encode("utf-8")),
            stdout=stdout,
            stderr=stderr,
            send_planner=planner,
            send_executor=executor,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(1, code)
        self.assertEqual([(session, message)], planner_calls)
        self.assertEqual(
            [
                (
                    planned,
                    {
                        "allow_write": True,
                        "timeout": DEFAULT_SEND_TIMEOUT_SECONDS,
                        "request_id": "request-kimi-cli",
                    },
                    [session],
                )
            ],
            executor_calls,
        )
        self.assertEqual(
            ("kimi", session.session_id, "request-kimi-cli"),
            (
                payload["agent"],
                payload["session_id"],
                payload["request_id"],
            ),
        )
        self.assertEqual(("completed", "unknown"), (
            payload["outcome"],
            payload["delivery"],
        ))
        self.assertIn(
            "completed but delivery unknown; do not retry",
            stderr.getvalue(),
        )
        self.assertNotIn("delivered", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(message, " ".join(argv))

    def test_completed_unknown_text_and_audit_replay_are_not_success(self):
        session = make_session("kimi-unknown-replay", agent="kimi")
        result = self.result(
            session,
            delivery="unknown",
            request_id="request-kimi-replay",
            replayed=True,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            [
                "send",
                "kimi-unknown",
                "private",
                "--allow-write",
                "--request-id",
                "request-kimi-replay",
            ],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=stderr,
            send_planner=lambda selected, message: object(),
            send_executor=lambda plan, **kwargs: result,
        )
        rendered = stdout.getvalue() + stderr.getvalue()

        self.assertEqual(1, code)
        self.assertEqual(
            "request_id=request-kimi-replay replayed=true\n"
            "completed but delivery unknown for kimi:kimi-unknown-replay; "
            "do not retry\n",
            stdout.getvalue(),
        )
        self.assertIn(
            "send: completed but delivery unknown; do not retry\n",
            stderr.getvalue(),
        )
        self.assertNotIn("delivered", rendered)

    def test_claude_cleanup_unknown_remains_failure_in_json_and_text(self):
        session = make_session("claude-cleanup", agent="claude")
        result = self.result(
            session,
            outcome="failed",
            delivery="unknown",
            error_code="cleanup_incomplete",
            request_id="request-claude-cleanup",
        )

        for as_json in (False, True):
            with self.subTest(as_json=as_json):
                stdout = io.StringIO()
                stderr = io.StringIO()
                argv = [
                    "send",
                    "claude-cleanup",
                    "private",
                    "--allow-write",
                    "--request-id",
                    "request-claude-cleanup",
                ]
                if as_json:
                    argv.append("--json")

                code = main(
                    argv,
                    scanner=FakeScanner([session]),
                    stdout=stdout,
                    stderr=stderr,
                    send_planner=lambda selected, message: object(),
                    send_executor=lambda plan, **kwargs: result,
                )
                rendered = stdout.getvalue() + stderr.getvalue()

                self.assertEqual(1, code)
                if as_json:
                    self.assertEqual(result.to_dict(), json.loads(stdout.getvalue()))
                else:
                    self.assertIn(
                        "delivery unknown for claude:claude-cleanup (failed)",
                        stdout.getvalue(),
                    )
                self.assertIn("cleanup_incomplete", stderr.getvalue())
                self.assertNotIn("delivered", rendered)

    def test_send_refresh_is_bound_to_selected_agent_and_exact_session(self):
        original = make_session("same-id", agent="claude")
        replacement = make_session("same-id-longer", agent="claude")

        class ReplacingScanner:
            def __init__(self):
                self.errors = []
                self.calls = 0

            def scan(self, recent_seconds=None):
                del recent_seconds
                self.calls += 1
                return [original] if self.calls == 1 else [replacement]

        scanner = ReplacingScanner()
        refreshed = []

        def executor(plan, **kwargs):
            del plan
            refreshed.extend(kwargs["refresher"]())
            raise SendError("session_unavailable")

        stdout = io.StringIO()
        code = main(
            [
                "send",
                "same-id",
                "PRIVATE",
                "--agent",
                "claude",
                "--exact-session",
                "--allow-write",
                "--request-id",
                "request-replaced",
                "--json",
            ],
            scanner=scanner,
            stdout=stdout,
            stderr=io.StringIO(),
            send_planner=lambda selected, message: object(),
            send_executor=executor,
        )

        self.assertEqual(2, code)
        self.assertEqual({"code": "session_unavailable"}, json.loads(stdout.getvalue()))
        self.assertEqual([], refreshed)

    def test_send_result_identity_mismatch_is_terminal_unknown(self):
        selected = make_session("bound-session", agent="claude")
        mismatches = (
            self.result(
                make_session("bound-session", agent="codex"),
                request_id="request-bound",
            ),
            self.result(
                make_session("replacement", agent="claude"),
                request_id="request-bound",
            ),
            self.result(selected, request_id="request-other"),
            self.result(
                make_session("replacement", agent="claude"),
                outcome="timed_out",
                error_code="timeout",
                request_id="request-bound",
            ),
        )
        for result in mismatches:
            with self.subTest(result=result):
                stdout = io.StringIO()
                code = main(
                    [
                        "send",
                        "bound-session",
                        "PRIVATE",
                        "--agent",
                        "claude",
                        "--allow-write",
                        "--request-id",
                        "request-bound",
                        "--json",
                    ],
                    scanner=FakeScanner([selected]),
                    stdout=stdout,
                    stderr=io.StringIO(),
                    send_planner=lambda target, message: object(),
                    send_executor=lambda plan, **kwargs: result,
                )
                payload = json.loads(stdout.getvalue())

                self.assertEqual(1, code)
                self.assertEqual("claude", payload["agent"])
                self.assertEqual("bound-session", payload["session_id"])
                self.assertEqual("request-bound", payload["request_id"])
                self.assertEqual("unknown", payload["delivery"])
                self.assertEqual("failed", payload["outcome"])
                self.assertEqual("executor_error", payload["error_code"])

    def test_send_refreshes_directly_and_blocks_source_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            transcript = root / "session.jsonl"
            transcript.write_text("{}\n", encoding="utf-8")
            executable = root / "agent"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            session = Session(
                agent="claude",
                session_id="fresh-session",
                project=str(root),
                transcript=str(transcript),
                updated_at=1.0,
                status=Status.WAITING,
                extra={},
            )
            scanner = FakeScanner([session])
            runner_calls = []

            def planner(selected, message):
                plan = build_send_plan(
                    selected,
                    message,
                    executable_resolver=lambda _name: str(executable),
                )
                transcript.write_text('{"changed":true}\n', encoding="utf-8")
                return plan

            def executor(plan, **kwargs):
                def runner(*args, **runner_kwargs):
                    runner_kwargs["pre_spawn"]()
                    runner_calls.append((args, runner_kwargs))

                return execute_send(
                    plan,
                    allow_write=kwargs["allow_write"],
                    timeout=kwargs["timeout"],
                    refresher=kwargs["refresher"],
                    executable_resolver=lambda _name: str(executable),
                    runtime_dir=root / "runtime",
                    runner=runner,
                )

            stderr = io.StringIO()
            code = main(
                [
                    "send",
                    "fresh",
                    "SOURCE-MUTATION-SECRET",
                    "--allow-write",
                ],
                scanner=scanner,
                stdout=io.StringIO(),
                stderr=stderr,
                send_planner=planner,
                send_executor=executor,
            )

        self.assertEqual(2, code)
        self.assertEqual([None, None], scanner.recent_calls)
        self.assertEqual([], runner_calls)
        self.assertIn("session changed", stderr.getvalue())
        self.assertNotIn("SOURCE-MUTATION-SECRET", stderr.getvalue())

    def test_json_success_is_exact_and_preserves_raw_result_text(self):
        session = make_session("json-session")
        result = self.result(
            session,
            response="\x1b[31mraw response\r",
            stderr="\x1b]0;raw warning\x07",
            request_id="request-json",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            [
                "send",
                "json",
                "private",
                "--allow-write",
                "--request-id",
                "request-json",
                "--json",
            ],
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

    def test_json_send_hygiene_handles_escapes_surrogates_and_large_text(self):
        session = make_session("json-hygiene")
        message = 'secret "line"\n雪'
        escaped = json.dumps(message, ensure_ascii=False)[1:-1]
        ascii_escaped = json.dumps(message, ensure_ascii=True)[1:-1]
        prefix = "x" * (256 * 1024)
        result = self.result(
            session,
            response="{}controls \x1b[31m\t{}|{}|{}".format(
                prefix,
                message,
                escaped,
                ascii_escaped,
            ),
            stderr="\r{} surrogate \ud800".format(message),
            request_id="request-json-hygiene",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            [
                "send",
                "json",
                message,
                "--allow-write",
                "--request-id",
                "request-json-hygiene",
                "--json",
            ],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=stderr,
            send_planner=lambda selected, private_message: object(),
            send_executor=lambda plan, **kwargs: result,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(0, code)
        self.assertEqual("", stderr.getvalue())
        self.assertTrue(payload["response"].startswith(prefix))
        self.assertIn("controls \x1b[31m\t", payload["response"])
        self.assertEqual(3, payload["response"].count("[message redacted]"))
        self.assertEqual("\r[message redacted] surrogate \ufffd", payload["stderr"])
        self.assertNotIn(message, payload["response"] + payload["stderr"])
        self.assertNotIn(escaped, payload["response"])
        self.assertNotIn(ascii_escaped, payload["response"])

    def test_human_success_sanitizes_response_stderr_and_redacts_message(self):
        session = make_session("human-session")
        message = "TOP-SECRET-MESSAGE"
        result = self.result(
            session,
            response="answer \x1b]0;pwned\x07 " + message,
            stderr="warning \x1b[31mred\x1b[0m " + message,
            request_id="request-human",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            [
                "send",
                "human",
                message,
                "--allow-write",
                "--request-id",
                "request-human",
            ],
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
            request_id="request-surrogate",
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
            [
                "send",
                "surrogate",
                message,
                "--allow-write",
                "--request-id",
                "request-surrogate",
            ],
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
                "target_not_found",
            ),
            (
                "unicode-session",
                "\ud800PRIVATE",
                FakeScanner([make_session("unicode-session")]),
                "message must contain valid Unicode scalars",
                "invalid_message_utf8",
            ),
        )
        for prefix, message, scanner, expected, expected_code in cases:
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
                self.assertEqual(
                    {"code": expected_code},
                    json.loads(stdout.getvalue()),
                )
                self.assertIn(expected, diagnostic)
                self.assertNotIn("PRIVATE", diagnostic)
                self.assertNotIn("\\ud800", diagnostic)
                self.assertEqual([], executor_calls)

    def test_human_success_without_response_prints_delivered_receipt(self):
        session = make_session("empty-response", agent="cursor-cli")
        stdout = io.StringIO()

        code = main(
            [
                "send",
                "empty",
                "private",
                "--allow-write",
                "--request-id",
                "request-empty",
            ],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=io.StringIO(),
            send_planner=lambda selected, message: object(),
            send_executor=lambda plan, **kwargs: self.result(
                session,
                request_id=kwargs["request_id"],
            ),
        )

        self.assertEqual(0, code)
        self.assertEqual(
            "request_id=request-empty replayed=false\n"
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
                        request_id="request-" + outcome,
                    )
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    argv = [
                        "send",
                        "runtime",
                        message,
                        "--allow-write",
                        "--request-id",
                        "request-" + outcome,
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
                            "request_id=request-{} replayed=false\n"
                            "delivery unknown for claude:runtime-session ({})\n".format(
                                outcome,
                                outcome,
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
            [
                "send",
                "interrupt",
                message,
                "--allow-write",
                "--request-id",
                "request-interrupt",
            ],
            scanner=FakeScanner([session]),
            stdout=stdout,
            stderr=stderr,
            send_planner=lambda selected, private_message: object(),
            send_executor=lambda plan, **kwargs: (_ for _ in ()).throw(
                KeyboardInterrupt
            ),
        )

        self.assertEqual(130, code)
        self.assertEqual(
            "request_id=request-interrupt replayed=false\n"
            "delivery unknown for claude:interrupt-session (failed)\n",
            stdout.getvalue(),
        )
        self.assertIn("delivery status is unknown", stderr.getvalue())
        self.assertNotIn(message, stderr.getvalue())

    def test_send_parser_and_help_expose_message_stdin_flag(self):
        parser = build_parser()
        command_parsers = next(
            action.choices
            for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )
        send_help = command_parsers["send"].format_help()
        stdin_args = parser.parse_args(["send", "abc", "--message-stdin"])
        positional_args = parser.parse_args(["send", "abc", "hello"])

        self.assertIn("--message-stdin", send_help)
        self.assertIn("standard input", send_help)
        self.assertTrue(stdin_args.message_stdin)
        self.assertIsNone(stdin_args.message)
        self.assertFalse(positional_args.message_stdin)
        self.assertEqual("hello", positional_args.message)

    def test_message_sources_are_mutually_exclusive_usage_errors(self):
        cases = (
            (
                [
                    "send",
                    "abc",
                    "POSITIONAL-SECRET",
                    "--message-stdin",
                    "--allow-write",
                ],
                b"STDIN-SECRET",
            ),
            (["send", "abc", "--allow-write"], b""),
        )
        for argv, stdin_bytes in cases:
            with self.subTest(argv=argv):
                scanner = FakeScanner([make_session("abc")])
                calls = []
                stdout = io.StringIO()
                stderr = io.StringIO()

                code = main(
                    argv,
                    scanner=scanner,
                    stdout=stdout,
                    stderr=stderr,
                    stdin=io.BytesIO(stdin_bytes),
                    send_planner=lambda *args, **kwargs: calls.append(
                        ("plan", args)
                    ),
                    send_executor=lambda *args, **kwargs: calls.append(
                        ("execute", args)
                    ),
                )
                diagnostic = stderr.getvalue()

                self.assertEqual(2, code)
                self.assertEqual("", stdout.getvalue())
                self.assertEqual(1, diagnostic.count("\n"))
                self.assertIn("send: ", diagnostic)
                self.assertIn("--message-stdin", diagnostic)
                self.assertNotIn("SECRET", diagnostic)
                self.assertEqual([], scanner.recent_calls)
                self.assertEqual([], calls)

    def test_message_stdin_channel_matches_positional_channel(self):
        message = "channel prompt 雪\nsecond line"
        session = make_session("channel-session")
        planner_calls = []
        payloads = []

        def planner(selected, received):
            planner_calls.append((selected, received))
            return object()

        def executor(plan, **kwargs):
            return self.result(
                session,
                response="done",
                request_id="request-channel",
            )

        argv_cases = (
            (
                [
                    "send",
                    "channel",
                    message,
                    "--allow-write",
                    "--request-id",
                    "request-channel",
                    "--json",
                ],
                io.BytesIO(b""),
            ),
            (
                [
                    "send",
                    "channel",
                    "--message-stdin",
                    "--allow-write",
                    "--request-id",
                    "request-channel",
                    "--json",
                ],
                io.TextIOWrapper(
                    io.BytesIO(message.encode("utf-8")),
                    encoding="utf-8",
                ),
            ),
        )
        for argv, stdin in argv_cases:
            with self.subTest(argv=argv[2]):
                stdout = io.StringIO()
                stderr = io.StringIO()

                code = main(
                    argv,
                    scanner=FakeScanner([session]),
                    stdout=stdout,
                    stderr=stderr,
                    stdin=stdin,
                    send_planner=planner,
                    send_executor=executor,
                )

                self.assertEqual(0, code)
                self.assertEqual("", stderr.getvalue())
                payloads.append(json.loads(stdout.getvalue()))

        self.assertEqual([(session, message), (session, message)], planner_calls)
        self.assertEqual(payloads[0], payloads[1])
        identities = [
            make_audit_identity(
                agent=session.agent,
                session_id=session.session_id,
                project=session.project,
                executable_basename="claude",
                confirmation_mode="allow_write",
                message=received.encode("utf-8"),
            )
            for _selected, received in planner_calls
        ]
        self.assertEqual(identities[0], identities[1])

    def test_stdin_message_reuses_existing_validation_fail_closed(self):
        cases = (
            (b"", "message must not be blank"),
            (b" \t\n", "message must not be blank"),
            (b"nul\x00byte", "message must not contain NUL"),
        )
        for stdin_bytes, expected in cases:
            with self.subTest(expected=expected, stdin_bytes=stdin_bytes):
                scanner = FakeScanner([make_session("validated-session")])
                stdout = io.StringIO()
                stderr = io.StringIO()

                code = main(
                    ["send", "validated", "--message-stdin", "--allow-write"],
                    scanner=scanner,
                    stdout=stdout,
                    stderr=stderr,
                    stdin=io.BytesIO(stdin_bytes),
                    send_executor=lambda *args, **kwargs: self.fail(
                        "executor must not run"
                    ),
                )

                self.assertEqual(2, code)
                self.assertEqual("", stdout.getvalue())
                self.assertIn("send: " + expected, stderr.getvalue())

    def test_oversized_stdin_message_fails_closed_before_scanning(self):
        scanner = FakeScanner([make_session("large-session")])
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["send", "large", "--message-stdin", "--allow-write"],
            scanner=scanner,
            stdout=stdout,
            stderr=stderr,
            stdin=io.BytesIO(b"x" * (MAX_MESSAGE_BYTES + 1)),
            send_planner=lambda *args, **kwargs: self.fail(
                "planner must not run"
            ),
            send_executor=lambda *args, **kwargs: self.fail(
                "executor must not run"
            ),
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("send: message exceeds the size limit", stderr.getvalue())
        self.assertEqual([], scanner.recent_calls)

    def test_exact_limit_stdin_message_is_accepted(self):
        message = "x" * MAX_MESSAGE_BYTES
        session = make_session("limit-session")
        received = []

        def planner(selected, value):
            received.append(value)
            return object()

        code = main(
            [
                "send",
                "limit",
                "--message-stdin",
                "--allow-write",
                "--request-id",
                "request-limit",
            ],
            scanner=FakeScanner([session]),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            stdin=io.BytesIO(message.encode("utf-8")),
            send_planner=planner,
            send_executor=lambda plan, **kwargs: self.result(
                session,
                request_id=kwargs["request_id"],
            ),
        )

        self.assertEqual(0, code)
        self.assertEqual([message], received)

    def test_invalid_utf8_stdin_message_fails_closed_without_echo(self):
        scanner = FakeScanner([make_session("utf8-session")])
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["send", "utf8", "--message-stdin", "--allow-write"],
            scanner=scanner,
            stdout=stdout,
            stderr=stderr,
            stdin=io.BytesIO(b"\xff\xfe UTF8-SECRET"),
            send_planner=lambda *args, **kwargs: self.fail(
                "planner must not run"
            ),
            send_executor=lambda *args, **kwargs: self.fail(
                "executor must not run"
            ),
        )

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn(
            "send: message must contain valid Unicode scalars",
            stderr.getvalue(),
        )
        self.assertNotIn("UTF8-SECRET", stderr.getvalue())
        self.assertEqual([], scanner.recent_calls)

    def test_interactive_stdin_is_refused_before_reading(self):
        class InteractiveStdin(io.BytesIO):
            def isatty(self):
                return True

            def read(self, *args, **kwargs):
                raise AssertionError(
                    "must not block reading an interactive terminal"
                )

        scanner = FakeScanner([make_session("tty-session")])
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["send", "tty", "--message-stdin", "--allow-write"],
            scanner=scanner,
            stdout=stdout,
            stderr=stderr,
            stdin=InteractiveStdin(b"TYPED-SECRET"),
            send_planner=lambda *args, **kwargs: self.fail(
                "planner must not run"
            ),
            send_executor=lambda *args, **kwargs: self.fail(
                "executor must not run"
            ),
        )
        diagnostic = stderr.getvalue()

        self.assertEqual(2, code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(1, diagnostic.count("\n"))
        self.assertIn(
            "send: standard input is an interactive terminal; "
            "pipe or redirect the message",
            diagnostic,
        )
        self.assertNotIn("TYPED-SECRET", diagnostic)
        self.assertEqual([], scanner.recent_calls)

    def test_keyboard_interrupt_during_stdin_read_exits_130_cleanly(self):
        class InterruptedStdin(io.BytesIO):
            def read(self, *args, **kwargs):
                raise KeyboardInterrupt

        scanner = FakeScanner([make_session("interrupt-session")])
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            ["send", "interrupt", "--message-stdin", "--allow-write"],
            scanner=scanner,
            stdout=stdout,
            stderr=stderr,
            stdin=InterruptedStdin(),
            send_planner=lambda *args, **kwargs: self.fail(
                "planner must not run"
            ),
            send_executor=lambda *args, **kwargs: self.fail(
                "executor must not run"
            ),
        )
        diagnostic = stderr.getvalue()

        self.assertEqual(130, code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(1, diagnostic.count("\n"))
        self.assertIn(
            "send: interrupted while reading the message from standard "
            "input; nothing was delivered",
            diagnostic,
        )
        self.assertNotIn("Traceback", diagnostic)
        self.assertEqual([], scanner.recent_calls)

    def test_unreadable_stdin_reports_dedicated_diagnostic(self):
        class BrokenStdin(io.BytesIO):
            def read(self, *args, **kwargs):
                raise OSError("stdin is closed")

        cases = (
            ("read raises OSError", BrokenStdin()),
            ("text stream yields str chunks", io.StringIO("text chunk")),
        )
        for label, stdin in cases:
            with self.subTest(label=label):
                scanner = FakeScanner([make_session("unreadable-session")])
                stdout = io.StringIO()
                stderr = io.StringIO()

                code = main(
                    ["send", "unreadable", "--message-stdin", "--allow-write"],
                    scanner=scanner,
                    stdout=stdout,
                    stderr=stderr,
                    stdin=stdin,
                    send_executor=lambda *args, **kwargs: self.fail(
                        "executor must not run"
                    ),
                )
                diagnostic = stderr.getvalue()

                self.assertEqual(2, code)
                self.assertEqual("", stdout.getvalue())
                self.assertIn(
                    "send: standard input could not be read as bytes",
                    diagnostic,
                )
                self.assertNotIn("message must be text", diagnostic)
                self.assertEqual([], scanner.recent_calls)

    def test_detached_stdin_raises_send_error_not_attribute_error(self):
        with self.assertRaises(SendError) as raised:
            _read_stdin_message(None)

        self.assertEqual("stdin_unreadable", raised.exception.code)

    def test_pending_tail_backport_is_scoped_to_send_subparser(self):
        parser = build_parser()
        command_parsers = next(
            action.choices
            for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )

        self.assertNotIsInstance(parser, _PendingTailArgumentParser)
        self.assertIsInstance(
            command_parsers["send"],
            _PendingTailArgumentParser,
        )
        for name, subparser in command_parsers.items():
            if name != "send":
                self.assertNotIsInstance(
                    subparser,
                    _PendingTailArgumentParser,
                    "backport must stay scoped to send: {}".format(name),
                )

    def test_pending_tail_matcher_mirrors_upstream_zero_width_semantics(self):
        parser = _PendingTailArgumentParser(prog="probe", add_help=False)
        parser.add_argument("--flag", action="store_true")
        parser.add_argument("head")
        parser.add_argument("tail", nargs="*")

        filled = parser.parse_args(["a", "--flag", "--", "-b"])
        empty = parser.parse_args(["a", "--flag"])

        self.assertEqual("a", filled.head)
        self.assertEqual(["-b"], filled.tail)
        self.assertEqual("a", empty.head)
        self.assertEqual([], empty.tail)


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
