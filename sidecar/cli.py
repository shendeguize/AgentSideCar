"""Command-line interface for sidecar observation and local message delivery."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Optional, Sequence, TextIO

import sidecar
from sidecar.adapters.base import sanitize_terminal_text
from sidecar.client import SidecarClient, SidecarClientError
from sidecar.daemon import (
    PIDFILE_NAME,
    DaemonAlreadyRunning,
    DaemonError,
    default_runtime_dir,
    read_pid,
    run_foreground,
)
from sidecar.inject import (
    DEFAULT_SEND_TIMEOUT_SECONDS,
    SendError,
    SendResult,
    build_send_plan,
    execute_send,
)
from sidecar.model import Session, Status
from sidecar.presentation import row_age, row_value
from sidecar.process import running_agent_processes
from sidecar.scan import ScanError, Scanner
from sidecar.tail import watch_sessions
from sidecar.tui import arrange_session_tree, run_tui

RECENT_SECONDS = 48.0 * 60.0 * 60.0
DAEMON_START_TIMEOUT = 5.0
DAEMON_STOP_TIMEOUT = 5.0
DAEMON_POLL_INTERVAL = 0.1
_SESSION_LINE = (
    "{branch}{agent:<11} {status:<7} {session:<16} {age:>4}"
    "  {updated:<25}  {title}\n"
)
_REMOTE_SESSION_LINE = (
    "{branch}{host:<16} {agent:<11} {status:<7} {session:<16} {age:>4}"
    "  {updated:<25}  {title}\n"
)


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
    list_parser.add_argument("--all", action="store_true", help="include sessions older than 48h")
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

    send_parser = commands.add_parser(
        "send",
        help="start a local headless resume that may modify agent state",
        description="Starts a local headless resume and may modify agent state.",
        allow_abbrev=False,
    )
    send_parser.add_argument(
        "session_prefix",
        metavar="session-prefix",
        help="exact session ID or unique prefix",
    )
    send_parser.add_argument("message", help="message delivered to the resumed agent")
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
        "--json",
        action="store_true",
        help="emit the bounded delivery result as JSON",
    )

    daemon_parser = commands.add_parser("daemon", help="manage the local sidecar daemon")
    daemon_commands = daemon_parser.add_subparsers(dest="daemon_command", required=True)
    daemon_commands.add_parser("start", help="start the daemon in the background")
    daemon_commands.add_parser("stop", help="stop the verified running daemon")
    daemon_commands.add_parser("status", help="report whether the daemon is running")
    daemon_commands.add_parser("run", help=argparse.SUPPRESS)

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


def _human_session_sort_key(session: object) -> tuple:
    status_priority = {
        Status.WORKING.value: 0,
        Status.WAITING.value: 1,
        Status.IDLE.value: 2,
        Status.DEAD.value: 3,
    }
    try:
        updated_at = float(row_value(session, "updated_at", 0.0))
    except (TypeError, ValueError):
        updated_at = 0.0
    return (
        status_priority.get(_status_value(session), 4),
        -updated_at,
        str(row_value(session, "host") or "").casefold(),
        str(row_value(session, "agent") or ""),
        str(row_value(session, "session_id") or ""),
    )


def _display_title(session: object, depth: int) -> str:
    title = _session_title(session)
    extra = row_value(session, "extra", {})
    if depth == 0 and isinstance(extra, Mapping) and extra.get("sidechain") is True:
        return "[sidechain] {}".format(title)
    return title


def _print_sessions(
    sessions: Iterable[object],
    stdout: TextIO,
    *,
    header: bool = False,
    status_priority: bool = False,
    show_host: bool = False,
) -> None:
    values = list(sessions)
    if status_priority:
        ordered = []
        known_statuses = (
            Status.WORKING.value,
            Status.WAITING.value,
            Status.IDLE.value,
            Status.DEAD.value,
        )
        for status in known_statuses:
            ordered.extend(
                arrange_session_tree(
                    (row for row in values if _status_value(row) == status),
                    sort_key=_human_session_sort_key,
                )
            )
        ordered.extend(
            arrange_session_tree(
                (row for row in values if _status_value(row) not in known_statuses),
                sort_key=_human_session_sort_key,
            )
        )
    else:
        ordered = arrange_session_tree(values)
    line_format = _REMOTE_SESSION_LINE if show_host else _SESSION_LINE
    if header:
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
    for session, depth in ordered:
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
) -> Optional[List[Mapping[str, Any]]]:
    try:
        rows = [dict(row) for row in client.status()]
    except (SidecarClientError, OSError):
        return None
    _report_scan_errors(getattr(client, "scan_errors", ()) or (), stderr)
    return rows


def _recent_rows(
    rows: Iterable[Mapping[str, Any]],
    seconds: Optional[float],
    now: Optional[float] = None,
) -> List[Mapping[str, Any]]:
    if seconds is None:
        return list(rows)
    current = time.time() if now is None else now
    recent: List[Mapping[str, Any]] = []
    for row in rows:
        try:
            updated_at = float(row.get("updated_at", 0.0))
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


def _remote_row_sort_key(command: str, row: object) -> tuple:
    try:
        updated_at = float(row_value(row, "updated_at", 0.0))
    except (TypeError, ValueError):
        updated_at = 0.0
    identity = (
        -updated_at,
        str(row_value(row, "host") or "").casefold(),
        str(row_value(row, "host") or ""),
        str(row_value(row, "agent") or "").casefold(),
        str(row_value(row, "session_id") or ""),
    )
    if command == "status":
        status_priority = {
            Status.WORKING.value: 0,
            Status.WAITING.value: 1,
        }
        return (status_priority.get(_status_value(row), 2),) + identity
    return identity


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
    daemon_rows = _client_status(client, stderr)
    if daemon_rows is None:
        active_scanner = Scanner() if scanner is None else scanner
        local_values: List[object] = _scan_sessions(
            active_scanner,
            stderr,
            None,
        )
    else:
        local_values = list(daemon_rows)
    local_rows: List[Mapping[str, Any]] = []
    for session in local_values:
        row = dict(_as_dict(session))
        row["host"] = "local"
        local_rows.append(row)

    result: Optional[object] = None
    control_error: Optional[str] = None
    try:
        result = provider(command, selected=args.host or None)
    except RemoteInventoryError:
        control_error = "remote: inventory\n"
    except OSError:
        control_error = "remote: setup\n"
    except ValueError as error:
        control_error = "remote: {}\n".format(sanitize_terminal_text(error))

    remote_rows: List[Mapping[str, Any]] = []
    if result is not None:
        failures = _aggregate_value(result, "failures", ())
        _report_remote_failures(
            failures if isinstance(failures, Iterable) else (),
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

    merged: List[object] = list(local_rows) + remote_rows
    if command == "list":
        recent = None if args.all else RECENT_SECONDS
        merged = list(_recent_rows(merged, recent))
        merged = _filter_agent_rows(merged, args.agent)
    else:
        merged = [
            row
            for row in merged
            if _status_value(row)
            in (Status.WORKING.value, Status.WAITING.value)
        ]
    merged.sort(key=lambda row: _remote_row_sort_key(command, row))

    if control_error is not None:
        stderr.write(control_error)
        stderr.flush()

    if args.json:
        _write_json([dict(_as_dict(row)) for row in merged], stdout)
    elif command == "status" and not merged:
        stdout.write("no active sessions\n")
        stdout.flush()
    else:
        _print_sessions(
            merged,
            stdout,
            header=True,
            status_priority=command == "status",
            show_host=True,
        )

    if control_error is not None:
        return 2
    try:
        exit_code = int(_aggregate_value(result, "exit_code", EXIT_NO_SUCCESS))
    except (TypeError, ValueError):
        exit_code = EXIT_NO_SUCCESS
    return 0 if exit_code == 0 else EXIT_NO_SUCCESS


def _ping_pid(client: SidecarClient) -> int:
    response = client.ping()
    if not isinstance(response, Mapping) or response.get("ok") is not True:
        raise SidecarClientError("daemon returned an invalid ping", code="invalid_response")
    pid = response.get("pid")
    if isinstance(pid, bool):
        raise SidecarClientError("daemon returned an invalid pid", code="invalid_response")
    try:
        parsed = int(pid)
    except (TypeError, ValueError) as error:
        raise SidecarClientError(
            "daemon returned an invalid pid",
            code="invalid_response",
        ) from error
    if parsed <= 0:
        raise SidecarClientError("daemon returned an invalid pid", code="invalid_response")
    return parsed


def _runtime_dir_for(client: object) -> Path:
    socket_path = getattr(client, "socket_path", None)
    if socket_path is not None:
        return Path(socket_path).expanduser().parent
    return default_runtime_dir()


def _cleanup_verified_pidfile(runtime_dir: Path, pid: int) -> None:
    if read_pid(runtime_dir) != pid:
        return
    path = runtime_dir / PIDFILE_NAME
    try:
        path.unlink()
    except OSError:
        pass


def _daemon_start(client: SidecarClient, stdout: TextIO, stderr: TextIO) -> int:
    try:
        pid = _ping_pid(client)
    except (SidecarClientError, OSError):
        pid = None
    if pid is not None:
        stdout.write("daemon already running (pid {})\n".format(pid))
        stdout.flush()
        return 0

    command = [sys.executable, "-m", "sidecar", "daemon", "run"]
    try:
        process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parent.parent),
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

    deadline = time.monotonic() + DAEMON_START_TIMEOUT
    while time.monotonic() < deadline:
        try:
            pid = _ping_pid(client)
        except (SidecarClientError, OSError):
            if process.poll() is not None:
                break
            time.sleep(DAEMON_POLL_INTERVAL)
            continue
        stdout.write("daemon started (pid {})\n".format(pid))
        stdout.flush()
        return 0

    stderr.write("daemon start: daemon did not become ready\n")
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
            _cleanup_verified_pidfile(runtime_dir, socket_pid)
            stdout.write("daemon stopped\n")
            stdout.flush()
            return 0
        if current_pid != socket_pid:
            _cleanup_verified_pidfile(runtime_dir, socket_pid)
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
        pid = _ping_pid(client)
    except (SidecarClientError, OSError):
        stdout.write("daemon is not running\n")
        stdout.flush()
        return 1
    stdout.write("daemon is running (pid {})\n".format(pid))
    stdout.flush()
    return 0


def _daemon_run(stderr: TextIO) -> int:
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
        run_foreground(stop_event=stop_event)
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


def _valid_unicode_scalars(text: str) -> str:
    return "".join(
        "\ufffd" if 0xD800 <= ord(character) <= 0xDFFF else character
        for character in text
    )


def _safe_send_text(value: object, message: str) -> str:
    text = str(value or "")
    message_forms = {message, _valid_unicode_scalars(message)}
    candidates = set(message_forms)
    for message_form in message_forms:
        for ensure_ascii in (False, True):
            encoded = json.dumps(message_form, ensure_ascii=ensure_ascii)
            candidates.add(encoded[1:-1])
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            text = text.replace(candidate, "[message redacted]")
    valid_text = _valid_unicode_scalars(text)
    return sanitize_terminal_text(valid_text)


def _report_send_error(error: object, message: str, stderr: TextIO) -> None:
    stderr.write("send: {}\n".format(_safe_send_text(error, message)))
    stderr.flush()


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
    if as_json:
        _write_json(result.to_dict(), stdout)
    elif result.outcome == "completed":
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
        stdout.flush()
    else:
        stdout.write(
            "delivery unknown for {}:{} ({})\n".format(
                sanitize_terminal_text(result.agent),
                sanitize_terminal_text(result.session_id),
                sanitize_terminal_text(result.outcome),
            )
        )
        stdout.flush()

    if result.outcome == "completed":
        if not as_json:
            _report_native_send_stderr(result, message, stderr)
        return 0

    diagnostic = "delivery status is unknown after {}".format(
        sanitize_terminal_text(result.outcome)
    )
    if result.error_code:
        diagnostic += " ({})".format(sanitize_terminal_text(result.error_code))
    stderr.write("send: {}\n".format(diagnostic))
    stderr.flush()
    _report_native_send_stderr(result, message, stderr)
    return 1


def _run_send(
    args: argparse.Namespace,
    *,
    scanner: Optional[Scanner],
    stdout: TextIO,
    stderr: TextIO,
    planner=None,
    executor=None,
) -> int:
    if args.allow_write is not True:
        stderr.write("send: explicit --allow-write is required\n")
        stderr.flush()
        return 2

    active_scanner = Scanner() if scanner is None else scanner
    sessions = _scan_sessions(active_scanner, stderr, None)
    try:
        selected = resolve_session_prefix(sessions, args.session_prefix)
    except (LookupError, ValueError) as error:
        _report_send_error(error, args.message, stderr)
        return 2

    plan_builder = build_send_plan if planner is None else planner
    send_executor = execute_send if executor is None else executor
    try:
        plan = plan_builder(selected, args.message)
        result = send_executor(
            plan,
            allow_write=args.allow_write,
            timeout=args.timeout,
        )
    except SendError as error:
        _report_send_error(error, args.message, stderr)
        return 2
    except KeyboardInterrupt:
        stderr.write("send: interrupted; delivery status is unknown\n")
        stderr.flush()
        return 130
    return _write_send_result(
        result,
        as_json=args.json,
        message=args.message,
        stdout=stdout,
        stderr=stderr,
    )


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    scanner: Optional[Scanner] = None,
    client: Optional[SidecarClient] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    process_provider=None,
    watch_provider=None,
    tui_runner=None,
    remote_aggregator=None,
    send_planner=None,
    send_executor=None,
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

    if args.command == "send":
        return _run_send(
            args,
            scanner=scanner,
            stdout=output,
            stderr=errors,
            planner=send_planner,
            executor=send_executor,
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
            return _daemon_start(active_client, output, errors)
        if args.daemon_command == "stop":
            return _daemon_stop(active_client, output, errors)
        if args.daemon_command == "status":
            return _daemon_status(active_client, output)
        return _daemon_run(errors)

    if args.command == "tui":
        runner = run_tui if tui_runner is None else tui_runner
        return runner(
            scanner=scanner,
            client=active_client,
            stdout=output,
            once=args.once,
        )

    if args.command == "list":
        if args.remote:
            return _run_remote_snapshot(
                "list",
                args,
                scanner=scanner,
                client=active_client,
                stdout=output,
                stderr=errors,
                remote_aggregator=remote_aggregator,
            )
        recent = None if args.all else RECENT_SECONDS
        daemon_rows = _client_status(active_client, errors)
        if daemon_rows is None:
            active_scanner = Scanner() if scanner is None else scanner
            sessions: List[object] = _scan_sessions(active_scanner, errors, recent)
        else:
            sessions = list(_recent_rows(daemon_rows, recent))
        sessions = _filter_agent_rows(sessions, args.agent)
        if args.json:
            _write_json([_as_dict(session) for session in sessions], output)
        else:
            _print_sessions(sessions, output, header=True)
        return 0

    if args.command == "status":
        if args.remote:
            return _run_remote_snapshot(
                "status",
                args,
                scanner=scanner,
                client=active_client,
                stdout=output,
                stderr=errors,
                remote_aggregator=remote_aggregator,
            )
        daemon_rows = _client_status(active_client, errors)
        if daemon_rows is None:
            active_scanner = Scanner() if scanner is None else scanner
            sessions = list(_scan_sessions(active_scanner, errors, None))
        else:
            sessions = list(daemon_rows)
        active = [
            session
            for session in sessions
            if _status_value(session) not in (Status.IDLE.value, Status.DEAD.value)
        ]
        if args.json:
            _write_json([_as_dict(session) for session in active], output)
        elif not active:
            output.write("no active sessions\n")
            output.flush()
        else:
            _print_sessions(active, output, status_priority=True)
        return 0

    if args.all and args.session_prefix:
        errors.write("watch: session prefix and --all are mutually exclusive\n")
        errors.flush()
        return 2
    if not args.all and not args.session_prefix:
        errors.write("watch: provide a session prefix or --all\n")
        errors.flush()
        return 2

    provider = watch_sessions if watch_provider is None else watch_provider
    if args.all and not args.from_start:
        try:
            for event in active_client.subscribe():
                _write_event(event, args.json, output)
            return 0
        except KeyboardInterrupt:
            return 130
        except (SidecarClientError, OSError):
            pass

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
        for event in provider(selected, from_start=args.from_start):
            _write_event(event, args.json, output)
    except KeyboardInterrupt:
        return 130
    return 0


__all__ = [
    "DAEMON_POLL_INTERVAL",
    "DAEMON_START_TIMEOUT",
    "DAEMON_STOP_TIMEOUT",
    "RECENT_SECONDS",
    "build_parser",
    "main",
    "resolve_session_prefix",
]
