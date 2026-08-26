"""Command-line interface for sidecar observation and local message delivery."""

from __future__ import annotations

import argparse
import datetime as dt
import inspect
import json
import math
import os
import queue
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence, TextIO

import sidecar
from sidecar.adapters.base import sanitize_terminal_text
from sidecar.client import PingInfo, SidecarClient, SidecarClientError
from sidecar.daemon import (
    PIDFILE_NAME,
    SOCKET_NAME,
    DaemonAlreadyRunning,
    DaemonError,
    _socket_is_live,
    default_runtime_dir,
    read_pid,
    run_foreground,
)
from sidecar.inject import (
    DEFAULT_SEND_TIMEOUT_SECONDS,
    MAX_MESSAGE_BYTES,
    SendError,
    SendResult,
    build_send_plan,
    execute_send,
)
from sidecar.launchd import (
    ServiceResult,
    install_service,
    service_status,
    uninstall_service,
)
from sidecar.model import Session, Status
from sidecar.presentation import row_age, row_value
from sidecar.process import running_agent_processes
from sidecar.process_runner import bounded_execution_signal_guard
from sidecar.remote import (
    MAX_RECENT_SECONDS,
    RemoteInventoryError,
    RemoteWatchEvent,
    RemoteWatchFailure,
    RemoteWatchReady,
    watch_remote,
)
from sidecar.release import (
    DEFAULT_RELEASE_PATH,
    ReleaseError,
    build_release_zipapp,
)
from sidecar.runtime_cmd import RuntimeCommandError, resolve_runtime_prefix
from sidecar.scan import ScanError, Scanner
from sidecar.send_audit import (
    AuditError,
    SendAuditStore,
    generate_request_id,
    validate_request_id,
)
from sidecar.tail import watch_sessions
from sidecar.tailer_pool import (
    MAX_TAIL_ERRORS,
    MAX_TAIL_ERROR_AGENT_LENGTH,
    MAX_TAIL_ERROR_CODE_LENGTH,
    MAX_TAIL_ERROR_SESSION_ID_LENGTH,
)
from sidecar.text_utils import normalize_scalar_text, redact_message
from sidecar.tui import arrange_session_tree, run_tui

RECENT_SECONDS = 48.0 * 60.0 * 60.0
DAEMON_START_TIMEOUT = 5.0
DAEMON_STOP_TIMEOUT = 5.0
DAEMON_POLL_INTERVAL = 0.1
DAEMON_CHILD_TERM_TIMEOUT = 2.0
DAEMON_CHILD_KILL_TIMEOUT = 2.0
HTTP_TOKEN_NAME = "http.token"
TAIL_ERROR_DEDUPE_LIMIT = 512
WATCH_HOST_WIDTH = 16
WATCH_PRODUCER_JOIN_SECONDS = 1.0
WATCH_QUEUE_ITEMS = 256
WATCH_QUEUE_POLL_SECONDS = 0.05
_EVENT_FIELDS = ("ts", "agent", "session_id", "kind", "text", "extra")
_SESSION_LINE = (
    "{branch}{agent:<11} {status:<7} {session:<16} {age:>4}"
    "  {updated:<25}  {title}\n"
)
_REMOTE_SESSION_LINE = (
    "{branch}{host:<16} {agent:<11} {status:<7} {session:<16} {age:>4}"
    "  {updated:<25}  {title}\n"
)


@dataclass(frozen=True)
class _WatchQueueEntry:
    kind: str
    value: object = None


class _WatchReadyWriter:
    def __init__(self, stdout: TextIO) -> None:
        self._stdout = stdout
        self.emitted = False

    def __call__(self) -> None:
        if self.emitted:
            return
        self.emitted = True
        self._stdout.write('{"type":"ready"}\n')
        self._stdout.flush()


@dataclass
class _WatchSourceLifecycle:
    name: str
    configured: bool
    started: bool = False
    ready: bool = False
    events: int = 0
    failed: bool = False
    crashed: bool = False
    ended: bool = False
    ready_hosts: set = field(default_factory=set)
    failed_hosts: set = field(default_factory=set)

    @property
    def active(self) -> bool:
        return self.started and not self.ended

    @property
    def delivered(self) -> bool:
        return self.events > 0

    @property
    def terminal_success(self) -> bool:
        if self.name == "local":
            return self.started and self.ready and self.ended and not self.failed
        return bool(
            not self.crashed
            and self.ended
            and self.ready_hosts.difference(self.failed_hosts)
        )


def _recent_seconds_argument(value: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError(
            "recent seconds must be a finite positive number"
        ) from error
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_RECENT_SECONDS:
        raise argparse.ArgumentTypeError(
            "recent seconds must be positive and at most {}".format(
                int(MAX_RECENT_SECONDS)
            )
        )
    return seconds


def _request_id_argument(value: str) -> str:
    try:
        return validate_request_id(value)
    except AuditError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _http_port_argument(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(
            "HTTP port must be an integer from 0 through 65535"
        ) from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            "HTTP port must be an integer from 0 through 65535"
        )
    return port


def _add_http_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--http",
        action="store_true",
        help="enable the private read-only loopback HTTP server",
    )
    parser.add_argument(
        "--http-port",
        type=_http_port_argument,
        default=None,
        metavar="PORT",
        help="loopback HTTP port (default: an ephemeral port; requires --http)",
    )


class _PendingTailArgumentParser(argparse.ArgumentParser):
    """Backport CPython 3.13's zero-width trailing-positional fix for send.

    Python 3.9's greedy partial matcher consumes an optional trailing
    positional with zero strings in an early chunk, which would break
    ``send <prefix> --allow-write -- <hyphen-message>`` once the message
    positional became optional. CPython 3.13's ``_match_arguments_partial``
    fixed this upstream by dropping a zero-width tail only when the match is
    followed by an option string, so a later chunk (for example after ``--``)
    can still fill it while end-of-input keeps upstream semantics: a trailing
    ``nargs='?'`` positional keeps its parser default and a trailing
    ``nargs='*'`` positional is still assigned its empty match. This subclass
    mirrors that exact condition (it is a no-op on Python 3.13 and newer) and
    is applied to the ``send`` subparser only.
    """

    def _match_arguments_partial(self, actions, arg_strings_pattern):
        counts = super()._match_arguments_partial(
            actions,
            arg_strings_pattern,
        )
        consumed = sum(counts)
        if (
            consumed < len(arg_strings_pattern)
            and arg_strings_pattern[consumed] == "O"
        ):
            while counts and counts[-1] == 0:
                counts.pop()
        return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-sidecar",
        description=(
            "Observation commands are read-only. send starts a local headless "
            "resume and may modify agent state."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s {}".format(sidecar.__version__),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    list_parser = commands.add_parser("list", help="list discovered sessions")
    recency = list_parser.add_mutually_exclusive_group()
    recency.add_argument(
        "--all",
        action="store_true",
        help="include sessions older than 48h",
    )
    recency.add_argument(
        "--recent-seconds",
        type=_recent_seconds_argument,
        default=None,
        help=argparse.SUPPRESS,
    )
    list_parser.add_argument("--json", action="store_true", help="emit a JSON array")
    list_parser.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="NAME",
        help="include exact agent name (repeatable, case-insensitive)",
    )
    list_parser.add_argument(
        "--remote",
        action="store_true",
        help="include eligible remote hosts",
    )
    list_parser.add_argument(
        "--host",
        action="append",
        default=[],
        metavar="ALIAS",
        help="select an eligible remote host (repeatable; requires --remote)",
    )

    ps_parser = commands.add_parser("ps", help="list running agent processes")
    ps_parser.add_argument("--json", action="store_true", help="emit a JSON array")

    status_parser = commands.add_parser("status", help="show working and waiting sessions")
    status_parser.add_argument("--json", action="store_true", help="emit a JSON array")
    status_parser.add_argument(
        "--remote",
        action="store_true",
        help="include eligible remote hosts",
    )
    status_parser.add_argument(
        "--host",
        action="append",
        default=[],
        metavar="ALIAS",
        help="select an eligible remote host (repeatable; requires --remote)",
    )

    watch_parser = commands.add_parser("watch", help="follow normalized session events")
    watch_parser.add_argument("session_prefix", nargs="?", help="unique session ID prefix")
    watch_parser.add_argument("--all", action="store_true", help="follow every discovered session")
    watch_parser.add_argument(
        "--from-start",
        action="store_true",
        help="replay existing records before following",
    )
    watch_parser.add_argument("--json", action="store_true", help="emit one JSON event per line")
    watch_parser.add_argument(
        "--stream-ready",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    watch_parser.add_argument(
        "--remote",
        action="store_true",
        help="also follow eligible remote hosts (requires --all)",
    )
    watch_parser.add_argument(
        "--host",
        action="append",
        default=[],
        metavar="ALIAS",
        help="select an eligible remote host (repeatable; requires --remote)",
    )

    send_parser = commands.add_parser(
        "send",
        help="start a local headless resume that may modify agent state",
        description="Starts a local headless resume and may modify agent state.",
        allow_abbrev=False,
    )
    # Scope the Python 3.9 partial-match backport to the send subparser, the
    # only parser with an optional trailing positional that must remain
    # fillable after ``--``. add_parser offers no per-command parser class,
    # so retype the freshly built instance (the subclass adds no state).
    send_parser.__class__ = _PendingTailArgumentParser
    send_parser.add_argument(
        "session_prefix",
        metavar="session-prefix",
        help="exact session ID or unique prefix",
    )
    send_parser.add_argument(
        "message",
        nargs="?",
        default=None,
        help="message delivered to the resumed agent",
    )
    send_parser.add_argument(
        "--agent",
        default=None,
        metavar="NAME",
        help="limit resolution to an exact agent name (case-insensitive)",
    )
    send_parser.add_argument(
        "--exact-session",
        action="store_true",
        help="require the full session ID; never fall back to prefix matching",
    )
    send_parser.add_argument(
        "--message-stdin",
        action="store_true",
        help=(
            "read the message from piped or redirected standard input "
            "instead of a positional argument, keeping it out of the "
            "agent-sidecar command line; interactive terminals are refused"
        ),
    )
    send_parser.add_argument(
        "--allow-write",
        action="store_true",
        help="explicitly permit the resume to modify agent state",
    )
    send_parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_SEND_TIMEOUT_SECONDS,
        metavar="SEC",
        help="native resume timeout in seconds (default: 300)",
    )
    send_parser.add_argument(
        "--request-id",
        type=_request_id_argument,
        default=None,
        metavar="ID",
        help="opaque idempotency key (ASCII, at most 128 bytes)",
    )
    send_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the bounded delivery result as JSON",
    )

    audit_parser = commands.add_parser(
        "audit",
        help="perform explicit send-audit maintenance",
    )
    audit_commands = audit_parser.add_subparsers(
        dest="audit_command",
        required=True,
    )
    audit_reset_parser = audit_commands.add_parser(
        "reset",
        help="clear send-audit idempotency history",
        allow_abbrev=False,
    )
    audit_reset_parser.add_argument(
        "--allow-write",
        action="store_true",
        help="explicitly permit deletion of send-audit state",
    )
    audit_reset_parser.add_argument(
        "--confirm",
        default=None,
        metavar="TEXT",
        help="must be exactly CLEAR-SEND-AUDIT",
    )

    package_parser = commands.add_parser(
        "package",
        help="build release artifacts",
    )
    package_commands = package_parser.add_subparsers(
        dest="package_command",
        required=True,
    )
    package_build_parser = package_commands.add_parser(
        "build",
        help="build a deterministic executable zipapp",
        allow_abbrev=False,
    )
    package_build_parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_RELEASE_PATH,
        metavar="PATH",
        help="artifact path (default: dist/agent-sidecar.pyz)",
    )

    daemon_parser = commands.add_parser("daemon", help="manage the local sidecar daemon")
    daemon_commands = daemon_parser.add_subparsers(dest="daemon_command", required=True)
    daemon_start_parser = daemon_commands.add_parser(
        "start",
        help="start the daemon in the background",
        allow_abbrev=False,
    )
    _add_http_arguments(daemon_start_parser)
    daemon_commands.add_parser("stop", help="stop the verified running daemon")
    daemon_commands.add_parser("status", help="report whether the daemon is running")
    daemon_run_parser = daemon_commands.add_parser(
        "run",
        help=argparse.SUPPRESS,
        allow_abbrev=False,
    )
    _add_http_arguments(daemon_run_parser)

    service_parser = commands.add_parser(
        "service",
        help="manage the explicit macOS user LaunchAgent",
    )
    service_commands = service_parser.add_subparsers(
        dest="service_command",
        required=True,
    )
    service_install_parser = service_commands.add_parser(
        "install",
        help="install and start the user LaunchAgent",
        allow_abbrev=False,
    )
    _add_http_arguments(service_install_parser)
    service_install_parser.add_argument(
        "--force",
        action="store_true",
        help="replace a different validated service definition",
    )
    service_commands.add_parser(
        "uninstall",
        help="stop and remove the validated user LaunchAgent",
        allow_abbrev=False,
    )
    service_commands.add_parser(
        "status",
        help="show combined LaunchAgent and daemon health",
        allow_abbrev=False,
    )

    tui_parser = commands.add_parser("tui", help="show the live terminal dashboard")
    tui_parser.add_argument(
        "--once",
        action="store_true",
        help="render one snapshot without terminal control sequences",
    )
    return parser


