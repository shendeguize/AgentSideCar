"""Zero-dependency ANSI terminal dashboard for sidecar session snapshots."""

from __future__ import annotations

import json
import os
import select
import shutil
import sys
import time
import unicodedata
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    TextIO,
    Tuple,
)

from sidecar.adapters.base import sanitize_terminal_text
from sidecar.client import SidecarClient, SidecarClientError
from sidecar.model import Status
from sidecar.presentation import row_age, row_value
from sidecar.scan import Scanner

ENTER_SCREEN = "\x1b[?1049h\x1b[?25l"
LEAVE_SCREEN = "\x1b[?25h\x1b[?1049l"
CLEAR_SCREEN = "\x1b[H\x1b[2J"
DEFAULT_REFRESH_INTERVAL = 1.0
DEFAULT_FALLBACK_SCAN_INTERVALS = (2.0, 5.0, 10.0)
DEFAULT_TERMINAL_WIDTH = 100
DEFAULT_TERMINAL_HEIGHT = 24
SOURCE_DAEMON = "daemon"
SOURCE_DIRECT = "direct"

_GROUPS: Sequence[Tuple[str, str]] = (
    (Status.WORKING.value, "WORKING"),
    (Status.WAITING.value, "WAITING"),
    (Status.IDLE.value, "IDLE"),
)
_LATEST_KEYS = (
    "latest",
    "last_message",
    "last_agent_message",
    "last_turn_reason",
    "current_command",
    "last_command",
    "summary",
    "message",
    "task",
    "plan",
)
_METADATA_KEYS = {
    "source",
    "store",
    "state",
    "index",
    "session_dir",
    "status_db",
    "workspace",
    "cwd_hash",
    "project_slug",
    "sidechain",
    "terminals_root",
}
TREE_MAX_DEPTH = 6


def _status(row: object) -> str:
    value = row_value(row, "status")
    return value.value if isinstance(value, Status) else str(value or "")


def _snapshot_fingerprint(rows: Iterable[object]) -> Tuple[str, ...]:
    fingerprints: List[str] = []
    for row in rows:
        if isinstance(row, Mapping):
            payload: Any = dict(row)
        else:
            serializer = getattr(row, "to_dict", None)
            try:
                payload = serializer() if callable(serializer) else None
            except Exception:
                payload = None
        if not isinstance(payload, Mapping):
            payload = {
                key: row_value(row, key, None)
                for key in (
                    "agent",
                    "session_id",
                    "project",
                    "transcript",
                    "updated_at",
                    "title",
                    "status",
                    "extra",
                    "parent_id",
                )
            }
        fingerprints.append(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                default=str,
            )
        )
    return tuple(sorted(fingerprints))


class FallbackScanBackoff:
    """Schedule costly direct scans while leaving the render loop unblocked."""

    def __init__(
        self,
        intervals: Sequence[float] = DEFAULT_FALLBACK_SCAN_INTERVALS,
    ) -> None:
        values = tuple(float(interval) for interval in intervals)
        if not values or any(interval <= 0 for interval in values):
            raise ValueError("fallback scan intervals must be positive")
        if any(current < previous for previous, current in zip(values, values[1:])):
            raise ValueError("fallback scan intervals must be nondecreasing")
        self.intervals = values
        self.reset()

    @property
    def next_scan_at(self) -> Optional[float]:
        return self._next_scan_at

    @property
    def current_interval(self) -> float:
        return self.intervals[self._interval_index]

    def reset(self) -> None:
        self._fingerprint: Optional[Tuple[str, ...]] = None
        self._interval_index = 0
        self._next_scan_at: Optional[float] = None

    def due(self, now: float) -> bool:
        return self._next_scan_at is None or now >= self._next_scan_at

    def record_scan(self, rows: Sequence[object], now: float) -> None:
        fingerprint = _snapshot_fingerprint(rows)
        active = any(
            _status(row) in (Status.WORKING.value, Status.WAITING.value)
            for row in rows
        )
        changed = self._fingerprint is not None and fingerprint != self._fingerprint
        if self._fingerprint is None or changed or active:
            self._interval_index = 0
        else:
            self._interval_index = min(
                len(self.intervals) - 1,
                self._interval_index + 1,
            )
        self._fingerprint = fingerprint
        self._next_scan_at = float(now) + self.current_interval


