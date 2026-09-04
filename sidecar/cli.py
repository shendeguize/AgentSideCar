"""Command-line interface for sidecar observation and local message delivery."""

from __future__ import annotations

import argparse
import datetime as dt
import inspect
import json
import math
import os
import queue
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    TextIO,
    Tuple,
)

import sidecar
from sidecar.adapters.base import sanitize_terminal_text
from sidecar.archive import (
    DEFAULT_IDLE_THRESHOLD_SECONDS,
    ArchiveError,
    ArchiveStore,
    normalize_statuses,
    parse_duration,
    select_archivable,
    session_target,
)
from sidecar.client import PingInfo, SidecarClient, SidecarClientError
from sidecar.cluster import (
    DEFAULT_CLUSTER_WINDOW_SECONDS,
    MAX_CLUSTER_WINDOW_SECONDS,
    cluster_sessions,
    merge_cluster_results,
)
from sidecar.daemon import (
    PIDFILE_NAME,
    SOCKET_NAME,
    DaemonAlreadyRunning,
    DaemonError,
    _socket_is_live,
    default_runtime_dir,
    read_pid,
    run_foreground,
    runtime_owner,
)
from sidecar.inject import (
    DEFAULT_SEND_TIMEOUT_SECONDS,
    MAX_MESSAGE_BYTES,
    SendError,
    SendResult,
    build_send_plan,
    execute_send,
)
from sidecar.service import (
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
    validate_remote_python_executable,
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
from sidecar.semantic import (
    DEFAULT_SEMANTIC_RULES,
    SEMANTIC_MAX_GROUPS,
    SEMANTIC_RULES,
    build_semantic_payload,
    run_headless_report,
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
# A daemon answers nothing until its first scan finishes, and that scan grows
# with the index: a 1,950-session machine took 22s. Five seconds bought a
# verdict before the evidence existed, so this budget outlasts a real scan and
# ends the moment a ping lands.
DAEMON_START_TIMEOUT = 45.0
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
REMOTE_PYTHON_ENV = "AGENT_SIDECAR_REMOTE_PYTHON"
_EVENT_FIELDS = ("ts", "agent", "session_id", "kind", "text", "extra")
_SESSION_LINE = (
    "{branch}{agent:<11} {status:<7} {session:<16} {age:>4}"
    "  {updated:<25}  {title}\n"
)
_REMOTE_SESSION_LINE = (
    "{branch}{host:<16} {agent:<11} {status:<7} {session:<16} {age:>4}"
    "  {updated:<25}  {title}\n"
)
_ARCHIVE_LINE = "{agent:<11} {session:<16} {archived:<25}  {reason:<7} {title}\n"


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


def _cluster_window_argument(value: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError(
            "cluster window must be a finite positive number"
        ) from error
    if (
        not math.isfinite(seconds)
        or not 0 < seconds <= MAX_CLUSTER_WINDOW_SECONDS
    ):
        raise argparse.ArgumentTypeError(
            "cluster window must be positive and at most {}".format(
                int(MAX_CLUSTER_WINDOW_SECONDS)
            )
        )
    return seconds


def _semantic_rules_argument(value: str) -> tuple[str, ...]:
    rules = tuple(part.strip().lower() for part in str(value).split(",") if part.strip())
    unknown = sorted(set(rules) - SEMANTIC_RULES)
    if unknown:
        raise argparse.ArgumentTypeError(
            "unknown semantic rule: {}".format(",".join(unknown))
        )
    if len(rules) != len(set(rules)):
        raise argparse.ArgumentTypeError("semantic rules must not repeat")
    return rules or DEFAULT_SEMANTIC_RULES


def _semantic_max_groups_argument(value: str) -> int:
    try:
        groups = int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError(
            "semantic max-groups must be a positive integer"
        ) from error
    if groups <= 0 or groups > 10_000:
        raise argparse.ArgumentTypeError(
            "semantic max-groups must be from 1 through 10000"
        )
    return groups


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


# A threshold without the switch would be silently ignored, which reads as
# "auto-archive is configured" when nothing is archiving at all.
_AUTO_ARCHIVE_ORPHAN_THRESHOLD = (
    "--auto-archive-after requires --auto-archive\n"
)


def _auto_archive_after_argument(value: str) -> float:
    try:
        return parse_duration(value)
    except ArchiveError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _add_auto_archive_arguments(parser: argparse.ArgumentParser) -> None:
    """Auto-archive policy flags for the daemon-starting commands.

    Default off, threshold conservative, and archive-only: the automatic
    path never disposes anything, and a session that speaks again is
    unarchived by the same scan that would have hidden it.
    """

    parser.add_argument(
        "--auto-archive",
        action="store_true",
        help=(
            "hide sessions idle longer than --auto-archive-after from the "
            "board (observation only; reversed automatically on new activity)"
        ),
    )
    parser.add_argument(
        "--auto-archive-after",
        type=_auto_archive_after_argument,
        default=None,
        metavar="DURATION",
        help=(
            "inactivity threshold for --auto-archive, e.g. 30m/2h/24h "
            "(default: 24h)"
        ),
    )


def _add_local_only_arguments(parser: argparse.ArgumentParser) -> None:
    """Accept the fleet flags on a per-host command, in order to refuse them.

    The archive registry belongs to the host that owns the sessions, so
    there is no fleet-wide archive to operate on. Silently ignoring
    `--remote` would be the dangerous reading ("I archived the fleet"), and
    an argparse "unrecognized argument" would not say what to do instead —
    so both flags parse and are answered with the ssh form that works
    (see {@link _local_only_error}).
    """

    parser.add_argument("--remote", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        metavar="ALIAS",
        help=argparse.SUPPRESS,
    )


def _local_only_error(command: str, argv: Sequence[str]) -> str:
    """The refusal text for a fleet flag on a per-host archive command."""

    return (
        "{command}: archiving is per host; --remote/--host are unsupported. "
        "Run it on the host instead: ssh HOST agent-sidecar {argv}\n".format(
            command=sanitize_terminal_text(command),
            argv=sanitize_terminal_text(shlex.join(argv)),
        )
    )


def _local_only_argv(args: argparse.Namespace) -> Tuple[str, ...]:
    """Rebuild the refused request as the per-host argv that would work.

    Echoing back the same selection means the suggested ssh line can be
    copied as-is; only the fleet flags are dropped.
    """

    if args.command == "unarchive":
        if args.all:
            return ("unarchive", "--all")
        prefix = args.session_prefix
        return ("unarchive", str(prefix)) if prefix else ("unarchive",)
    argv = ["archive", str(args.archive_action)]
    if args.archive_action == "list":
        return tuple(argv)
    # The default threshold is a number set by the parser; only a value the
    # caller actually typed is worth repeating.
    if isinstance(args.idle_longer_than, str):
        argv.extend(("--idle-longer-than", args.idle_longer_than))
    if args.status is not None:
        argv.extend(("--status", str(args.status)))
    if args.dry_run:
        argv.append("--dry-run")
    if args.yes:
        argv.append("--yes")
    return tuple(argv)


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
        "--archived",
        action="store_true",
        help="list archived sessions instead of active ones (local only)",
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
    list_parser.add_argument(
        "--remote-python",
        default=None,
        metavar="PATH",
        help="pin an absolute Python path on every remote host (requires --remote)",
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
    status_parser.add_argument(
        "--remote-python",
        default=None,
        metavar="PATH",
        help="pin an absolute Python path on every remote host (requires --remote)",
    )

    cluster_parser = commands.add_parser(
        "cluster",
        help="group sessions by project, agent, model, and time window",
    )
    cluster_recency = cluster_parser.add_mutually_exclusive_group()
    cluster_recency.add_argument(
        "--all",
        action="store_true",
        help="include sessions older than 48h",
    )
    cluster_recency.add_argument(
        "--recent-seconds",
        type=_recent_seconds_argument,
        default=None,
        help=argparse.SUPPRESS,
    )
    cluster_parser.add_argument(
        "--window-seconds",
        type=_cluster_window_argument,
        default=DEFAULT_CLUSTER_WINDOW_SECONDS,
        metavar="SEC",
        help="deterministic time bucket size (default: 86400)",
    )
    cluster_parser.add_argument("--json", action="store_true", help="emit JSON")
    cluster_parser.add_argument(
        "--remote",
        action="store_true",
        help="include eligible remote hosts",
    )
    cluster_parser.add_argument(
        "--host",
        action="append",
        default=[],
        metavar="ALIAS",
        help="select an eligible remote host (repeatable; requires --remote)",
    )
    cluster_parser.add_argument(
        "--remote-python",
        default=None,
        metavar="PATH",
        help="pin an absolute Python path on every remote host (requires --remote)",
    )
    cluster_parser.add_argument(
        "--semantic",
        action="store_true",
        help="optionally add a bounded local dsh headless summary",
    )
    cluster_parser.add_argument(
        "--semantic-rules",
        type=_semantic_rules_argument,
        default=DEFAULT_SEMANTIC_RULES,
        metavar="RULES",
        help="comma-separated semantic selection rules",
    )
    cluster_parser.add_argument(
        "--semantic-max-groups",
        type=_semantic_max_groups_argument,
        default=SEMANTIC_MAX_GROUPS,
        metavar="N",
        help="maximum clusters sent to semantic analysis (default: 100)",
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
    watch_parser.add_argument(
        "--remote-python",
        default=None,
        metavar="PATH",
        help="pin an absolute Python path on every remote host (requires --remote)",
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
        help="CLEAR-SEND-AUDIT, or PURGE-SEND-AUDIT with --purge",
    )
    audit_reset_parser.add_argument(
        "--purge",
        action="store_true",
        help="irreversibly delete active and archived send-audit state",
    )
    audit_rebind_parser = audit_commands.add_parser(
        "rebind",
        help="repair a strictly verified moved send-audit namespace",
        allow_abbrev=False,
    )
    audit_rebind_parser.add_argument(
        "--allow-write",
        action="store_true",
        help="explicitly permit updating the audit namespace marker",
    )
    audit_rebind_parser.add_argument(
        "--confirm",
        default=None,
        metavar="TEXT",
        help="must be exactly REBIND-SEND-AUDIT",
    )

    archive_parser = commands.add_parser(
        "archive",
        help="hide idle or dead sessions from the board without touching them",
        description=(
            "Archiving is observation-level: it never edits a vendor "
            "transcript and never stops a process. A session that becomes "
            "active again is released automatically."
        ),
        allow_abbrev=False,
    )
    archive_parser.add_argument(
        "archive_action",
        nargs="?",
        default="apply",
        choices=("apply", "list"),
        metavar="ACTION",
        help="apply the threshold selection (default) or list archived sessions",
    )
    archive_parser.add_argument(
        "--idle-longer-than",
        default=DEFAULT_IDLE_THRESHOLD_SECONDS,
        metavar="DURATION",
        help="inactivity threshold such as 30m, 2h, or 24h (default: 2h)",
    )
    archive_parser.add_argument(
        "--status",
        default=None,
        metavar="NAMES",
        help="comma-separated statuses to archive (default: idle,dead)",
    )
    archive_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the selection without archiving anything",
    )
    archive_parser.add_argument(
        "--yes",
        action="store_true",
        help="archive the selection instead of printing it for review",
    )
    archive_parser.add_argument("--json", action="store_true", help="emit JSON")
    _add_local_only_arguments(archive_parser)

    unarchive_parser = commands.add_parser(
        "unarchive",
        help="return archived sessions to the board",
        allow_abbrev=False,
    )
    unarchive_parser.add_argument(
        "session_prefix",
        nargs="?",
        default=None,
        metavar="session-prefix",
        help="exact archived session ID or unique prefix",
    )
    unarchive_parser.add_argument(
        "--all",
        action="store_true",
        help="release every archived session",
    )
    unarchive_parser.add_argument("--json", action="store_true", help="emit JSON")
    _add_local_only_arguments(unarchive_parser)

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
    _add_auto_archive_arguments(daemon_start_parser)
    daemon_commands.add_parser("stop", help="stop the verified running daemon")
    daemon_commands.add_parser("status", help="report whether the daemon is running")
    daemon_run_parser = daemon_commands.add_parser(
        "run",
        help=argparse.SUPPRESS,
        allow_abbrev=False,
    )
    _add_http_arguments(daemon_run_parser)
    _add_auto_archive_arguments(daemon_run_parser)

    service_parser = commands.add_parser(
        "service",
        help="manage the explicit macOS LaunchAgent or Linux systemd user service",
    )
    service_commands = service_parser.add_subparsers(
        dest="service_command",
        required=True,
    )
    service_install_parser = service_commands.add_parser(
        "install",
        help="install and start the current-user service",
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
        help="stop and remove the validated current-user service",
        allow_abbrev=False,
    )
    service_commands.add_parser(
        "status",
        help="show combined service and daemon health",
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
    sessions = _scan_sessions(
        active_scanner,
        stderr,
        _snapshot_recent_seconds(command, args),
    )
    # The daemon already hides archived sessions; the direct-scan fallback
    # has to apply the same registry so both paths agree.
    return _hide_archived(sessions, stderr)


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


def _report_archive_scope(stderr: TextIO) -> None:
    """Say what the fleet view cannot show, and where to manage it.

    A merged snapshot is a union of per-host VISIBLE sets: rows archived
    here are absent, and every remote host applies its own registry before
    answering. Stating that once, with the local count, keeps a hidden row
    from reading as a lost session or a broken host — and names the only
    command that can act on a remote registry.
    """

    try:
        archived = len(ArchiveStore().entries())
    except ArchiveError:
        # A corrupt registry is already reported by the snapshot path; a
        # second complaint here would add noise, not information.
        return
    if archived <= 0:
        return
    stderr.write(
        "remote: {} session(s) archived on this host are not listed; each "
        "remote host applies its own registry (ssh HOST agent-sidecar "
        "archive list)\n".format(archived)
    )
    stderr.flush()


def _python_too_old_host_count(failures: Iterable[object]) -> int:
    hosts = {
        str(_aggregate_value(failure, "host", "")).casefold()
        for failure in failures
        if _aggregate_value(failure, "code", "") == "python_too_old"
    }
    return len(hosts)


def _report_remote_python_hint(
    host_count: int,
    *,
    explicit: bool,
    stderr: TextIO,
) -> None:
    if host_count <= 0:
        return
    if explicit:
        stderr.write(
            "remote: the interpreter set via "
            "--remote-python/AGENT_SIDECAR_REMOTE_PYTHON is missing or "
            "older than 3.8 on {} host(s)\n".format(host_count)
        )
    else:
        stderr.write(
            "remote: no Python >= 3.8 found among bounded candidates on "
            "{} host(s); use --remote-python <absolute-path> or "
            "AGENT_SIDECAR_REMOTE_PYTHON to pin an interpreter\n".format(
                host_count
            )
        )
    stderr.flush()


def _report_empty_remote_fleet(stderr: TextIO) -> None:
    stderr.write("remote: no eligible hosts; showing local sessions only\n")
    stderr.flush()


def _cluster_recent_seconds(args: argparse.Namespace) -> Optional[float]:
    if args.recent_seconds is not None:
        return args.recent_seconds
    return None if args.all else RECENT_SECONDS


def _print_clusters(
    groups: Iterable[Mapping[str, Any]],
    stdout: TextIO,
) -> None:
    for group in groups:
        hosts = ",".join(
            _bounded_terminal_field(host, 64)
            for host in group.get("hosts", ())
            if isinstance(host, str)
        )
        stdout.write(
            "{cluster:<16} {count:>5} {agent:<11} {model:<24} "
            "{project} [{hosts}]\n".format(
                cluster=_bounded_terminal_field(group.get("cluster_id"), 16),
                count=group.get("count", 0),
                agent=_bounded_terminal_field(group.get("agent"), 11),
                model=_bounded_terminal_field(group.get("model"), 24),
                project=_bounded_terminal_field(group.get("project"), 120),
                hosts=hosts or "local",
            )
        )
    stdout.flush()


def _run_cluster(
    args: argparse.Namespace,
    *,
    scanner: Optional[Scanner],
    client: SidecarClient,
    stdout: TextIO,
    stderr: TextIO,
    remote_aggregator=None,
) -> int:
    local_args = argparse.Namespace(
        all=args.all,
        recent_seconds=args.recent_seconds,
        agent=[],
    )
    local_rows = _acquire_local_snapshot(
        "list",
        local_args,
        scanner=scanner,
        client=client,
        stderr=stderr,
    )
    # Only the direct-scan fallback narrows itself; the daemon hands back its
    # whole index. Applying the window here is what makes --all mean something
    # and keeps the answer the same whether or not the daemon was answering.
    local_rows = _recent_rows(local_rows, _cluster_recent_seconds(args))
    local_values: List[Mapping[str, Any]] = []
    for session in local_rows:
        row = dict(_as_dict(session))
        row["host"] = "local"
        local_values.append(row)
    local_groups = cluster_sessions(
        local_values,
        window_seconds=args.window_seconds,
    )

    groups: List[Mapping[str, Any]] = list(local_groups)
    if args.remote:
        from sidecar.remote import (
            EXIT_NO_SUCCESS,
            RemoteInventoryError,
            aggregate_remote,
        )

        provider = aggregate_remote if remote_aggregator is None else remote_aggregator
        try:
            result = provider(
                "cluster",
                recent_seconds=_cluster_recent_seconds(args),
                selected=args.host or None,
                python_candidates=args.remote_python_candidates,
            )
        except RemoteInventoryError:
            stderr.write("remote: inventory\n")
            stderr.flush()
            return 2
        except (OSError, TypeError, ValueError) as error:
            stderr.write(
                "remote: {}\n".format(sanitize_terminal_text(str(error)))
            )
            stderr.flush()
            return 2
        failures = _aggregate_value(result, "failures", ())
        _report_remote_failures(failures, stderr)
        remote_groups = _aggregate_value(result, "rows", ())
        groups.extend(
            row
            for row in remote_groups
            if isinstance(row, Mapping)
        )
        try:
            result_exit_code = int(
                _aggregate_value(result, "exit_code", EXIT_NO_SUCCESS)
            )
        except (TypeError, ValueError):
            result_exit_code = EXIT_NO_SUCCESS
        if result_exit_code != 0 and not remote_groups:
            groups = local_groups
    merged = merge_cluster_results(groups)
    semantic = None
    if args.semantic:
        semantic = run_headless_report(
            build_semantic_payload(
                merged,
                rules=args.semantic_rules,
                max_groups=args.semantic_max_groups,
            )
        )
        if not semantic["ok"]:
            stderr.write("semantic: unavailable; deterministic clusters retained\n")
            stderr.flush()
    if args.json:
        _write_json(
            {"clusters": merged, "semantic": semantic}
            if args.semantic
            else merged,
            stdout,
        )
    else:
        _print_clusters(merged, stdout)
    return 0


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
    selection_error = False
    try:
        provider_arguments = {"selected": args.host or None}
        if command == "list" and _accepts_recent_seconds(provider):
            provider_arguments["recent_seconds"] = _snapshot_recent_seconds(
                command,
                args,
            )
        if args.remote_python_candidates is not None:
            if not _accepts_keyword_argument(provider, "python_candidates"):
                raise TypeError(
                    "remote aggregator does not accept python_candidates"
                )
            provider_arguments["python_candidates"] = (
                args.remote_python_candidates
            )
        result = provider(command, **provider_arguments)
    except RemoteInventoryError:
        control_error = "remote: inventory\n"
    except OSError:
        control_error = "remote: setup\n"
    except ValueError as error:
        selection_error = True
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
        _report_remote_python_hint(
            _python_too_old_host_count(failure_values),
            explicit=args.remote_python_candidates is not None,
            stderr=stderr,
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

    _report_archive_scope(stderr)

    _render_snapshot(
        command,
        args,
        remote_rows if selection_error else list(local_rows) + remote_rows,
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


def _silent_daemon_line(client: object) -> str:
    """Describe a runtime whose socket said nothing.

    Silence has two causes with opposite meanings: no daemon at all, or one
    that is still doing the first scan it must finish before it answers. Only
    the second leaves a live owner behind, so when there is one the pid is
    reported instead of an absence the ping never established.
    """

    owner = runtime_owner(_runtime_dir_for(client))
    if owner is None:
        return "daemon is not running\n"
    return (
        "daemon is not answering: pid {} owns the runtime and a starting "
        "daemon stays silent until its first scan finishes\n".format(owner)
    )


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
    auto_archive: bool = False,
    auto_archive_after: Optional[float] = None,
) -> int:
    requested_http_port = 0 if http_port is None else http_port
    if auto_archive_after is not None and not auto_archive:
        stderr.write(_AUTO_ARCHIVE_ORPHAN_THRESHOLD)
        stderr.flush()
        return 2
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
    # An already-running daemon keeps whatever policy it was started with:
    # there is no live reconfiguration op, and silently mutating another
    # process's policy from a `start` that only adopted it would be worse
    # than requiring a restart. These flags shape the child we spawn.
    if auto_archive:
        command.append("--auto-archive")
        if auto_archive_after is not None:
            command.extend(("--auto-archive-after", str(auto_archive_after)))
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
                    # The child loses the ownership lock when a service or a
                    # racing start already holds it, and the winner is then
                    # mid-scan and silent. Waiting it out ends in the adoption
                    # path below; calling it a dead daemon ends in a lie.
                    owner = runtime_owner(_runtime_dir_for(client))
                    if owner is None or owner == process.pid:
                        failure = "daemon child exited before readiness"
                        break
                    failure = (
                        "daemon child lost the runtime to pid {}, which has "
                        "not answered yet".format(owner)
                    )
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
        stdout.write(_silent_daemon_line(client))
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
        stdout.write(_silent_daemon_line(client))
        stdout.flush()
        return 1
    reported = sanitize_terminal_text(info.version)
    if reported:
        stdout.write(
            "daemon is running (pid {}, version {})\n".format(info.pid, reported)
        )
    else:
        stdout.write("daemon is running (pid {})\n".format(info.pid))
    # The daemon's own tree is what it would load on restart; this CLI's
    # version is only a stand-in for it, and a wrong one when the two come
    # from different installs. Between releases the version cannot move at
    # all, so the daemon's own content check is the only signal left.
    on_disk = sanitize_terminal_text(info.source_version) or sidecar.__version__
    if reported and reported != on_disk:
        # A long-lived daemon keeps serving the code it started with, so an
        # upgraded install would otherwise be read as live without a signal.
        stdout.write(
            "daemon is stale: installed code is {}; restart the daemon or the "
            "service to load it\n".format(on_disk)
        )
    elif info.source_changed:
        stdout.write(
            "daemon is stale: the installed code changed since it started "
            "(still {}); restart the daemon or the service to load it\n".format(on_disk)
        )
    _report_http_info(info, client, stdout)
    stdout.flush()
    return 0


def _daemon_run(
    stderr: TextIO,
    http_port: Optional[int] = None,
    *,
    auto_archive: bool = False,
    auto_archive_after: Optional[float] = None,
) -> int:
    if auto_archive_after is not None and not auto_archive:
        stderr.write(_AUTO_ARCHIVE_ORPHAN_THRESHOLD)
        stderr.flush()
        return 2
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
        options: Dict[str, Any] = {}
        if http_port is not None:
            options["http_port"] = http_port
        if auto_archive:
            options["auto_archive"] = True
            if auto_archive_after is not None:
                options["auto_archive_after"] = auto_archive_after
        run_foreground(stop_event=stop_event, **options)
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
    if args.remote_python_candidates is not None:
        if not _accepts_keyword_argument(provider, "python_candidates"):
            raise TypeError(
                "remote watch provider does not accept python_candidates"
            )
        arguments["python_candidates"] = args.remote_python_candidates
    return provider(**arguments)


def _remote_watch_expected_hosts(session: object) -> Optional[set]:
    try:
        hosts = getattr(session, "hosts", None)
        if hosts is None or isinstance(hosts, (str, bytes, Mapping)):
            return None
        values = tuple(hosts)
    except Exception:
        return None
    if any(not isinstance(host, str) or not host for host in values):
        return None
    return {host.casefold() for host in values}


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
    remote_expected_hosts = None
    remote_startup_hosts = set()
    python_too_old_hosts = set()
    python_hint_reported = False

    def report_remote_python_hint_if_ready(*, force: bool = False) -> None:
        nonlocal python_hint_reported
        if python_hint_reported or not python_too_old_hosts:
            return
        if not force and (
            remote_expected_hosts is None
            or not remote_expected_hosts.issubset(remote_startup_hosts)
        ):
            return
        _report_remote_python_hint(
            len(python_too_old_hosts),
            explicit=args.remote_python_candidates is not None,
            stderr=stderr,
        )
        python_hint_reported = True

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

                remote_expected_hosts = _remote_watch_expected_hosts(
                    remote_session
                )
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
                        remote_startup_hosts.add(item.host.casefold())
                        report_remote_python_hint_if_ready()
                        continue
                    if isinstance(item, RemoteWatchFailure):
                        remote_state.failed = True
                        remote_state.failed_hosts.add(item.host)
                        _report_remote_watch_failure(item, stderr)
                        remote_startup_hosts.add(item.host.casefold())
                        if item.code == "python_too_old":
                            python_too_old_hosts.add(item.host.casefold())
                        report_remote_python_hint_if_ready()
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
                        remote_startup_hosts.add(item.host.casefold())
                        report_remote_python_hint_if_ready()
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
                try:
                    try:
                        report_remote_python_hint_if_ready(force=True)
                    except (BrokenPipeError, OSError, ValueError):
                        pass
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
    if getattr(error, "detail", None) == "namespace_moved":
        stderr.write(
            "send: audit namespace moved; run "
            "`agent-sidecar audit rebind --allow-write "
            "--confirm REBIND-SEND-AUDIT`\n"
        )
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
        payload = {"code": code}
        detail = getattr(error, "detail", None)
        if detail is not None:
            payload["detail"] = detail
        _write_json(payload, stdout)
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
        stderr.write(
            "send: remote rows from list --remote cannot be targeted by send\n"
        )
        stderr.flush()
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
    purge = bool(getattr(args, "purge", False))
    expected = "PURGE-SEND-AUDIT" if purge else "CLEAR-SEND-AUDIT"
    if (
        args.allow_write is not True
        or args.confirm != expected
    ):
        stderr.write(
            "audit reset: requires --allow-write and "
            "--confirm {}\n".format(expected)
        )
        stderr.flush()
        return 2
    operation = SendAuditStore().reset if resetter is None else resetter
    try:
        result = operation() if not purge else operation(purge=True)
    except AuditError as error:
        stderr.write(
            "audit reset: {}\n".format(sanitize_terminal_text(str(error)))
        )
        stderr.flush()
        return 1
    if purge:
        stdout.write("send audit purged\n")
    elif result:
        stdout.write("send audit reset; archived {}\n".format(result))
    else:
        stdout.write("send audit reset; no prior state\n")
    stdout.flush()
    if purge:
        stderr.write(
            "warning: send audit and all archived request-id idempotency "
            "history has been permanently deleted\n"
        )
    elif result:
        stderr.write(
            "send request-id idempotency history is retained in the archive\n"
        )
    else:
        stderr.write("send audit had no retained history\n")
    stderr.flush()
    return 0


def _run_audit_rebind(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    stderr: TextIO,
    rebinder=None,
) -> int:
    if (
        args.allow_write is not True
        or args.confirm != "REBIND-SEND-AUDIT"
    ):
        stderr.write(
            "audit rebind: requires --allow-write and "
            "--confirm REBIND-SEND-AUDIT\n"
        )
        stderr.flush()
        return 2
    operation = SendAuditStore().rebind if rebinder is None else rebinder
    try:
        operation()
    except AuditError as error:
        stderr.write(
            "audit rebind: {}\n".format(sanitize_terminal_text(str(error)))
        )
        stderr.flush()
        return 1
    stdout.write("send audit rebound\n")
    stdout.flush()
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


def _archive_statuses(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    return [name for name in value.split(",") if name.strip()]


def _print_archive_rows(rows: Sequence[object], stdout: TextIO) -> None:
    _print_sessions([(row, 0) for row in rows], stdout)


def _print_archive_entries(
    entries: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    stdout: TextIO,
) -> None:
    titles = {
        (str(row.get("agent", "")), str(row.get("session_id", ""))): row
        for row in sessions
        if isinstance(row, Mapping)
    }
    stdout.write(_ARCHIVE_LINE.format(
        agent="AGENT",
        session="SESSION",
        archived="ARCHIVED",
        reason="REASON",
        title="TITLE",
    ))
    for entry in entries:
        agent = str(entry.get("agent", ""))
        session_id = str(entry.get("session_id", ""))
        row = titles.get((agent, session_id), {})
        try:
            archived_at = float(entry.get("archived_at", 0.0))
        except (TypeError, ValueError):
            archived_at = 0.0
        stdout.write(_ARCHIVE_LINE.format(
            agent=sanitize_terminal_text(agent),
            session=sanitize_terminal_text(session_id)[:16],
            archived=_local_time(archived_at),
            reason=sanitize_terminal_text(str(entry.get("reason", ""))),
            title=sanitize_terminal_text(str(row.get("title", ""))),
        ))
    stdout.flush()


def _hide_archived(sessions: Sequence[object], stderr: TextIO) -> List[object]:
    try:
        return list(ArchiveStore().partition(sessions).visible)
    except ArchiveError as error:
        stderr.write("archive: {}; showing every session\n".format(error))
        stderr.flush()
        return list(sessions)


def _archive_targets(rows: Iterable[object]) -> List[Mapping[str, str]]:
    targets = []
    for row in rows:
        agent, session_id = session_target(row)
        targets.append({"agent": agent, "session_id": session_id})
    return targets


def _local_archive_candidates(
    args: argparse.Namespace,
    *,
    scanner: Optional[Scanner],
    idle_seconds: float,
    statuses: Optional[Sequence[str]],
    stderr: TextIO,
) -> List[object]:
    active_scanner = Scanner() if scanner is None else scanner
    sessions = _scan_sessions(active_scanner, stderr, None)
    store = ArchiveStore()
    visible = store.partition(sessions).visible
    return select_archivable(
        visible,
        idle_seconds=idle_seconds,
        statuses=statuses,
    )


def _run_archive_list(
    args: argparse.Namespace,
    *,
    client: SidecarClient,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        response = client.archive_list()
        entries = response.get("entries", [])
        rows = response.get("sessions", [])
    except (SidecarClientError, OSError):
        try:
            entries = [entry.to_dict() for entry in ArchiveStore().entries()]
        except ArchiveError as error:
            stderr.write("archive list: {}\n".format(error))
            stderr.flush()
            return 1
        rows = entries
    if args.json:
        _write_json({"entries": entries, "sessions": rows}, stdout)
        return 0
    if not entries:
        stdout.write("no archived sessions\n")
        stdout.flush()
        return 0
    _print_archive_entries(entries, rows, stdout)
    return 0


def _run_archive(
    args: argparse.Namespace,
    *,
    scanner: Optional[Scanner],
    client: SidecarClient,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.archive_action == "list":
        return _run_archive_list(
            args,
            client=client,
            stdout=stdout,
            stderr=stderr,
        )

    try:
        idle_seconds = parse_duration(args.idle_longer_than)
        statuses = normalize_statuses(_archive_statuses(args.status))
    except ArchiveError as error:
        stderr.write("archive: {}\n".format(error))
        stderr.flush()
        return 2

    token: Optional[str] = None
    try:
        preview = client.archive_preview(
            idle_seconds=idle_seconds,
            statuses=statuses,
        )
        candidates: List[object] = list(preview.get("candidates", []))
        token = preview.get("token")
    except (SidecarClientError, OSError):
        candidates = _local_archive_candidates(
            args,
            scanner=scanner,
            idle_seconds=idle_seconds,
            statuses=statuses,
            stderr=stderr,
        )

    try:
        targets = _archive_targets(candidates)
    except ArchiveError as error:
        stderr.write("archive: {}\n".format(error))
        stderr.flush()
        return 1

    applied = bool(targets) and args.yes and not args.dry_run
    if applied:
        try:
            if token is not None:
                result = client.archive_apply(targets, token=token)
                archived_count = int(result.get("count", 0))
            else:
                archived_count = len(
                    ArchiveStore().archive(candidates, reason="batch")
                )
        except (ArchiveError, SidecarClientError, OSError) as error:
            stderr.write("archive: {}\n".format(error))
            stderr.flush()
            return 1
    else:
        archived_count = 0

    if args.json:
        _write_json(
            {
                "idle_seconds": idle_seconds,
                "statuses": list(statuses),
                "candidates": [dict(_as_dict(row)) for row in candidates],
                "count": len(candidates),
                "applied": applied,
                "archived": archived_count,
            },
            stdout,
        )
        return 0

    if not candidates:
        stdout.write("no sessions idle longer than the threshold\n")
        stdout.flush()
        return 0
    _print_archive_rows(candidates, stdout)
    if applied:
        # "on this host" is the whole scope story: there is no fleet-wide
        # archive, and the same command over ssh is what covers a pod.
        stdout.write(
            "archived {} session(s) on this host\n".format(archived_count)
        )
    elif args.dry_run:
        stdout.write(
            "dry run: {} session(s) would be archived\n".format(len(candidates))
        )
    else:
        stdout.write(
            "{} session(s) selected; rerun with --yes to archive\n".format(
                len(candidates)
            )
        )
    stdout.flush()
    return 0


def _resolve_archived_target(
    prefix: str,
    entries: Sequence[Mapping[str, Any]],
    stderr: TextIO,
) -> Optional[Mapping[str, str]]:
    matches = [
        entry
        for entry in entries
        if str(entry.get("session_id", "")).startswith(prefix)
    ]
    if not matches:
        stderr.write("unarchive: no archived session matches {}\n".format(prefix))
        stderr.flush()
        return None
    if len(matches) > 1:
        stderr.write(
            "unarchive: {} archived sessions match {}\n".format(
                len(matches),
                prefix,
            )
        )
        stderr.flush()
        return None
    entry = matches[0]
    return {
        "agent": str(entry.get("agent", "")),
        "session_id": str(entry.get("session_id", "")),
    }


def _run_unarchive(
    args: argparse.Namespace,
    *,
    client: SidecarClient,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if not args.all and not args.session_prefix:
        stderr.write("unarchive: provide a session prefix or --all\n")
        stderr.flush()
        return 2
    if args.all and args.session_prefix:
        stderr.write("unarchive: session prefix and --all are exclusive\n")
        stderr.flush()
        return 2

    daemon_available = True
    try:
        entries = list(client.archive_list().get("entries", []))
    except (SidecarClientError, OSError):
        daemon_available = False
        try:
            entries = [entry.to_dict() for entry in ArchiveStore().entries()]
        except ArchiveError as error:
            stderr.write("unarchive: {}\n".format(error))
            stderr.flush()
            return 1

    if args.all:
        targets = [
            {
                "agent": str(entry.get("agent", "")),
                "session_id": str(entry.get("session_id", "")),
            }
            for entry in entries
        ]
    else:
        resolved = _resolve_archived_target(
            args.session_prefix,
            entries,
            stderr,
        )
        if resolved is None:
            return 1
        targets = [resolved]

    if not targets:
        if args.json:
            _write_json({"released": [], "count": 0}, stdout)
        else:
            stdout.write("no archived sessions\n")
            stdout.flush()
        return 0

    try:
        if daemon_available:
            result = client.unarchive(targets)
            released = list(result.get("released", []))
        else:
            released = [
                {"agent": agent, "session_id": session_id}
                for agent, session_id in ArchiveStore().unarchive(
                    [(target["agent"], target["session_id"]) for target in targets]
                )
            ]
    except (ArchiveError, SidecarClientError, OSError) as error:
        stderr.write("unarchive: {}\n".format(error))
        stderr.flush()
        return 1

    if args.json:
        _write_json({"released": released, "count": len(released)}, stdout)
        return 0
    stdout.write(
        "released {} session(s) on this host\n".format(len(released))
    )
    stdout.flush()
    return 0


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
    audit_rebinder=None,
    service_installer=None,
    service_uninstaller=None,
    service_status_provider=None,
) -> int:
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    args = build_parser().parse_args(argv)

    if (
        args.command in ("list", "status", "watch", "cluster")
        and args.remote_python is not None
        and not args.remote
    ):
        errors.write(
            "{}: --remote-python requires --remote\n".format(args.command)
        )
        errors.flush()
        return 2

    args.remote_python_candidates = None
    if (
        args.command in ("list", "status", "watch", "cluster")
        and args.remote
    ):
        remote_python = args.remote_python
        if remote_python is None and REMOTE_PYTHON_ENV in os.environ:
            remote_python = os.environ[REMOTE_PYTHON_ENV]
        if remote_python is not None:
            try:
                validated_remote_python = (
                    validate_remote_python_executable(remote_python)
                )
            except ValueError:
                errors.write(
                    "{}: --remote-python/{} must be a valid absolute "
                    "executable path\n".format(
                        args.command,
                        REMOTE_PYTHON_ENV,
                    )
                )
                errors.flush()
                return 2
            args.remote_python_candidates = (validated_remote_python,)

    if (
        args.command in ("list", "status", "cluster")
        and args.host
        and not args.remote
    ):
        errors.write("{}: --host requires --remote\n".format(args.command))
        errors.flush()
        return 2

    if args.command == "list" and args.archived and args.remote:
        errors.write(_local_only_error("list", ("archive", "list")))
        errors.flush()
        return 2

    if args.command in ("archive", "unarchive") and (args.remote or args.host):
        errors.write(_local_only_error(args.command, _local_only_argv(args)))
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
        if args.audit_command == "reset":
            return _run_audit_reset(
                args,
                stdout=output,
                stderr=errors,
                resetter=audit_resetter,
            )
        return _run_audit_rebind(
            args,
            stdout=output,
            stderr=errors,
            rebinder=audit_rebinder,
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
                auto_archive=args.auto_archive,
                auto_archive_after=args.auto_archive_after,
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
            auto_archive=args.auto_archive,
            auto_archive_after=args.auto_archive_after,
        )

    if args.command == "tui":
        runner = run_tui if tui_runner is None else tui_runner
        return runner(
            scanner=scanner,
            client=_TailReportingClient(active_client, errors),
            stdout=output,
            once=args.once,
        )

    if args.command == "cluster":
        return _run_cluster(
            args,
            scanner=scanner,
            client=active_client,
            stdout=output,
            stderr=errors,
            remote_aggregator=remote_aggregator,
        )

    if args.command == "archive":
        return _run_archive(
            args,
            scanner=scanner,
            client=active_client,
            stdout=output,
            stderr=errors,
        )

    if args.command == "unarchive":
        return _run_unarchive(
            args,
            client=active_client,
            stdout=output,
            stderr=errors,
        )

    if args.command in ("list", "status"):
        if args.command == "list" and args.archived:
            return _run_archive_list(
                args,
                client=active_client,
                stdout=output,
                stderr=errors,
            )
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