def _write_json(value: object, stream: TextIO) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    stream.write("\n")
    stream.flush()


def _local_time(epoch: float) -> str:
    try:
        return dt.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _scan_error_text(error: object) -> str:
    if isinstance(error, ScanError):
        location = " session={}".format(error.session_id) if error.session_id else ""
        return "{} {}{}: {}".format(error.adapter, error.stage, location, error.message)
    if isinstance(error, Mapping):
        session_id = error.get("session_id")
        location = " session={}".format(session_id) if session_id else ""
        return "{} {}{}: {}".format(
            error.get("adapter", ""),
            error.get("stage", ""),
            location,
            error.get("message", ""),
        )
    return str(error)


def _report_scan_errors(scan_errors: Iterable[object], stderr: TextIO) -> None:
    for error in scan_errors:
        stderr.write(
            "scan error: {}\n".format(
                sanitize_terminal_text(_scan_error_text(error))
            )
        )
    stderr.flush()


def _report_scanner_errors(scanner: object, stderr: TextIO) -> None:
    _report_scan_errors(getattr(scanner, "errors", ()) or (), stderr)


def _bounded_terminal_field(value: object, limit: int) -> str:
    text = normalize_scalar_text(
        str(value or ""),
        errors="replace",
    )
    return sanitize_terminal_text(text)[:limit] or "unknown"


class _BoundedTailErrorDedupe:
    def __init__(self) -> None:
        self._keys = deque()
        self._seen = set()

    def __contains__(self, key: object) -> bool:
        return key in self._seen

    def add(self, key: object) -> None:
        if key in self._seen:
            return
        if len(self._keys) >= TAIL_ERROR_DEDUPE_LIMIT:
            self._seen.discard(self._keys.popleft())
        self._keys.append(key)
        self._seen.add(key)