class SnapshotPoller:
    """Prefer daemon snapshots and adaptively gate direct fallback scans."""

    def __init__(
        self,
        client: SidecarClient,
        scanner: Scanner,
        *,
        fallback_intervals: Sequence[float] = DEFAULT_FALLBACK_SCAN_INTERVALS,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.client = client
        self.scanner = scanner
        self.clock = time.monotonic if clock is None else clock
        self.backoff = FallbackScanBackoff(fallback_intervals)
        self.rows: List[object] = []
        self.source = SOURCE_DAEMON

    def poll(self, now: Optional[float] = None) -> Tuple[List[object], str]:
        current = self.clock() if now is None else float(now)
        try:
            rows = list(self.client.status())
        except (SidecarClientError, OSError):
            self.source = SOURCE_DIRECT
            if self.backoff.due(current):
                try:
                    rows = list(self.scanner.scan())
                except Exception:
                    rows = []
                completed_at = self.clock() if now is None else current
                self.rows = rows
                self.backoff.record_scan(rows, completed_at)
            return list(self.rows), self.source

        self.rows = rows
        self.source = SOURCE_DAEMON
        self.backoff.reset()
        return list(self.rows), self.source


def _fit(value: Any, width: int) -> str:
    if width <= 0:
        return ""
    text = sanitize_terminal_text(
        value,
        collapse_whitespace=False,
    )
    widths = [
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in ("F", "W")
        else 1
        for character in text
    ]
    if sum(widths) <= width:
        return text
    if width == 1:
        return "…"
    fitted: List[str] = []
    used = 0
    for character, character_width in zip(text, widths):
        if used + character_width > width - 1:
            break
        fitted.append(character)
        used += character_width
    return "".join(fitted) + "…"


def _updated_at(row: object) -> float:
    try:
        return float(row_value(row, "updated_at", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _render_extra(value: Any) -> str:
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=isinstance(value, Mapping),
            default=str,
        )
    return str(value or "")


def _latest(row: object) -> str:
    extra = row_value(row, "extra", {})
    if not isinstance(extra, Mapping):
        return ""
    for key in _LATEST_KEYS:
        value = extra.get(key)
        if value not in (None, "", [], {}):
            return "{}={}".format(key, _render_extra(value))
    for key in sorted(extra):
        value = extra[key]
        if key in _METADATA_KEYS or value in (None, "", False, [], {}):
            continue
        return "{}={}".format(key, _render_extra(value))
    return ""


def _sort_key(row: object) -> Tuple[float, str, str, str]:
    return (
        -_updated_at(row),
        str(row_value(row, "host") or "").casefold(),
        str(row_value(row, "agent")),
        str(row_value(row, "session_id")),
    )


def arrange_session_tree(
    rows: Iterable[object],
    *,
    sort_key: Optional[Callable[[object], Tuple[Any, ...]]] = None,
    max_depth: int = TREE_MAX_DEPTH,
) -> List[Tuple[object, int]]:
    """Order one same-priority group as a bounded, non-recursive tree."""

    if max_depth < 0:
        raise ValueError("max_depth must be non-negative")
    values = list(rows)
    identities = [
        (
            str(row_value(row, "host") or ""),
            str(row_value(row, "agent") or ""),
            str(row_value(row, "session_id") or ""),
        )
        for row in values
    ]
    candidates: Dict[Tuple[str, str, str], List[int]] = {}
    for index, identity in enumerate(identities):
        candidates.setdefault(identity, []).append(index)

    parents: List[Optional[int]] = [None] * len(values)
    for index, row in enumerate(values):
        parent_id = row_value(row, "parent_id", None)
        if not isinstance(parent_id, str) or not parent_id:
            continue
        matches = candidates.get(
            (identities[index][0], identities[index][1], parent_id),
            (),
        )
        if len(matches) == 1 and matches[0] != index:
            parents[index] = matches[0]

    # A parent pointer graph has at most one outgoing edge per node. Break
    # every edge inside a cycle so all cyclic rows remain visible as roots.
    state = [0] * len(values)
    for start in range(len(values)):
        if state[start]:
            continue
        trail: List[int] = []
        positions: Dict[int, int] = {}
        current: Optional[int] = start
        while current is not None and state[current] == 0:
            positions[current] = len(trail)
            trail.append(current)
            state[current] = 1
            current = parents[current]
        if current is not None and state[current] == 1:
            cycle_start = positions.get(current)
            if cycle_start is not None:
                for cycle_index in trail[cycle_start:]:
                    parents[cycle_index] = None
        for trail_index in trail:
            state[trail_index] = 2

    children: Dict[int, List[int]] = {}
    roots: List[int] = []
    for index, parent in enumerate(parents):
        if parent is None:
            roots.append(index)
        else:
            children.setdefault(parent, []).append(index)

    key = _sort_key if sort_key is None else sort_key

    def indexed_key(index: int) -> Tuple[Any, ...]:
        return (
            key(values[index]),
            identities[index],
            str(row_value(values[index], "transcript") or ""),
            index,
        )

    roots.sort(key=indexed_key)
    for child_rows in children.values():
        child_rows.sort(key=indexed_key)

    ordered: List[Tuple[object, int]] = []
    stack = [(index, 0) for index in reversed(roots)]
    while stack:
        index, depth = stack.pop()
        ordered.append((values[index], depth))
        next_depth = min(max_depth, depth + 1)
        stack.extend(
            (child, next_depth)
            for child in reversed(children.get(index, ()))
        )
    return ordered


def _sidechain_marker(row: object, depth: int) -> str:
    extra = row_value(row, "extra", {})
    if depth == 0 and isinstance(extra, Mapping) and extra.get("sidechain") is True:
        return "[sidechain] "
    return ""


def _session_line(row: object, now: float, columns: int, depth: int = 0) -> str:
    title = row_value(row, "title") or "(untitled)"
    project = sanitize_terminal_text(
        row_value(row, "project") or "(unknown)"
    )
    latest = _latest(row) or "(none)"
    branch = "  "
    if depth:
        branch += "{}↳ ".format("  " * min(depth, TREE_MAX_DEPTH))
    summary = (
        "{branch}{agent}  {session_id}  {age}  {marker}{title}"
        "  |  project: {project}  |  latest: {latest}"
    ).format(
        branch=branch,
        agent=sanitize_terminal_text(row_value(row, "agent") or "?"),
        session_id=sanitize_terminal_text(
            row_value(row, "session_id") or "?"
        ),
        age=sanitize_terminal_text(row_age(row, now, default="?")),
        marker=_sidechain_marker(row, depth),
        title=sanitize_terminal_text(title),
        project=project,
        latest=sanitize_terminal_text(latest),
    )
    return _fit(summary, columns)


def render_snapshot(
    rows: Iterable[object],
    width: int,
    now: float,
    height: int = DEFAULT_TERMINAL_HEIGHT,
    *,
    source: str = SOURCE_DAEMON,
    interactive: bool = True,
) -> str:
    """Render a deterministic, ANSI-free, viewport-bounded snapshot."""

    columns = max(1, int(width))
    line_limit = max(0, int(height))
    if line_limit == 0:
        return ""
    grouped: Dict[str, List[object]] = {
        status: [] for status, _heading in _GROUPS
    }
    for row in rows:
        status = _status(row)
        if status in grouped:
            grouped[status].append(row)

    visible = sum(len(grouped[status]) for status, _heading in _GROUPS)
    if source == SOURCE_DAEMON:
        source_text = "source: daemon/connected"
    elif source == SOURCE_DIRECT:
        source_text = (
            "source: direct/offline; run: agent-sidecar daemon start"
        )
    else:
        source_text = "source: {}".format(sanitize_terminal_text(source))
    header_parts = [
        "Agent Sidecar  {} session{}".format(
            visible,
            "" if visible == 1 else "s",
        ),
        source_text,
    ]
    if interactive:
        header_parts.append("q quit")
    header = _fit(
        "  |  ".join(header_parts),
        columns,
    )
    lines = [header]
    active_sections: List[Tuple[str, List[Tuple[object, int]]]] = []
    for status, heading in _GROUPS[:2]:
        sessions = arrange_session_tree(grouped[status])
        if sessions:
            active_sections.append((heading, sessions))

    full_active_size = 1 + sum(
        1 + len(sessions) for _heading, sessions in active_sections
    )
    if full_active_size > line_limit:
        indexed_sections = list(enumerate(active_sections))
        priority_order = sorted(
            indexed_sections,
            key=lambda item: (
                0 if item[1][0] == "WAITING" else 1,
                _sort_key(item[1][1][0][0]),
            ),
        )
        available = max(0, line_limit - len(lines))
        if available >= 2 * len(active_sections):
            selected = {index for index, _section in indexed_sections}
        else:
            complete_sections = min(
                len(active_sections),
                available // 2,
            )
            selected = {
                index
                for index, _section in priority_order[:complete_sections]
            }
            if not selected and available:
                selected.add(priority_order[0][0])

        allocations = {index: 0 for index in selected}
        row_capacity = max(0, available - len(selected))
        for index, (_heading, sessions) in priority_order:
            if index not in selected or row_capacity <= 0:
                continue
            allocations[index] = 1
            row_capacity -= 1

        total_active = sum(
            len(sessions) for _heading, sessions in active_sections
        )
        allocated = sum(allocations.values())
        show_hidden = allocated < total_active and row_capacity > 0
        if show_hidden:
            row_capacity -= 1

        for index, (_heading, sessions) in priority_order:
            if index not in selected or row_capacity <= 0:
                continue
            additional = min(
                row_capacity,
                len(sessions) - allocations[index],
            )
            allocations[index] += additional
            row_capacity -= additional

        for index, (heading, sessions) in indexed_sections:
            if index not in selected:
                continue
            lines.append(
                _fit("{} ({})".format(heading, len(sessions)), columns)
            )
            for row, depth in sessions[:allocations[index]]:
                lines.append(_session_line(row, now, columns, depth))
        hidden = total_active - sum(allocations.values())
        if show_hidden and hidden:
            lines.append(_fit("… {} hidden".format(hidden), columns))
        return "\n".join(lines) + "\n"

    for heading, sessions in active_sections:
        lines.append(_fit("{} ({})".format(heading, len(sessions)), columns))
        lines.extend(
            _session_line(row, now, columns, depth)
            for row, depth in sessions
        )

    idle_sessions = arrange_session_tree(grouped[Status.IDLE.value])
    if not idle_sessions:
        return "\n".join(lines) + "\n"

    remaining = line_limit - len(lines)
    if remaining <= 0:
        return "\n".join(lines) + "\n"

    if 1 + len(idle_sessions) <= remaining:
        lines.append(
            _fit("IDLE ({})".format(len(idle_sessions)), columns)
        )
        lines.extend(
            _session_line(row, now, columns, depth)
            for row, depth in idle_sessions
        )
        return "\n".join(lines) + "\n"

    # The hidden count may replace IDLE rows, but never a higher-priority
    # WORKING or WAITING row.
    idle_capacity = max(0, remaining - 2)
    if remaining >= 2:
        lines.append(
            _fit("IDLE ({})".format(len(idle_sessions)), columns)
        )
        lines.extend(
            _session_line(row, now, columns, depth)
            for row, depth in idle_sessions[:idle_capacity]
        )
    hidden = len(idle_sessions) - idle_capacity
    lines.append(_fit("… {} hidden".format(hidden), columns))
    return "\n".join(lines) + "\n"


def _isatty(stream: TextIO) -> bool:
    method = getattr(stream, "isatty", None)
    try:
        return bool(method()) if callable(method) else False
    except (OSError, ValueError):
        return False


def _terminal_size(
    output: TextIO,
    width: Optional[int],
    height: Optional[int],
    *,
    query: bool,
) -> Tuple[int, int]:
    fallback = os.terminal_size(
        (DEFAULT_TERMINAL_WIDTH, DEFAULT_TERMINAL_HEIGHT)
    )
    size = fallback
    if query:
        try:
            descriptor = output.fileno()
            if descriptor >= 0:
                try:
                    size = os.get_terminal_size(descriptor)
                except OSError:
                    size = shutil.get_terminal_size(fallback)
        except (AttributeError, OSError, ValueError):
            size = fallback
    columns = size.columns if width is None else int(width)
    lines = size.lines if height is None else int(height)
    return max(1, columns), max(0, lines)


def run_tui(
    *,
    scanner: Optional[Scanner] = None,
    client: Optional[SidecarClient] = None,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
    once: bool = False,
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> int:
    """Run the dashboard, restoring terminal settings on every exit path."""

    if refresh_interval <= 0:
        raise ValueError("refresh_interval must be positive")
    input_stream = sys.stdin if stdin is None else stdin
    output = sys.stdout if stdout is None else stdout
    active_scanner = Scanner() if scanner is None else scanner
    active_client = SidecarClient() if client is None else client
    poller = SnapshotPoller(active_client, active_scanner)

    interactive = not once and _isatty(input_stream) and _isatty(output)
    if not interactive:
        columns, lines = _terminal_size(
            output,
            width,
            height,
            query=False,
        )
        rows, source = poller.poll()
        output.write(
            render_snapshot(
                rows,
                columns,
                time.time(),
                lines,
                source=source,
                interactive=False,
            )
        )
        output.flush()
        return 0

    import termios
    import tty

    descriptor = input_stream.fileno()
    previous_settings = termios.tcgetattr(descriptor)
    entered_screen = False
    try:
        tty.setcbreak(descriptor)
        output.write(ENTER_SCREEN)
        output.flush()
        entered_screen = True
        while True:
            columns, lines = _terminal_size(
                output,
                width,
                height,
                query=True,
            )
            rows, source = poller.poll()
            snapshot = render_snapshot(
                rows,
                columns,
                time.time(),
                lines,
                source=source,
                interactive=True,
            )
            output.write(CLEAR_SCREEN)
            output.write(snapshot)
            output.flush()
            try:
                readable, _writable, _exceptional = select.select(
                    [input_stream],
                    [],
                    [],
                    refresh_interval,
                )
            except InterruptedError:
                continue
            if not readable:
                continue
            key = input_stream.read(1)
            if not key or key.lower() == "q":
                return 0
    except KeyboardInterrupt:
        return 130
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous_settings)
        if entered_screen:
            output.write(LEAVE_SCREEN)
            output.flush()


__all__ = [
    "CLEAR_SCREEN",
    "DEFAULT_FALLBACK_SCAN_INTERVALS",
    "DEFAULT_REFRESH_INTERVAL",
    "DEFAULT_TERMINAL_HEIGHT",
    "DEFAULT_TERMINAL_WIDTH",
    "ENTER_SCREEN",
    "FallbackScanBackoff",
    "LEAVE_SCREEN",
    "SOURCE_DAEMON",
    "SOURCE_DIRECT",
    "SnapshotPoller",
    "TREE_MAX_DEPTH",
    "arrange_session_tree",
    "render_snapshot",
    "run_tui",
]