def _report_tail_errors(
    tail_errors: Iterable[object],
    stderr: TextIO,
    reported: Optional[_BoundedTailErrorDedupe] = None,
) -> None:
    seen = _BoundedTailErrorDedupe() if reported is None else reported
    wrote = False
    for index, error in enumerate(tail_errors):
        if index >= MAX_TAIL_ERRORS:
            break
        if not isinstance(error, Mapping):
            continue
        key = (
            _bounded_terminal_field(
                error.get("agent"),
                MAX_TAIL_ERROR_AGENT_LENGTH,
            ),
            _bounded_terminal_field(
                error.get("session_id"),
                MAX_TAIL_ERROR_SESSION_ID_LENGTH,
            ),
            _bounded_terminal_field(
                error.get("code"),
                MAX_TAIL_ERROR_CODE_LENGTH,
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        stderr.write(
            "tail error: {} session={} code={}\n".format(*key)
        )
        wrote = True
    if wrote:
        stderr.flush()


def _scan_sessions(
    scanner: Scanner,
    stderr: TextIO,
    recent_seconds: Optional[float],
) -> List[Session]:
    try:
        sessions = list(scanner.scan(recent_seconds=recent_seconds))
    except Exception as error:
        stderr.write(
            "scan error: {}: {}\n".format(
                error.__class__.__name__,
                sanitize_terminal_text(error),
            )
        )
        stderr.flush()
        return []
    _report_scanner_errors(scanner, stderr)
    return sessions


def _status_value(row: object) -> str:
    value = row_value(row, "status")
    return value.value if isinstance(value, Status) else str(value or "")


def _session_title(session: object) -> str:
    return str(
        row_value(session, "title")
        or row_value(session, "project")
        or "(untitled)"
    )


def _as_dict(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    converter = getattr(value, "to_dict", None)
    if callable(converter):
        converted = converter()
        if isinstance(converted, Mapping):
            return dict(converted)
    raise TypeError("value cannot be represented as a mapping")


def _updated_at_value(session: object) -> float:
    try:
        return float(row_value(session, "updated_at", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _snapshot_sort_key(command: str, session: object) -> tuple:
    status_priority = {
        Status.WORKING.value: 0,
        Status.WAITING.value: 1,
    }
    priority = (
        (status_priority.get(_status_value(session), 2),)
        if command == "status"
        else ()
    )
    host = str(row_value(session, "host") or "")
    agent = str(row_value(session, "agent") or "")
    return (
        *priority,
        -_updated_at_value(session),
        host.casefold(),
        host,
        agent.casefold(),
        agent,
        str(row_value(session, "session_id") or ""),
    )


def _display_title(session: object, depth: int) -> str:
    title = _session_title(session)
    extra = row_value(session, "extra", {})
    if depth == 0 and isinstance(extra, Mapping) and extra.get("sidechain") is True:
        return "[sidechain] {}".format(title)
    return title


def _arrange_snapshot_rows(
    command: str,
    sessions: Iterable[object],
) -> List[tuple]:
    values = list(sessions)
    if command == "list":
        return arrange_session_tree(
            values,
            sort_key=lambda row: _snapshot_sort_key(command, row),
        )

    ordered = []
    for status in (Status.WORKING.value, Status.WAITING.value):
        ordered.extend(
            arrange_session_tree(
                (row for row in values if _status_value(row) == status),
                sort_key=lambda row: _snapshot_sort_key(command, row),
            )
        )
    return ordered


def _print_sessions(
    sessions: Iterable[tuple],
    stdout: TextIO,
    *,
    show_host: bool = False,
) -> None:
    line_format = _REMOTE_SESSION_LINE if show_host else _SESSION_LINE
    stdout.write(
        line_format.format(
            branch="",
            host="HOST",
            agent="AGENT",
            status="STATUS",
            session="SESSION",
            age="AGE",
            updated="UPDATED",
            title="TITLE",
        )
    )
    for session, depth in sessions:
        branch = "{}↳ ".format("  " * depth) if depth else ""
        stdout.write(
            line_format.format(
                branch=branch,
                host=sanitize_terminal_text(row_value(session, "host") or "local"),
                agent=sanitize_terminal_text(row_value(session, "agent")),
                status=sanitize_terminal_text(_status_value(session)),
                session=sanitize_terminal_text(
                    row_value(session, "session_id")
                )[:16],
                age=sanitize_terminal_text(row_age(session)),
                updated=_local_time(
                    float(row_value(session, "updated_at", 0.0))
                ),
                title=sanitize_terminal_text(_display_title(session, depth)),
            )
        )
    stdout.flush()


def _print_processes(processes: Iterable[object], stdout: TextIO) -> None:
    for process in processes:
        if not isinstance(process, dict):
            continue
        stdout.write(
            "{pid:>7} {etime:<12} {exe:<13} {cwd} {cmd}\n".format(
                pid=sanitize_terminal_text(process.get("pid", "")),
                etime=sanitize_terminal_text(process.get("etime", "")),
                exe=sanitize_terminal_text(process.get("exe", "")),
                cwd=sanitize_terminal_text(process.get("cwd", "")),
                cmd=sanitize_terminal_text(process.get("cmd", "")),
            )
        )
    stdout.flush()


def resolve_session_prefix(sessions: Sequence[Session], prefix: str) -> Session:
    """Resolve a unique ID prefix, preferring a sole exact ID match."""

    exact = [session for session in sessions if session.session_id == prefix]
    matches = exact or [session for session in sessions if session.session_id.startswith(prefix)]
    if not matches:
        raise LookupError("no session matches prefix {!r}".format(prefix))
    if len(matches) != 1:
        candidates = ", ".join(
            "{}:{}".format(session.agent, session.session_id) for session in matches[:8]
        )
        if len(matches) > 8:
            candidates += ", …"
        raise ValueError(
            "ambiguous session prefix {!r} ({} matches): {}".format(
                prefix,
                len(matches),
                candidates,
            )
        )
    return matches[0]


def _resolve_exact_session(
    sessions: Sequence[Session],
    session_id: str,
) -> Session:
    """Resolve exactly one full session ID without prefix fallback."""

    matches = [
        session for session in sessions if session.session_id == session_id
    ]
    if not matches:
        raise LookupError(
            "no session exactly matches ID {!r}".format(session_id)
        )
    if len(matches) != 1:
        raise ValueError(
            "ambiguous exact session ID {!r} ({} matches)".format(
                session_id,
                len(matches),
            )
        )
    return matches[0]


def _supports_direct_watch(session: object) -> bool:
    transcript = str(row_value(session, "transcript") or "")
    if not transcript:
        return False
    suffix = Path(transcript).suffix.lower()
    extra = row_value(session, "extra", {})
    if (
        isinstance(extra, Mapping)
        and extra.get("transcript_kind") == "cursor-chat-sqlite"
    ):
        return suffix in (".db", ".sqlite", ".sqlite3")
    if str(row_value(session, "agent")) == "dsh":
        return True
    return suffix == ".jsonl"


def _print_event(event: object, stdout: TextIO) -> None:
    stdout.write(
        "{ts} {agent:<11} {session:<12} {kind:<12} {text}\n".format(
            ts=sanitize_terminal_text(row_value(event, "ts")),
            agent=sanitize_terminal_text(row_value(event, "agent")),
            session=sanitize_terminal_text(row_value(event, "session_id"))[:12],
            kind=sanitize_terminal_text(row_value(event, "kind")),
            text=sanitize_terminal_text(row_value(event, "text")),
        )
    )
    stdout.flush()


def _client_status(
    client: SidecarClient,
    stderr: TextIO,
    reported_tail_errors: Optional[_BoundedTailErrorDedupe] = None,
) -> Optional[List[Mapping[str, Any]]]:
    try:
        rows = [dict(row) for row in client.status()]
    except (SidecarClientError, OSError):
        return None
    _report_scan_errors(getattr(client, "scan_errors", ()) or (), stderr)
    _report_tail_errors(
        getattr(client, "tail_errors", ()) or (),
        stderr,
        reported_tail_errors,
    )
    return rows


class _TailReportingClient:
    def __init__(self, client: object, stderr: TextIO) -> None:
        self._client = client
        self._stderr = stderr
        self._reported_tail_errors = _BoundedTailErrorDedupe()

    def status(self):
        rows = self._client.status()
        _report_tail_errors(
            getattr(self._client, "tail_errors", ()) or (),
            self._stderr,
            self._reported_tail_errors,
        )
        return rows

    def __getattr__(self, name: str):
        return getattr(self._client, name)


def _recent_rows(
    rows: Iterable[object],
    seconds: Optional[float],
    now: Optional[float] = None,
) -> List[object]:
    if seconds is None:
        return list(rows)
    current = time.time() if now is None else now
    recent: List[object] = []
    for row in rows:
        try:
            updated_at = float(row_value(row, "updated_at", 0.0))
        except (TypeError, ValueError):
            continue
        if max(0.0, current - updated_at) <= seconds:
            recent.append(row)
    return recent


def _filter_agent_rows(
    rows: Iterable[object],
    agent_names: Sequence[str],
) -> List[object]:
    if not agent_names:
        return list(rows)
    selected = {str(name).casefold() for name in agent_names}
    return [
        row
        for row in rows
        if str(row_value(row, "agent") or "").casefold() in selected
    ]


def _snapshot_recent_seconds(
    command: str,
    args: argparse.Namespace,
) -> Optional[float]:
    if command != "list":
        return None
    if args.recent_seconds is not None:
        return args.recent_seconds
    return None if args.all else RECENT_SECONDS


def _select_snapshot_rows(
    command: str,
    args: argparse.Namespace,
    rows: Iterable[object],
) -> List[object]:
    selected = _recent_rows(
        rows,
        _snapshot_recent_seconds(command, args),
    )
    if command == "list":
        return _filter_agent_rows(selected, args.agent)
    return [
        row
        for row in selected
        if _status_value(row)
        in (Status.WORKING.value, Status.WAITING.value)
    ]


def _render_snapshot(
    command: str,
    args: argparse.Namespace,
    rows: Iterable[object],
    *,
    stdout: TextIO,
    show_host: bool,
) -> None:
    selected = _select_snapshot_rows(command, args, rows)
    if args.json:
        ordered = sorted(
            selected,
            key=lambda row: _snapshot_sort_key(command, row),
        )
        _write_json([dict(_as_dict(row)) for row in ordered], stdout)
        return
    if command == "status" and not selected:
        stdout.write("no active sessions\n")
        stdout.flush()
        return
    _print_sessions(
        _arrange_snapshot_rows(command, selected),
        stdout,
        show_host=show_host,
    )


def _acquire_local_snapshot(
    command: str,
    args: argparse.Namespace,
    *,
    scanner: Optional[Scanner],
    client: SidecarClient,
    stderr: TextIO,
) -> List[object]:
    daemon_rows = _client_status(client, stderr)
    if daemon_rows is not None:
        return list(daemon_rows)
    active_scanner = Scanner() if scanner is None else scanner
    return list(
        _scan_sessions(
            active_scanner,
            stderr,
            _snapshot_recent_seconds(command, args),
        )
    )


def _aggregate_value(result: object, name: str, default: object) -> object:
    if isinstance(result, Mapping):
        return result.get(name, default)
    return getattr(result, name, default)


def _report_remote_failures(failures: Iterable[object], stderr: TextIO) -> None:
    values = sorted(
        failures,
        key=lambda failure: str(
            _aggregate_value(failure, "host", "")
        ).casefold(),
    )
    for failure in values:
        host = sanitize_terminal_text(_aggregate_value(failure, "host", ""))
        code = sanitize_terminal_text(_aggregate_value(failure, "code", "remote"))
        stderr.write("remote {}: {}\n".format(host, code))
    stderr.flush()


def _report_empty_remote_fleet(stderr: TextIO) -> None:
    stderr.write("remote: no eligible hosts; showing local sessions only\n")
    stderr.flush()


def _accepts_keyword_argument(provider: object, name: str) -> bool:
    try:
        parameters = inspect.signature(provider).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == name
            and parameter.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        )
        for parameter in parameters
    )


def _accepts_recent_seconds(provider: object) -> bool:
    return _accepts_keyword_argument(provider, "recent_seconds")


def _run_remote_snapshot(
    command: str,
    args: argparse.Namespace,
    *,
    scanner: Optional[Scanner],
    client: SidecarClient,
    stdout: TextIO,
    stderr: TextIO,
    remote_aggregator=None,
) -> int:
    from sidecar.remote import (
        EXIT_NO_SUCCESS,
        RemoteInventoryError,
        aggregate_remote,
    )

    provider = aggregate_remote if remote_aggregator is None else remote_aggregator
    local_values = _acquire_local_snapshot(
        command,
        args,
        scanner=scanner,
        client=client,
        stderr=stderr,
    )
    local_rows: List[Mapping[str, Any]] = []
    for session in local_values:
        row = dict(_as_dict(session))
        row["host"] = "local"
        local_rows.append(row)

    result: Optional[object] = None
    control_error: Optional[str] = None
    try:
        provider_arguments = {"selected": args.host or None}
        if command == "list" and _accepts_recent_seconds(provider):
            provider_arguments["recent_seconds"] = _snapshot_recent_seconds(
                command,
                args,
            )
        result = provider(command, **provider_arguments)
    except RemoteInventoryError:
        control_error = "remote: inventory\n"
    except OSError:
        control_error = "remote: setup\n"
    except ValueError as error:
        control_error = "remote: {}\n".format(sanitize_terminal_text(error))

    remote_rows: List[Mapping[str, Any]] = []
    failure_values = ()
    if result is not None:
        failures = _aggregate_value(result, "failures", ())
        failure_values = tuple(
            failures
            if isinstance(failures, Iterable)
            and not isinstance(failures, (str, bytes, Mapping))
            else ()
        )
        _report_remote_failures(
            failure_values,
            stderr,
        )
        remote_rows_value = _aggregate_value(result, "rows", ())
        remote_row_values = (
            remote_rows_value
            if isinstance(remote_rows_value, Iterable)
            and not isinstance(remote_rows_value, (str, bytes, Mapping))
            else ()
        )
        remote_rows = [
            dict(_as_dict(row))
            for row in remote_row_values
        ]

    try:
        result_exit_code = int(
            _aggregate_value(result, "exit_code", EXIT_NO_SUCCESS)
        )
    except (TypeError, ValueError):
        result_exit_code = EXIT_NO_SUCCESS
    if result_exit_code == 0 and not remote_rows and not failure_values:
        hosts = _aggregate_value(result, "hosts", ())
        succeeded = _aggregate_value(result, "succeeded", ())
        if not hosts and not succeeded:
            _report_empty_remote_fleet(stderr)

    if control_error is not None:
        stderr.write(control_error)
        stderr.flush()

    _render_snapshot(
        command,
        args,
        list(local_rows) + remote_rows,
        stdout=stdout,
        show_host=True,
    )

    if control_error is not None:
        return 2
    return 0 if result_exit_code == 0 else EXIT_NO_SUCCESS


def _client_ping_info(client: SidecarClient) -> PingInfo:
    provider = getattr(client, "ping_info", None)
    if callable(provider):
        value = provider()
        if isinstance(value, PingInfo):
            return value
        if isinstance(value, Mapping):
            return PingInfo.from_response(value)
        raise SidecarClientError(
            "daemon returned an invalid ping",
            code="invalid_response",
        )
    return PingInfo.from_response(client.ping())


def _ping_pid(client: SidecarClient) -> int:
    return _client_ping_info(client).pid


def _runtime_dir_for(client: object) -> Path:
    socket_path = getattr(client, "socket_path", None)
    if socket_path is not None:
        return Path(socket_path).expanduser().parent
    return default_runtime_dir()


def _report_http_info(
    info: PingInfo,
    client: object,
    stdout: TextIO,
) -> None:
    if not info.http.enabled or info.http.port is None:
        return
    url = "http://127.0.0.1:{}".format(info.http.port)
    token_path = _runtime_dir_for(client) / HTTP_TOKEN_NAME
    stdout.write("HTTP URL: {}\n".format(sanitize_terminal_text(url)))
    stdout.write(
        "HTTP token file: {}\n".format(
            sanitize_terminal_text(str(token_path))
        )
    )


def _http_request_matches(info: PingInfo, requested_port: int) -> bool:
    if (
        not info.http.enabled
        or info.http.host != "127.0.0.1"
        or info.http.port is None
    ):
        return False
    return requested_port == 0 or info.http.port == requested_port


@dataclass(frozen=True)
class _OwnedDaemonPaths:
    runtime_dir: Path
    pid: int
    pid_identity: tuple
    socket_path: Path
    socket_identity: Optional[tuple]


def _path_identity(path: Path, expected_type: int) -> Optional[tuple]:
    try:
        details = path.lstat()
    except OSError:
        return None
    if stat.S_IFMT(details.st_mode) != expected_type:
        return None
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _capture_owned_daemon_paths(
    client: object,
    owned_pids: Iterable[int],
) -> Optional[_OwnedDaemonPaths]:
    owned = set(owned_pids)
    runtime_dir = _runtime_dir_for(client)
    pid = read_pid(runtime_dir)
    if pid not in owned:
        return None
    pid_path = runtime_dir / PIDFILE_NAME
    pid_identity = _path_identity(pid_path, stat.S_IFREG)
    if pid_identity is None:
        return None
    configured_socket = getattr(client, "socket_path", None)
    socket_path = (
        runtime_dir / SOCKET_NAME
        if configured_socket is None
        else Path(configured_socket).expanduser()
    )
    return _OwnedDaemonPaths(
        runtime_dir=runtime_dir,
        pid=pid,
        pid_identity=pid_identity,
        socket_path=socket_path,
        socket_identity=_path_identity(socket_path, stat.S_IFSOCK),
    )


def _cleanup_owned_daemon_paths(paths: Optional[_OwnedDaemonPaths]) -> None:
    if paths is None:
        return
    pid_path = paths.runtime_dir / PIDFILE_NAME
    if (
        _path_identity(pid_path, stat.S_IFREG) != paths.pid_identity
        or read_pid(paths.runtime_dir) != paths.pid
    ):
        return
    current_socket_identity = _path_identity(paths.socket_path, stat.S_IFSOCK)
    if current_socket_identity not in (None, paths.socket_identity):
        return
    if (
        paths.socket_identity is not None
        and current_socket_identity == paths.socket_identity
    ):
        if _socket_is_live(paths.socket_path):
            return
        try:
            paths.socket_path.unlink()
        except OSError:
            pass
    if _path_identity(pid_path, stat.S_IFREG) == paths.pid_identity:
        _cleanup_verified_pidfile(paths.runtime_dir, paths.pid)


def _owned_daemon_paths_are_gone(paths: _OwnedDaemonPaths) -> bool:
    pid_path = paths.runtime_dir / PIDFILE_NAME
    socket_is_gone = (
        paths.socket_identity is None
        or _path_identity(paths.socket_path, stat.S_IFSOCK) != paths.socket_identity
    )
    return (
        _path_identity(pid_path, stat.S_IFREG) != paths.pid_identity
        and socket_is_gone
    )


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError):
        return True
    return True


def _cleanup_verified_pidfile(runtime_dir: Path, pid: int) -> None:
    if read_pid(runtime_dir) != pid:
        return
    path = runtime_dir / PIDFILE_NAME
    try:
        path.unlink()
    except OSError:
        pass


def _ping_belongs_to_child(process: Any, info: PingInfo) -> bool:
    if info.pid == process.pid:
        return getattr(process, "returncode", None) is None
    try:
        return os.getpgid(info.pid) == process.pid
    except (OSError, ValueError):
        return False


def _child_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_for_child_group(
    process: Any,
    process_group: int,
    timeout: float,
) -> bool:
    clock = time.monotonic
    try:
        deadline = clock() + timeout
    except Exception:
        clock = time.time
        deadline = clock() + timeout
    while True:
        try:
            process.poll()
        except (Exception, KeyboardInterrupt):
            pass
        if not _child_group_exists(process_group):
            try:
                process.wait(timeout=0)
            except (Exception, KeyboardInterrupt):
                pass
            return True
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        try:
            time.sleep(min(DAEMON_POLL_INTERVAL, remaining))
        except KeyboardInterrupt:
            continue


def _terminate_and_reap_daemon_child(process: Any) -> None:
    try:
        return_code = process.poll()
    except (Exception, KeyboardInterrupt):
        return_code = getattr(process, "returncode", None)
    if return_code is not None:
        try:
            process.wait(timeout=0)
        except (Exception, KeyboardInterrupt):
            pass
        return

    process_group = process.pid
    try:
        if os.getpgid(process.pid) != process_group:
            return
    except (OSError, ValueError):
        try:
            process.wait(timeout=0)
        except (Exception, KeyboardInterrupt):
            pass
        return

    try:
        os.killpg(process_group, signal.SIGTERM)
    except OSError:
        pass
    if not _wait_for_child_group(
        process,
        process_group,
        DAEMON_CHILD_TERM_TIMEOUT,
    ):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except OSError:
            pass
        _wait_for_child_group(
            process,
            process_group,
            DAEMON_CHILD_KILL_TIMEOUT,
        )
    try:
        process.wait(timeout=0)
    except (Exception, KeyboardInterrupt):
        pass


def _write_daemon_started(
    info: PingInfo,
    client: object,
    stdout: TextIO,
    *,
    already_running: bool = False,
) -> None:
    state = "already running" if already_running else "started"
    stdout.write("daemon {} (pid {})\n".format(state, info.pid))
    _report_http_info(info, client, stdout)
    stdout.flush()


def _daemon_start(
    client: SidecarClient,
    stdout: TextIO,
    stderr: TextIO,
    *,
    http: bool = False,
    http_port: Optional[int] = None,
) -> int:
    requested_http_port = 0 if http_port is None else http_port
    try:
        info = _client_ping_info(client)
    except (SidecarClientError, OSError):
        info = None
    if info is not None:
        if http and not _http_request_matches(info, requested_http_port):
            stderr.write(
                "daemon start: running daemon HTTP configuration "
                "does not match the request\n"
            )
            stderr.flush()
            return 1
        _write_daemon_started(
            info,
            client,
            stdout,
            already_running=True,
        )
        return 0

    try:
        command = list(resolve_runtime_prefix()) + ["daemon", "run"]
    except RuntimeCommandError:
        stderr.write("daemon start: cannot resolve the runtime command\n")
        stderr.flush()
        return 2
    if http:
        command.append("--http")
        if http_port is not None:
            command.extend(("--http-port", str(http_port)))
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except OSError as error:
        stderr.write("daemon start: {}\n".format(sanitize_terminal_text(error)))
        stderr.flush()
        return 1

    failure = "daemon did not become ready"
    interrupted = False
    try:
        deadline = time.monotonic() + DAEMON_START_TIMEOUT
        while time.monotonic() < deadline:
            try:
                info = _client_ping_info(client)
            except (SidecarClientError, OSError):
                try:
                    return_code = process.poll()
                except Exception as error:
                    failure = "child polling failed: {}".format(
                        sanitize_terminal_text(error)
                    )
                    break
                if return_code is not None:
                    failure = "daemon child exited before readiness"
                    break
                time.sleep(DAEMON_POLL_INTERVAL)
                continue
            if not _ping_belongs_to_child(process, info):
                failure = "another daemon became ready during startup"
                break
            if http and not _http_request_matches(info, requested_http_port):
                failure = (
                    "daemon became ready without the requested HTTP listener"
                )
                break
            _write_daemon_started(info, client, stdout)
            return 0
    except KeyboardInterrupt:
        interrupted = True
        failure = "startup interrupted"
    except Exception as error:
        failure = "readiness polling failed: {}".format(
            sanitize_terminal_text(error)
        )

    try:
        final_info = _client_ping_info(client)
    except KeyboardInterrupt:
        interrupted = True
        final_info = None
    except Exception:
        final_info = None
    try:
        final_owned = bool(
            final_info is not None
            and _ping_belongs_to_child(process, final_info)
        )
    except KeyboardInterrupt:
        interrupted = True
        final_owned = False
    final_matches = bool(
        final_info is not None
        and (
            not http
            or _http_request_matches(final_info, requested_http_port)
        )
    )
    if not interrupted and final_owned and final_matches:
        _write_daemon_started(final_info, client, stdout)
        return 0

    owned_pids = [process.pid]
    if final_owned and final_info is not None:
        owned_pids.append(final_info.pid)
    try:
        owned_paths = _capture_owned_daemon_paths(client, owned_pids)
    except KeyboardInterrupt:
        interrupted = True
        owned_paths = None
    except Exception:
        owned_paths = None
    _terminate_and_reap_daemon_child(process)
    _cleanup_owned_daemon_paths(owned_paths)

    if interrupted:
        return 130
    if final_info is not None and not final_owned and final_matches:
        _write_daemon_started(
            final_info,
            client,
            stdout,
            already_running=True,
        )
        return 0
    if final_info is not None and http and not final_matches:
        failure = "running daemon HTTP configuration does not match the request"
    stderr.write("daemon start: {}\n".format(failure))
    stderr.flush()
    return 1


def _daemon_stop(client: SidecarClient, stdout: TextIO, stderr: TextIO) -> int:
    runtime_dir = _runtime_dir_for(client)
    try:
        socket_pid = _ping_pid(client)
    except (SidecarClientError, OSError):
        stdout.write("daemon is not running\n")
        stdout.flush()
        return 1

    pidfile_pid = read_pid(runtime_dir)
    if pidfile_pid != socket_pid:
        stderr.write(
            "daemon stop: refusing to signal pid {}; pidfile contains {}\n".format(
                socket_pid,
                "no valid pid" if pidfile_pid is None else pidfile_pid,
            )
        )
        stderr.flush()
        return 2

    owned_paths = _capture_owned_daemon_paths(client, (socket_pid,))
    try:
        os.kill(socket_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except (OSError, ValueError) as error:
        stderr.write(
            "daemon stop: cannot signal verified pid {}: {}\n".format(
                socket_pid,
                sanitize_terminal_text(error),
            )
        )
        stderr.flush()
        return 1

    deadline = time.monotonic() + DAEMON_STOP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            current_pid = _ping_pid(client)
        except (SidecarClientError, OSError):
            if owned_paths is None:
                _cleanup_verified_pidfile(runtime_dir, socket_pid)
                stopped = True
            else:
                _cleanup_owned_daemon_paths(owned_paths)
                stopped = (
                    _owned_daemon_paths_are_gone(owned_paths)
                    and not _pid_is_alive(socket_pid)
                )
            if stopped:
                stdout.write("daemon stopped\n")
                stdout.flush()
                return 0
            time.sleep(DAEMON_POLL_INTERVAL)
            continue
        if current_pid != socket_pid:
            if owned_paths is None:
                _cleanup_verified_pidfile(runtime_dir, socket_pid)
            else:
                _cleanup_owned_daemon_paths(owned_paths)
            stdout.write(
                "daemon stopped; another daemon is running (pid {})\n".format(current_pid)
            )
            stdout.flush()
            return 0
        time.sleep(DAEMON_POLL_INTERVAL)

    stderr.write("daemon stop: timed out waiting for pid {}\n".format(socket_pid))
    stderr.flush()
    return 1


def _daemon_status(client: SidecarClient, stdout: TextIO) -> int:
    try:
        info = _client_ping_info(client)
    except (SidecarClientError, OSError):
        stdout.write("daemon is not running\n")
        stdout.flush()
        return 1
    stdout.write("daemon is running (pid {})\n".format(info.pid))
    _report_http_info(info, client, stdout)
    stdout.flush()
    return 0


def _daemon_run(stderr: TextIO, http_port: Optional[int] = None) -> int:
    stop_event = threading.Event()
    received_signal: List[int] = []
    previous = {}

    def request_stop(signum: int, frame: object) -> None:
        del frame
        received_signal.append(signum)
        stop_event.set()

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        if http_port is None:
            run_foreground(stop_event=stop_event)
        else:
            run_foreground(stop_event=stop_event, http_port=http_port)
    except KeyboardInterrupt:
        stop_event.set()
        return 130
    except (DaemonAlreadyRunning, DaemonError, OSError) as error:
        stderr.write("daemon run: {}\n".format(sanitize_terminal_text(error)))
        stderr.flush()
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 128 + received_signal[-1] if received_signal else 0


def _write_event(event: object, as_json: bool, stdout: TextIO) -> None:
    if as_json:
        _write_json(_as_dict(event), stdout)
    else:
        _print_event(event, stdout)


def _remote_event_payload(event: object, host: str) -> Mapping[str, Any]:
    value = _as_dict(event)
    payload = {field: value[field] for field in _EVENT_FIELDS}
    payload["host"] = host
    return payload


def _write_remote_event(
    event: object,
    host: str,
    *,
    as_json: bool,
    stdout: TextIO,
) -> None:
    payload = _remote_event_payload(event, host)
    if as_json:
        _write_json(payload, stdout)
        return
    bounded_host = sanitize_terminal_text(host)[:WATCH_HOST_WIDTH] or "unknown"
    stdout.write("{:<{width}} ".format(bounded_host, width=WATCH_HOST_WIDTH))
    _print_event(payload, stdout)


def _report_remote_watch_failure(failure: object, stderr: TextIO) -> None:
    host = _bounded_terminal_field(
        _aggregate_value(failure, "host", ""),
        256,
    )
    code = _bounded_terminal_field(
        _aggregate_value(failure, "code", "remote"),
        MAX_TAIL_ERROR_CODE_LENGTH,
    )
    stderr.write(
        "watch: remote failure host={} code={}; events may be missed\n".format(
            host,
            code,
        )
    )
    stderr.flush()


def _put_watch_entry(
    target: object,
    entry: _WatchQueueEntry,
    cancel_event: threading.Event,
) -> bool:
    while not cancel_event.is_set():
        try:
            target.put(entry, timeout=WATCH_QUEUE_POLL_SECONDS)
            return True
        except queue.Full:
            continue
    return False


def _close_iterator(iterator: object) -> None:
    closer = getattr(iterator, "close", None)
    if callable(closer):
        try:
            closer()
        except BaseException:
            pass


def _invoke_direct_watch(
    provider: object,
    sessions: Sequence[Session],
    *,
    from_start: bool,
    cancel_event: threading.Event,
) -> object:
    arguments = {"from_start": from_start}
    if _accepts_keyword_argument(provider, "cancel_event"):
        arguments["cancel_event"] = cancel_event
    return provider(sessions, **arguments)


def _invoke_daemon_subscribe(
    client: object,
    cancel_event: threading.Event,
) -> object:
    subscriber = client.subscribe
    if _accepts_keyword_argument(subscriber, "cancel_event"):
        return subscriber(cancel_event=cancel_event)
    return subscriber()


def _direct_watch_selection(
    scanner: Scanner,
    stderr: TextIO,
) -> List[Session]:
    sessions = _scan_sessions(scanner, stderr, None)
    if not sessions:
        stderr.write("watch: no sessions found\n")
        stderr.flush()
        return []
    selected = [session for session in sessions if _supports_direct_watch(session)]
    skipped = len(sessions) - len(selected)
    if skipped:
        stderr.write(
            "watch: skipped {} unsupported session{} without usable direct event sources\n".format(
                skipped,
                "" if skipped == 1 else "s",
            )
        )
        stderr.flush()
    if not selected:
        stderr.write("watch: no sessions with usable direct event sources\n")
        stderr.flush()
    return selected


def _produce_direct_watch(
    provider: object,
    sessions: Sequence[Session],
    from_start: bool,
    cancel_event: threading.Event,
    target: object,
) -> None:
    iterator = _invoke_direct_watch(
        provider,
        sessions,
        from_start=from_start,
        cancel_event=cancel_event,
    )
    try:
        if not _put_watch_entry(
            target,
            _WatchQueueEntry("ready"),
            cancel_event,
        ):
            return
        for event in iterator:
            if not _put_watch_entry(
                target,
                _WatchQueueEntry("event", event),
                cancel_event,
            ):
                return
    finally:
        _close_iterator(iterator)


def _produce_local_watch(
    mode: str,
    selected: Sequence[Session],
    *,
    provider: object,
    scanner: Scanner,
    client: object,
    from_start: bool,
    cancel_event: threading.Event,
    target: object,
    stderr: TextIO,
) -> None:
    try:
        if mode == "daemon":
            iterator = None
            subscription_lost = False
            try:
                iterator = _invoke_daemon_subscribe(client, cancel_event)
                if not _put_watch_entry(
                    target,
                    _WatchQueueEntry("ready"),
                    cancel_event,
                ):
                    return
                for event in iterator:
                    if not _put_watch_entry(
                        target,
                        _WatchQueueEntry("event", event),
                        cancel_event,
                    ):
                        return
            except (SidecarClientError, OSError):
                subscription_lost = True
                if cancel_event.is_set():
                    return
                _put_watch_entry(
                    target,
                    _WatchQueueEntry(
                        "notice",
                        "watch: daemon subscription lost; switching to direct local "
                        "tailing; events during transition may be missed\n",
                    ),
                    cancel_event,
                )
            finally:
                if iterator is not None:
                    _close_iterator(iterator)
            if not subscription_lost:
                return
            if cancel_event.is_set():
                return
            selected = _direct_watch_selection(scanner, stderr)
            if not selected:
                _put_watch_entry(
                    target,
                    _WatchQueueEntry(
                        "local_failure",
                        "watch: local source failed\n",
                    ),
                    cancel_event,
                )
                return
        _produce_direct_watch(
            provider,
            selected,
            from_start=from_start,
            cancel_event=cancel_event,
            target=target,
        )
    except Exception:
        if not cancel_event.is_set():
            _put_watch_entry(
                target,
                _WatchQueueEntry("local_failure", "watch: local source failed\n"),
                cancel_event,
            )
    finally:
        _put_watch_entry(target, _WatchQueueEntry("done"), cancel_event)


def _produce_remote_watch(
    session: object,
    *,
    cancel_event: threading.Event,
    target: object,
) -> None:
    try:
        for item in session:
            if not _put_watch_entry(
                target,
                _WatchQueueEntry("item", item),
                cancel_event,
            ):
                return
    except Exception:
        if not cancel_event.is_set():
            _put_watch_entry(
                target,
                _WatchQueueEntry("remote_failure"),
                cancel_event,
            )
    finally:
        _put_watch_entry(target, _WatchQueueEntry("done"), cancel_event)


def _next_watch_entry(
    queues: Sequence[object],
    active: Sequence[bool],
    cursor: int,
) -> tuple:
    for offset in range(len(queues)):
        index = (cursor + offset) % len(queues)
        try:
            return index, queues[index].get_nowait()
        except queue.Empty:
            pass
    for offset in range(len(queues)):
        index = (cursor + offset) % len(queues)
        if not active[index] and queues[index].empty():
            continue
        try:
            return index, queues[index].get(timeout=WATCH_QUEUE_POLL_SECONDS)
        except queue.Empty:
            pass
    return -1, None


def _cleanup_watch_producers(
    cancel_event: threading.Event,
    remote_session: object,
    producers: Sequence[threading.Thread],
) -> None:
    cancel_event.set()
    closer = getattr(remote_session, "close", None)
    if callable(closer):
        try:
            closer()
        except BaseException:
            pass
    deadline = time.monotonic() + WATCH_PRODUCER_JOIN_SECONDS
    for producer in producers:
        if producer.ident is None:
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            producer.join(timeout=remaining)
        except RuntimeError:
            pass


def _start_watch_producer(
    producer: threading.Thread,
    lifecycle: _WatchSourceLifecycle,
    producers: List[threading.Thread],
    stderr: TextIO,
) -> bool:
    producers.append(producer)
    try:
        producer.start()
    except RuntimeError:
        lifecycle.started = producer.ident is not None
        lifecycle.failed = True
        lifecycle.crashed = True
        lifecycle.ended = not lifecycle.started
        stderr.write(
            "watch: {} producer setup failed\n".format(lifecycle.name)
        )
        stderr.flush()
        return False
    lifecycle.started = True
    return True


def _new_watch_queue(injected: object) -> object:
    if injected is None:
        return queue.Queue(maxsize=WATCH_QUEUE_ITEMS)
    if (
        not hasattr(injected, "put")
        or not hasattr(injected, "get")
        or not hasattr(injected, "get_nowait")
        or not hasattr(injected, "empty")
    ):
        raise TypeError("watch queue must provide the queue interface")
    return injected


def _open_remote_watch(
    provider: object,
    args: argparse.Namespace,
    cancel_event: threading.Event,
) -> object:
    arguments = {
        "selected": args.host or None,
        "from_start": args.from_start,
    }
    if _accepts_keyword_argument(provider, "cancel_event"):
        arguments["cancel_event"] = cancel_event
    return provider(**arguments)


def _run_remote_watch(
    args: argparse.Namespace,
    *,
    scanner: Optional[Scanner],
    client: SidecarClient,
    stdout: TextIO,
    stderr: TextIO,
    watch_provider: object,
    remote_watch_provider: object,
    local_watch_queue: object,
    remote_watch_queue: object,
) -> int:
    cancel_event = threading.Event()
    local_queue = _new_watch_queue(local_watch_queue)
    remote_queue = _new_watch_queue(remote_watch_queue)
    remote_session = None
    producers: List[threading.Thread] = []

    try:
        with bounded_execution_signal_guard():
            try:
                try:
                    remote_session = _open_remote_watch(
                        remote_watch_provider,
                        args,
                        cancel_event,
                    )
                except RemoteInventoryError:
                    stderr.write("watch: remote inventory\n")
                    stderr.flush()
                    return 2
                except OSError:
                    stderr.write("watch: remote setup\n")
                    stderr.flush()
                    return 2
                except ValueError:
                    stderr.write("watch: remote selection\n")
                    stderr.flush()
                    return 2

                active_scanner = Scanner() if scanner is None else scanner
                local_mode = "direct"
                local_selected: List[Session] = []
                if not args.from_start:
                    daemon_rows = _client_status(client, stderr)
                    if daemon_rows is not None:
                        local_mode = "daemon"
                    else:
                        local_selected = _direct_watch_selection(
                            active_scanner,
                            stderr,
                        )
                else:
                    local_selected = _direct_watch_selection(
                        active_scanner,
                        stderr,
                    )
                local_exists = local_mode == "daemon" or bool(local_selected)
                remote_empty = bool(getattr(remote_session, "empty", False))
                remote_exists = not remote_empty
                local_state = _WatchSourceLifecycle("local", local_exists)
                remote_state = _WatchSourceLifecycle("remote", remote_exists)
                if remote_empty:
                    _report_empty_remote_fleet(stderr)
                if not local_state.configured and not remote_state.configured:
                    return 1

                if local_state.configured:
                    local_producer = threading.Thread(
                        target=_produce_local_watch,
                        args=(local_mode, local_selected),
                        kwargs={
                            "provider": watch_provider,
                            "scanner": active_scanner,
                            "client": client,
                            "from_start": args.from_start,
                            "cancel_event": cancel_event,
                            "target": local_queue,
                            "stderr": stderr,
                        },
                        name="sidecar-watch-local",
                        daemon=False,
                    )
                    if not _start_watch_producer(
                        local_producer,
                        local_state,
                        producers,
                        stderr,
                    ):
                        return 2
                if remote_state.configured:
                    remote_producer = threading.Thread(
                        target=_produce_remote_watch,
                        args=(remote_session,),
                        kwargs={
                            "cancel_event": cancel_event,
                            "target": remote_queue,
                        },
                        name="sidecar-watch-remote",
                        daemon=False,
                    )
                    if not _start_watch_producer(
                        remote_producer,
                        remote_state,
                        producers,
                        stderr,
                    ):
                        return 2

                source_queues = (local_queue, remote_queue)
                states = (local_state, remote_state)
                cursor = 0
                while any(state.active for state in states) or any(
                    not source_queue.empty() for source_queue in source_queues
                ):
                    source, entry = _next_watch_entry(
                        source_queues,
                        [state.active for state in states],
                        cursor,
                    )
                    if source < 0:
                        continue
                    cursor = (source + 1) % len(source_queues)
                    if not isinstance(entry, _WatchQueueEntry):
                        raise RuntimeError("invalid watch queue item")
                    state = states[source]
                    if entry.kind == "done":
                        state.ended = True
                        continue
                    if entry.kind == "ready":
                        state.ready = True
                        continue
                    if entry.kind == "notice":
                        stderr.write(str(entry.value))
                        stderr.flush()
                        continue
                    if entry.kind == "local_failure":
                        local_state.failed = True
                        local_state.crashed = True
                        stderr.write(str(entry.value))
                        stderr.flush()
                        continue
                    if entry.kind == "remote_failure":
                        remote_state.failed = True
                        remote_state.crashed = True
                        stderr.write(
                            "watch: remote failure code=remote; "
                            "events may be missed\n"
                        )
                        stderr.flush()
                        continue
                    if source == 0 and entry.kind == "event":
                        _write_remote_event(
                            entry.value,
                            "local",
                            as_json=args.json,
                            stdout=stdout,
                        )
                        local_state.events += 1
                        continue
                    item = entry.value
                    if isinstance(item, RemoteWatchReady):
                        remote_state.ready = True
                        remote_state.ready_hosts.add(item.host)
                        continue
                    if isinstance(item, RemoteWatchFailure):
                        remote_state.failed = True
                        remote_state.failed_hosts.add(item.host)
                        _report_remote_watch_failure(item, stderr)
                        continue
                    if isinstance(item, RemoteWatchEvent):
                        _write_remote_event(
                            item,
                            item.host,
                            as_json=args.json,
                            stdout=stdout,
                        )
                        remote_state.ready = True
                        remote_state.ready_hosts.add(item.host)
                        remote_state.events += 1
                        continue
                    raise RuntimeError("invalid remote watch item")

                if (
                    local_state.terminal_success
                    or remote_state.terminal_success
                ):
                    return 0
                if remote_state.configured:
                    return 3
                return 1
            except KeyboardInterrupt:
                return 130
            except (BrokenPipeError, OSError, ValueError):
                return 1
            finally:
                _cleanup_watch_producers(
                    cancel_event,
                    remote_session,
                    producers,
                )
    except KeyboardInterrupt:
        _cleanup_watch_producers(cancel_event, remote_session, producers)
        return 130


def _redacted_send_text(value: object, message: str) -> str:
    text = str(value or "")
    redacted = redact_message(text, message)
    return normalize_scalar_text(redacted, errors="replace")


def _safe_send_text(value: object, message: str) -> str:
    return sanitize_terminal_text(_redacted_send_text(value, message))


def _report_send_error(error: object, message: str, stderr: TextIO) -> None:
    stderr.write("send: {}\n".format(_safe_send_text(error, message)))
    stderr.flush()


def _write_send_error(
    error: object,
    code: str,
    *,
    as_json: bool,
    message: str,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    if as_json:
        _write_json({"code": code}, stdout)
    _report_send_error(error, message, stderr)


def _report_native_send_stderr(
    result: SendResult,
    message: str,
    stderr: TextIO,
) -> None:
    native_error = _safe_send_text(result.stderr, message)
    if native_error:
        stderr.write("send: native stderr: {}\n".format(native_error))
        stderr.flush()


def _write_send_result(
    result: SendResult,
    *,
    as_json: bool,
    message: str,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    delivered = result.delivery == "delivered"
    if as_json:
        payload = result.to_dict()
        payload["response"] = _redacted_send_text(payload.get("response"), message)
        payload["stderr"] = _redacted_send_text(payload.get("stderr"), message)
        _write_json(payload, stdout)
    else:
        stdout.write(
            "request_id={} replayed={}\n".format(
                sanitize_terminal_text(result.request_id),
                "true" if result.replayed else "false",
            )
        )
        if delivered:
            response = _safe_send_text(result.response, message)
            if response:
                stdout.write(response + "\n")
            else:
                stdout.write(
                    "delivered to {}:{}\n".format(
                        sanitize_terminal_text(result.agent),
                        sanitize_terminal_text(result.session_id),
                    )
                )
        elif result.outcome == "completed":
            stdout.write(
                "completed but delivery unknown for {}:{}; do not retry\n".format(
                    sanitize_terminal_text(result.agent),
                    sanitize_terminal_text(result.session_id),
                )
            )
        else:
            stdout.write(
                "delivery unknown for {}:{} ({})\n".format(
                    sanitize_terminal_text(result.agent),
                    sanitize_terminal_text(result.session_id),
                    sanitize_terminal_text(result.outcome),
                )
            )
        stdout.flush()

    if delivered:
        if not as_json:
            _report_native_send_stderr(result, message, stderr)
        return 0

    if result.outcome == "completed":
        diagnostic = "completed but delivery unknown; do not retry"
    else:
        diagnostic = "delivery status is unknown after {}".format(
            sanitize_terminal_text(result.outcome)
        )
    if result.error_code:
        diagnostic += " ({})".format(sanitize_terminal_text(result.error_code))
    stderr.write("send: {}\n".format(diagnostic))
    stderr.flush()
    _report_native_send_stderr(result, message, stderr)
    return 1


def _read_stdin_message(stream: object) -> str:
    """Read a byte-bounded piped stdin message and decode it fail-closed.

    Interactive terminals are rejected before any read so the command never
    silently blocks waiting for typed input, and read-mechanics failures
    (a detached ``sys.stdin``, closed or unreadable streams, non-byte
    chunks) report the dedicated stdin vocabulary instead of a misleading
    message-validation diagnostic.
    """

    binary = None if stream is None else getattr(stream, "buffer", stream)
    if binary is None:
        raise SendError("stdin_unreadable")
    try:
        interactive = binary.isatty()
    except (AttributeError, OSError, ValueError):
        interactive = False
    if interactive:
        raise SendError("stdin_interactive")
    chunks: List[bytes] = []
    received = 0
    while received <= MAX_MESSAGE_BYTES:
        try:
            chunk = binary.read(MAX_MESSAGE_BYTES + 1 - received)
        except (AttributeError, OSError, ValueError) as error:
            raise SendError("stdin_unreadable") from error
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise SendError("stdin_unreadable")
        chunks.append(bytes(chunk))
        received += len(chunks[-1])
    data = b"".join(chunks)
    if len(data) > MAX_MESSAGE_BYTES:
        raise SendError("message_too_large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SendError("invalid_message_utf8") from error


def _run_send(
    args: argparse.Namespace,
    *,
    scanner: Optional[Scanner],
    stdout: TextIO,
    stderr: TextIO,
    stdin: Optional[object] = None,
    planner=None,
    executor=None,
) -> int:
    if args.allow_write is not True:
        stderr.write("send: explicit --allow-write is required\n")
        stderr.flush()
        return 2

    if args.message_stdin == (args.message is not None):
        stderr.write(
            "send: provide the message exactly once, either as the "
            "positional argument or through --message-stdin\n"
        )
        stderr.flush()
        return 2

    if args.message_stdin:
        try:
            message = _read_stdin_message(
                sys.stdin if stdin is None else stdin
            )
        except KeyboardInterrupt:
            stderr.write(
                "send: interrupted while reading the message from "
                "standard input; nothing was delivered\n"
            )
            stderr.flush()
            return 130
        except SendError as error:
            _write_send_error(
                error,
                error.code,
                as_json=args.json,
                message="",
                stdout=stdout,
                stderr=stderr,
            )
            return 2
    else:
        message = args.message

    active_scanner = Scanner() if scanner is None else scanner
    sessions = _scan_sessions(active_scanner, stderr, None)
    if args.agent is not None:
        sessions = _filter_agent_rows(sessions, (args.agent,))
    try:
        selected = (
            _resolve_exact_session(sessions, args.session_prefix)
            if args.exact_session
            else resolve_session_prefix(sessions, args.session_prefix)
        )
    except LookupError as error:
        _write_send_error(
            error,
            "target_not_found",
            as_json=args.json,
            message=message,
            stdout=stdout,
            stderr=stderr,
        )
        return 2
    except ValueError as error:
        _write_send_error(
            error,
            "ambiguous_session",
            as_json=args.json,
            message=message,
            stdout=stdout,
            stderr=stderr,
        )
        return 2

    plan_builder = build_send_plan if planner is None else planner
    send_executor = execute_send if executor is None else executor
    request_id = args.request_id or generate_request_id()

    def refresh_selected_session() -> List[Session]:
        return [
            session
            for session in _scan_sessions(active_scanner, stderr, None)
            if isinstance(session, Session)
            and session.agent == selected.agent
            and session.session_id == selected.session_id
        ]

    try:
        plan = plan_builder(selected, message)
        result = send_executor(
            plan,
            allow_write=args.allow_write,
            timeout=args.timeout,
            refresher=refresh_selected_session,
            request_id=request_id,
        )
        if (
            result.agent != selected.agent
            or result.session_id != selected.session_id
            or result.request_id != request_id
        ):
            result = SendResult(
                agent=selected.agent,
                session_id=selected.session_id,
                outcome="failed",
                delivery="unknown",
                error_code="executor_error",
                request_id=request_id,
            )
    except SendError as error:
        _write_send_error(
            error,
            error.code,
            as_json=args.json,
            message=message,
            stdout=stdout,
            stderr=stderr,
        )
        return 2
    except KeyboardInterrupt:
        result = SendResult(
            agent=selected.agent,
            session_id=selected.session_id,
            outcome="failed",
            delivery="unknown",
            error_code="interrupted",
            request_id=request_id,
        )
        _write_send_result(
            result,
            as_json=args.json,
            message=message,
            stdout=stdout,
            stderr=stderr,
        )
        return 130
    return _write_send_result(
        result,
        as_json=args.json,
        message=message,
        stdout=stdout,
        stderr=stderr,
    )


def _run_audit_reset(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    stderr: TextIO,
    resetter=None,
) -> int:
    if (
        args.allow_write is not True
        or args.confirm != "CLEAR-SEND-AUDIT"
    ):
        stderr.write(
            "audit reset: requires --allow-write and "
            "--confirm CLEAR-SEND-AUDIT\n"
        )
        stderr.flush()
        return 2
    operation = SendAuditStore().reset if resetter is None else resetter
    try:
        operation()
    except AuditError as error:
        stderr.write(
            "audit reset: {}\n".format(sanitize_terminal_text(str(error)))
        )
        stderr.flush()
        return 1
    stdout.write("send audit reset\n")
    stdout.flush()
    stderr.write(
        "warning: send request-id idempotency history has been lost\n"
    )
    stderr.flush()
    return 0


def _run_package_build(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        artifact = build_release_zipapp(args.output)
    except ReleaseError as error:
        stderr.write(
            "package build: {}\n".format(
                sanitize_terminal_text(str(error))
            )
        )
        stderr.flush()
        return 1
    stdout.write(
        "path={} sha256={} size={}\n".format(
            sanitize_terminal_text(str(artifact.path)),
            artifact.sha256,
            artifact.size,
        )
    )
    stdout.flush()
    return 0


def _write_service_result(
    result: ServiceResult,
    *,
    command: str,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    code = int(result.exit_code)
    target = stdout if code == 0 or command == "status" and code == 1 else stderr
    target.write("{}\n".format(sanitize_terminal_text(result.message)))
    target.flush()
    return code


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    scanner: Optional[Scanner] = None,
    client: Optional[SidecarClient] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    stdin: Optional[object] = None,
    process_provider=None,
    watch_provider=None,
    remote_watch_provider=None,
    local_watch_queue=None,
    remote_watch_queue=None,
    tui_runner=None,
    remote_aggregator=None,
    send_planner=None,
    send_executor=None,
    audit_resetter=None,
    service_installer=None,
    service_uninstaller=None,
    service_status_provider=None,
) -> int:
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    args = build_parser().parse_args(argv)

    if (
        args.command in ("list", "status")
        and args.host
        and not args.remote
    ):
        errors.write("{}: --host requires --remote\n".format(args.command))
        errors.flush()
        return 2

    if args.command == "watch" and args.stream_ready and (
        not args.all
        or not args.json
        or args.remote
        or args.session_prefix
    ):
        errors.write(
            "watch: --stream-ready requires local --all --json\n"
        )
        errors.flush()
        return 2

    if args.command == "watch" and args.host and not args.remote:
        errors.write("watch: --host requires --remote\n")
        errors.flush()
        return 2
    if args.command == "watch" and args.remote:
        if args.session_prefix:
            errors.write(
                "watch: --remote conflicts with a session prefix; use --all\n"
            )
            errors.flush()
            return 2
        if not args.all:
            errors.write("watch: --remote requires --all\n")
            errors.flush()
            return 2

    if (
        args.command == "daemon"
        and args.daemon_command in ("start", "run")
        and args.http_port is not None
        and not args.http
    ):
        errors.write(
            "daemon {}: --http-port requires --http\n".format(
                args.daemon_command
            )
        )
        errors.flush()
        return 2

    if (
        args.command == "service"
        and args.service_command == "install"
        and args.http_port is not None
        and not args.http
    ):
        errors.write("service install: --http-port requires --http\n")
        errors.flush()
        return 2

    if args.command == "send":
        return _run_send(
            args,
            scanner=scanner,
            stdout=output,
            stderr=errors,
            stdin=stdin,
            planner=send_planner,
            executor=send_executor,
        )

    if args.command == "audit":
        return _run_audit_reset(
            args,
            stdout=output,
            stderr=errors,
            resetter=audit_resetter,
        )

    if args.command == "package":
        return _run_package_build(
            args,
            stdout=output,
            stderr=errors,
        )

    if args.command == "service":
        active_client = SidecarClient() if client is None else client
        if args.service_command == "install":
            provider = (
                install_service
                if service_installer is None
                else service_installer
            )
            result = provider(
                http=args.http,
                http_port=args.http_port,
                force=args.force,
                client=active_client,
            )
        elif args.service_command == "uninstall":
            provider = (
                uninstall_service
                if service_uninstaller is None
                else service_uninstaller
            )
            result = provider(client=active_client)
        else:
            provider = (
                service_status
                if service_status_provider is None
                else service_status_provider
            )
            result = provider(client=active_client)
        return _write_service_result(
            result,
            command=args.service_command,
            stdout=output,
            stderr=errors,
        )

    if args.command == "ps":
        provider = running_agent_processes if process_provider is None else process_provider
        processes = provider()
        if args.json:
            _write_json(processes, output)
        else:
            _print_processes(processes, output)
        return 0

    active_client = SidecarClient() if client is None else client

    if args.command == "daemon":
        if args.daemon_command == "start":
            return _daemon_start(
                active_client,
                output,
                errors,
                http=args.http,
                http_port=args.http_port,
            )
        if args.daemon_command == "stop":
            return _daemon_stop(active_client, output, errors)
        if args.daemon_command == "status":
            return _daemon_status(active_client, output)
        return _daemon_run(
            errors,
            http_port=(
                0 if args.http and args.http_port is None else args.http_port
            ),
        )

    if args.command == "tui":
        runner = run_tui if tui_runner is None else tui_runner
        return runner(
            scanner=scanner,
            client=_TailReportingClient(active_client, errors),
            stdout=output,
            once=args.once,
        )

    if args.command in ("list", "status"):
        if args.remote:
            return _run_remote_snapshot(
                args.command,
                args,
                scanner=scanner,
                client=active_client,
                stdout=output,
                stderr=errors,
                remote_aggregator=remote_aggregator,
            )
        sessions = _acquire_local_snapshot(
            args.command,
            args,
            scanner=scanner,
            client=active_client,
            stderr=errors,
        )
        _render_snapshot(
            args.command,
            args,
            sessions,
            stdout=output,
            show_host=False,
        )
        return 0

    if args.remote:
        local_provider = watch_sessions if watch_provider is None else watch_provider
        fleet_provider = (
            watch_remote
            if remote_watch_provider is None
            else remote_watch_provider
        )
        return _run_remote_watch(
            args,
            scanner=scanner,
            client=active_client,
            stdout=output,
            stderr=errors,
            watch_provider=local_provider,
            remote_watch_provider=fleet_provider,
            local_watch_queue=local_watch_queue,
            remote_watch_queue=remote_watch_queue,
        )

    if args.all and args.session_prefix:
        errors.write("watch: session prefix and --all are mutually exclusive\n")
        errors.flush()
        return 2
    if not args.all and not args.session_prefix:
        errors.write("watch: provide a session prefix or --all\n")
        errors.flush()
        return 2

    provider = watch_sessions if watch_provider is None else watch_provider
    ready_writer = _WatchReadyWriter(output) if args.stream_ready else None
    if args.all and not args.from_start:
        daemon_rows = _client_status(active_client, errors)
        if daemon_rows is not None:
            try:
                subscription = (
                    active_client.subscribe()
                    if ready_writer is None
                    else active_client.subscribe(on_ready=ready_writer)
                )
                for event in subscription:
                    _write_event(event, args.json, output)
                return 0
            except KeyboardInterrupt:
                return 130
            except (SidecarClientError, OSError):
                errors.write(
                    "watch: daemon subscription lost; switching to direct local "
                    "tailing; events during transition may be missed\n"
                )
                errors.flush()
            except Exception:
                errors.write("watch: local source failed\n")
                errors.flush()
                return 1

    active_scanner = Scanner() if scanner is None else scanner
    sessions = _scan_sessions(active_scanner, errors, None)
    if args.all:
        if not sessions:
            errors.write("watch: no sessions found\n")
            errors.flush()
            return 1
        selected = [session for session in sessions if _supports_direct_watch(session)]
        skipped = len(sessions) - len(selected)
        if skipped:
            errors.write(
                "watch: skipped {} unsupported session{} without usable direct event sources\n".format(
                    skipped,
                    "" if skipped == 1 else "s",
                )
            )
            errors.flush()
        if not selected:
            errors.write("watch: no sessions with usable direct event sources\n")
            errors.flush()
            return 1
    else:
        try:
            selected = [resolve_session_prefix(sessions, args.session_prefix)]
        except (LookupError, ValueError) as error:
            errors.write("watch: {}\n".format(sanitize_terminal_text(error)))
            errors.flush()
            return 2
        if not _supports_direct_watch(selected[0]):
            errors.write(
                "watch: session {}:{} has no usable direct event source\n".format(
                    sanitize_terminal_text(selected[0].agent),
                    sanitize_terminal_text(selected[0].session_id),
                )
            )
            errors.flush()
            return 2

    try:
        provider_arguments = {"from_start": args.from_start}
        if ready_writer is not None:
            provider_arguments["on_ready"] = ready_writer
        for event in provider(selected, **provider_arguments):
            _write_event(event, args.json, output)
    except KeyboardInterrupt:
        return 130
    except Exception:
        errors.write("watch: local source failed\n")
        errors.flush()
        return 1
    return 0


__all__ = [
    "DAEMON_POLL_INTERVAL",
    "DAEMON_START_TIMEOUT",
    "DAEMON_STOP_TIMEOUT",
    "RECENT_SECONDS",
    "TAIL_ERROR_DEDUPE_LIMIT",
    "build_parser",
    "main",
    "resolve_session_prefix",
]
