import io
import os
import termios
import unittest
import unicodedata
from unittest import mock

from sidecar.client import SidecarClientError
from sidecar.model import Session, Status
from sidecar.tui import (
    ENTER_SCREEN,
    LEAVE_SCREEN,
    SOURCE_DAEMON,
    SOURCE_DIRECT,
    TREE_MAX_DEPTH,
    SnapshotPoller,
    arrange_session_tree,
    render_snapshot,
    run_tui,
)


def make_session(
    session_id,
    status,
    updated_at,
    *,
    agent="claude",
    title="A title",
    project="/tmp/project",
    extra=None,
    parent_id=None,
):
    return Session(
        agent=agent,
        session_id=session_id,
        project=project,
        transcript="/tmp/{}.jsonl".format(session_id),
        updated_at=updated_at,
        title=title,
        status=status,
        extra={} if extra is None else extra,
        parent_id=parent_id,
    )


class FakeClient:
    def __init__(self, rows=(), offline=False):
        self.rows = list(rows)
        self.offline = offline
        self.calls = 0

    def status(self):
        self.calls += 1
        if self.offline:
            raise SidecarClientError("offline", code="connection_failed")
        return list(self.rows)


class FakeScanner:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = 0

    def scan(self):
        self.calls += 1
        return list(self.rows)


class FakeTTY(io.StringIO):
    def __init__(self, value="", descriptor=42):
        super().__init__(value)
        self.descriptor = descriptor

    def isatty(self):
        return True

    def fileno(self):
        return self.descriptor


class FakeClock:
    def __init__(self, now=0.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


def display_width(value):
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in ("F", "W")
        else 1
        for character in value
    )


class TUIRenderTests(unittest.TestCase):
    def test_render_groups_in_lifecycle_order_and_shows_fields(self):
        rows = [
            make_session("idle-id", Status.IDLE, 700.0, agent="codex"),
            make_session(
                "working-id",
                Status.WORKING,
                990.0,
                title="Implement feature",
                project="/work/sidecar",
                extra={"last_message": "running tests"},
            ),
            make_session("waiting-id", Status.WAITING, 900.0, agent="cursor-cli"),
            make_session("dead-id", Status.DEAD, 999.0),
        ]

        rendered = render_snapshot(rows, width=120, now=1000.0)

        self.assertLess(rendered.index("WORKING"), rendered.index("WAITING"))
        self.assertLess(rendered.index("WAITING"), rendered.index("IDLE"))
        self.assertIn("claude  working-id  10s  Implement feature", rendered)
        self.assertIn("project: /work/sidecar", rendered)
        self.assertIn("latest: last_message=running tests", rendered)
        self.assertNotIn("dead-id", rendered)
        working_line = next(
            line for line in rendered.splitlines() if "working-id" in line
        )
        self.assertIn("project: /work/sidecar", working_line)
        self.assertIn("latest: last_message=running tests", working_line)

    def test_render_formats_dict_session_and_malformed_row_ages(self):
        seconds = make_session(
            "dict-seconds",
            Status.WORKING,
            941.0,
        ).to_dict()
        minute = make_session("session-minute", Status.WAITING, 940.0)
        hour = make_session(
            "dict-hour",
            Status.IDLE,
            -2600.0,
        ).to_dict()
        day = make_session("session-day", Status.IDLE, -85400.0)
        malformed = make_session(
            "dict-malformed",
            Status.IDLE,
            0.0,
        ).to_dict()
        malformed["updated_at"] = "not-a-timestamp"

        rendered = render_snapshot(
            [seconds, minute, hour, day, malformed],
            width=160,
            now=1000.0,
            height=12,
        )

        self.assertIn("dict-seconds  59s", rendered)
        self.assertIn("session-minute  1m", rendered)
        self.assertIn("dict-hour  1h", rendered)
        self.assertIn("session-day  1d", rendered)
        self.assertIn("dict-malformed  ?", rendered)

    def test_render_orders_newest_first_within_group_and_respects_width(self):
        rows = [
            make_session("older", Status.WORKING, 100.0),
            make_session("newer", Status.WORKING, 200.0),
        ]

        rendered = render_snapshot(rows, width=48, now=300.0)

        self.assertLess(rendered.index("newer"), rendered.index("older"))
        self.assertTrue(all(len(line) <= 48 for line in rendered.splitlines()))

    def test_render_orders_same_status_parent_before_indented_child(self):
        rows = [
            make_session(
                "child",
                Status.WAITING,
                200.0,
                parent_id="parent",
            ),
            make_session("parent", Status.WAITING, 100.0),
            make_session(
                "orphan",
                Status.WAITING,
                300.0,
                parent_id="missing",
                extra={"sidechain": True},
            ),
        ]

        rendered = render_snapshot(rows, width=120, now=300.0, height=8)
        child_line = next(
            line for line in rendered.splitlines() if "child" in line
        )
        orphan_line = next(
            line for line in rendered.splitlines() if "orphan" in line
        )

        self.assertLess(rendered.index("parent"), rendered.index("child"))
        self.assertTrue(child_line.startswith("    ↳ claude"))
        self.assertFalse(orphan_line.startswith("    ↳"))
        self.assertIn("[sidechain]", orphan_line)

    def test_tree_identity_and_parent_lookup_include_host(self):
        local_parent = make_session(
            "shared",
            Status.WAITING,
            100.0,
        ).to_dict()
        local_parent["host"] = "local"
        remote_same_id = make_session(
            "shared",
            Status.WAITING,
            300.0,
        ).to_dict()
        remote_same_id["host"] = "edge"
        remote_child = make_session(
            "remote-child",
            Status.WAITING,
            200.0,
            parent_id="shared",
        ).to_dict()
        remote_child["host"] = "edge"
        other_host_child = make_session(
            "other-child",
            Status.WAITING,
            400.0,
            parent_id="shared",
        ).to_dict()
        other_host_child["host"] = "other"

        arranged = arrange_session_tree(
            [other_host_child, remote_child, local_parent, remote_same_id]
        )
        depths = {
            (row["host"], row["session_id"]): depth
            for row, depth in arranged
        }

        self.assertEqual(0, depths[("local", "shared")])
        self.assertEqual(0, depths[("edge", "shared")])
        self.assertEqual(1, depths[("edge", "remote-child")])
        self.assertEqual(0, depths[("other", "other-child")])

    def test_render_breaks_cycles_and_caps_deep_tree_without_recursion(self):
        cycle_rows = [
            make_session(
                "cycle-a",
                Status.WAITING,
                20.0,
                parent_id="cycle-b",
            ),
            make_session(
                "cycle-b",
                Status.WAITING,
                10.0,
                parent_id="cycle-a",
            ),
        ]
        cycle = render_snapshot(
            cycle_rows,
            width=100,
            now=30.0,
            height=6,
        )

        self.assertIn("cycle-a", cycle)
        self.assertIn("cycle-b", cycle)
        self.assertNotIn("↳", cycle)

        deep_rows = []
        parent_id = None
        for index in range(1200):
            session_id = "deep-{:04d}".format(index)
            deep_rows.append(
                make_session(
                    session_id,
                    Status.WAITING,
                    float(index),
                    parent_id=parent_id,
                )
            )
            parent_id = session_id
        deep = render_snapshot(
            deep_rows,
            width=100,
            now=1200.0,
            height=16,
        )
        branch_offsets = [
            line.index("↳")
            for line in deep.splitlines()
            if "↳" in line
        ]

        self.assertIn("deep-0000", deep)
        self.assertLessEqual(max(branch_offsets), 2 + 2 * TREE_MAX_DEPTH)
        self.assertLessEqual(len(deep.splitlines()), 16)

    def test_status_groups_keep_active_children_ahead_of_idle_tree(self):
        rows = [
            make_session(
                "working-child",
                Status.WORKING,
                50.0,
                parent_id="idle-parent",
            ),
            make_session("waiting-root", Status.WAITING, 40.0),
            make_session("idle-parent", Status.IDLE, 30.0),
        ]
        rows.extend(
            make_session(
                "idle-child-{:02d}".format(index),
                Status.IDLE,
                float(index),
                parent_id="idle-parent",
            )
            for index in range(20)
        )

        rendered = render_snapshot(
            rows,
            width=80,
            now=60.0,
            height=6,
        )
        working_line = next(
            line for line in rendered.splitlines() if "working-child" in line
        )

        self.assertIn("working-child", rendered)
        self.assertIn("waiting-root", rendered)
        self.assertFalse(working_line.startswith("    ↳"))
        self.assertNotIn("idle-parent", rendered)
        self.assertLessEqual(len(rendered.splitlines()), 6)

    def test_render_bounds_realistic_snapshot_and_keeps_all_active(self):
        rows = [
            make_session(
                "working-active",
                Status.WORKING,
                1000.0,
                title="Current work",
            )
        ]
        rows.extend(
            make_session(
                "waiting-{:02d}".format(index),
                Status.WAITING,
                900.0 + index,
                title="Needs input",
            )
            for index in range(14)
        )
        rows.extend(
            make_session(
                "idle-{:03d}".format(index),
                Status.IDLE,
                float(index),
            )
            for index in range(207)
        )

        rendered = render_snapshot(
            rows,
            width=160,
            now=1000.0,
            height=24,
        )

        self.assertLessEqual(len(rendered.splitlines()), 24)
        self.assertIn("WORKING (1)", rendered)
        self.assertIn("WAITING (14)", rendered)
        self.assertIn("working-active", rendered)
        for index in range(14):
            self.assertIn("waiting-{:02d}".format(index), rendered)
        self.assertIn("idle-206", rendered)
        self.assertNotIn("idle-202", rendered)
        self.assertIn("… 203 hidden", rendered)

    def test_render_reserves_space_for_each_active_group(self):
        rows = [
            make_session(
                "working-{:02d}".format(index),
                Status.WORKING,
                100.0 + index,
            )
            for index in range(25)
        ]
        rows.append(make_session("waiting-for-user", Status.WAITING, 200.0))

        rendered = render_snapshot(
            rows,
            width=100,
            now=300.0,
            height=10,
        )

        self.assertLessEqual(len(rendered.splitlines()), 10)
        self.assertIn("waiting-for-user", rendered)
        self.assertIn("working-24", rendered)
        self.assertIn("… 20 hidden", rendered)

    def test_render_handles_narrow_widths_and_tiny_heights(self):
        rows = [
            make_session("work-id", Status.WORKING, 20.0),
            make_session("wait-id", Status.WAITING, 10.0),
            make_session("idle-id", Status.IDLE, 0.0),
        ]

        for height in range(5):
            rendered = render_snapshot(
                rows,
                width=7,
                now=30.0,
                height=height,
            )
            self.assertLessEqual(len(rendered.splitlines()), height)
            self.assertTrue(
                all(display_width(line) <= 7 for line in rendered.splitlines())
            )
        self.assertEqual(
            "",
            render_snapshot(rows, width=1, now=30.0, height=0),
        )

    def test_render_is_unicode_width_and_control_safe(self):
        row = make_session(
            "id\x1b]0;pwned\x07\x1b[31m-red\x1b[0m\x9b2J",
            Status.WORKING,
            20.0,
            title="修复\r终端 \x1b]2;st-pwned\x1b\\完成",
            project="/tmp/\n项目\x9d0;c1-pwned\x9c",
            extra={"last_message": "等待\t输入\x1b[999C\x85继续"},
        )

        rendered = render_snapshot(
            [row],
            width=160,
            now=30.0,
            height=4,
        )

        self.assertIn("id-red", rendered)
        self.assertIn("修复 终端 完成", rendered)
        self.assertIn("project: /tmp/ 项目", rendered)
        self.assertIn("latest: last_message=等待 输入 继续", rendered)
        self.assertNotIn("pwned", rendered)
        self.assertNotIn("\x1b", rendered)
        self.assertNotIn("\x9b", rendered)
        self.assertNotIn("\r", rendered)
        self.assertTrue(
            all(
                character == "\n"
                or not (
                    ord(character) < 0x20
                    or 0x7F <= ord(character) <= 0x9F
                )
                for character in rendered
            )
        )
        self.assertTrue(
            all(display_width(line) <= 160 for line in rendered.splitlines())
        )

    def test_render_hud_distinguishes_daemon_and_direct_fallback(self):
        daemon = render_snapshot(
            [],
            width=160,
            now=30.0,
            source=SOURCE_DAEMON,
        )
        direct = render_snapshot(
            [],
            width=160,
            now=30.0,
            source=SOURCE_DIRECT,
        )
        untrusted = render_snapshot(
            [],
            width=160,
            now=30.0,
            source="bad\x1b]0;pwned\x07\nsource",
        )

        self.assertIn("source: daemon/connected", daemon)
        self.assertIn("source: direct/offline", direct)
        self.assertIn("agent-sidecar daemon start", direct)
        self.assertNotIn("pwned", untrusted)
        self.assertNotIn("\x1b", untrusted)
        self.assertNotIn("\nsource", untrusted)


class TUIPollerTests(unittest.TestCase):
    def test_idle_direct_scans_back_off_to_cap(self):
        client = FakeClient(offline=True)
        scanner = FakeScanner()
        poller = SnapshotPoller(client, scanner)

        rows, source = poller.poll(now=0.0)
        self.assertEqual([], rows)
        self.assertEqual(SOURCE_DIRECT, source)
        self.assertEqual(1, scanner.calls)
        self.assertEqual(2.0, poller.backoff.current_interval)

        poller.poll(now=1.99)
        self.assertEqual(1, scanner.calls)
        poller.poll(now=2.0)
        self.assertEqual(2, scanner.calls)
        self.assertEqual(5.0, poller.backoff.current_interval)

        poller.poll(now=6.99)
        self.assertEqual(2, scanner.calls)
        poller.poll(now=7.0)
        self.assertEqual(3, scanner.calls)
        self.assertEqual(10.0, poller.backoff.current_interval)

        poller.poll(now=16.99)
        self.assertEqual(3, scanner.calls)
        poller.poll(now=17.0)
        poller.poll(now=27.0)
        self.assertEqual(5, scanner.calls)
        self.assertEqual(10.0, poller.backoff.current_interval)

    def test_activity_and_snapshot_changes_reset_fast_scan_cadence(self):
        client = FakeClient(offline=True)
        scanner = FakeScanner()
        poller = SnapshotPoller(client, scanner)

        poller.poll(now=0.0)
        poller.poll(now=2.0)
        self.assertEqual(5.0, poller.backoff.current_interval)

        active = make_session("active", Status.WORKING, 7.0)
        scanner.rows = [active]
        rows, _source = poller.poll(now=7.0)
        self.assertEqual([active], rows)
        self.assertEqual(2.0, poller.backoff.current_interval)

        poller.poll(now=8.0)
        self.assertEqual(3, scanner.calls)
        poller.poll(now=9.0)
        self.assertEqual(4, scanner.calls)
        self.assertEqual(2.0, poller.backoff.current_interval)

        scanner.rows = [make_session("active", Status.IDLE, 11.0)]
        poller.poll(now=11.0)
        self.assertEqual(2.0, poller.backoff.current_interval)
        poller.poll(now=13.0)
        self.assertEqual(5.0, poller.backoff.current_interval)

    def test_daemon_is_probed_each_refresh_and_recovers_before_scan_is_due(self):
        client = FakeClient(offline=True)
        direct = make_session("direct", Status.IDLE, 0.0)
        scanner = FakeScanner([direct])
        poller = SnapshotPoller(client, scanner)

        rows, source = poller.poll(now=0.0)
        self.assertEqual([direct], rows)
        self.assertEqual(SOURCE_DIRECT, source)

        daemon = make_session("daemon", Status.WORKING, 1.0)
        client.offline = False
        client.rows = [daemon.to_dict()]
        rows, source = poller.poll(now=1.0)
        self.assertEqual([daemon.to_dict()], rows)
        self.assertEqual(SOURCE_DAEMON, source)
        self.assertEqual(1, scanner.calls)
        self.assertEqual(2, client.calls)

        client.offline = True
        scanner.rows = [make_session("direct-again", Status.IDLE, 1.1)]
        rows, source = poller.poll(now=1.1)
        self.assertEqual("direct-again", rows[0].session_id)
        self.assertEqual(SOURCE_DIRECT, source)
        self.assertEqual(2, scanner.calls)


class TUIRunTests(unittest.TestCase):
    def test_once_prefers_daemon_and_emits_no_terminal_control_sequences(self):
        daemon = make_session("daemon-id", Status.WORKING, 100.0)
        scanner = FakeScanner([make_session("direct-id", Status.IDLE, 100.0)])
        output = io.StringIO()

        code = run_tui(
            client=FakeClient([daemon.to_dict()]),
            scanner=scanner,
            stdout=output,
            once=True,
            width=100,
        )

        self.assertEqual(0, code)
        self.assertIn("daemon-id", output.getvalue())
        self.assertNotIn("direct-id", output.getvalue())
        self.assertNotIn(ENTER_SCREEN, output.getvalue())
        self.assertIn("source: daemon/connected", output.getvalue())
        self.assertNotIn("q quit", output.getvalue())
        self.assertEqual(0, scanner.calls)

    def test_once_falls_back_to_scanner_when_daemon_is_unavailable(self):
        direct = make_session("direct-id", Status.WAITING, 100.0)
        scanner = FakeScanner([direct])
        output = io.StringIO()

        code = run_tui(
            client=FakeClient(offline=True),
            scanner=scanner,
            stdout=output,
            once=True,
            width=100,
        )

        self.assertEqual(0, code)
        self.assertIn("direct-id", output.getvalue())
        self.assertIn("source: direct/offline", output.getvalue())
        self.assertIn("agent-sidecar daemon start", output.getvalue())
        self.assertNotIn("q quit", output.getvalue())
        self.assertEqual(1, scanner.calls)

    def test_once_uses_deterministic_fallback_height(self):
        rows = [
            make_session(
                "idle-{:02d}".format(index),
                Status.IDLE,
                float(index),
            )
            for index in range(40)
        ]
        output = io.StringIO()

        with mock.patch(
            "sidecar.tui.shutil.get_terminal_size",
            side_effect=AssertionError("non-TTY size must be deterministic"),
        ):
            code = run_tui(
                client=FakeClient(rows),
                scanner=FakeScanner(),
                stdout=output,
                once=True,
            )

        self.assertEqual(0, code)
        self.assertLessEqual(len(output.getvalue().splitlines()), 24)
        self.assertIn("hidden", output.getvalue())

    def test_interactive_refresh_queries_rows_and_columns_and_restores(self):
        input_stream = FakeTTY("q", descriptor=42)
        output = FakeTTY(descriptor=43)
        terminal_size = os.terminal_size((72, 11))

        with mock.patch(
            "sidecar.tui.os.get_terminal_size",
            return_value=terminal_size,
        ) as get_size, mock.patch(
            "sidecar.tui.select.select",
            return_value=([input_stream], [], []),
        ), mock.patch.object(
            termios,
            "tcgetattr",
            return_value=["saved"],
        ), mock.patch.object(
            termios,
            "tcsetattr",
        ) as restore, mock.patch(
            "tty.setcbreak",
        ):
            code = run_tui(
                client=FakeClient(
                    [make_session("active", Status.WORKING, 100.0)]
                ),
                scanner=FakeScanner(),
                stdin=input_stream,
                stdout=output,
                refresh_interval=0.01,
            )

        self.assertEqual(0, code)
        get_size.assert_called_once_with(43)
        restore.assert_called_once_with(42, termios.TCSADRAIN, ["saved"])
        self.assertIn(ENTER_SCREEN, output.getvalue())
        self.assertIn(LEAVE_SCREEN, output.getvalue())

    def test_interactive_keeps_polling_input_while_direct_scans_back_off(self):
        input_stream = FakeTTY("q", descriptor=42)
        output = FakeTTY(descriptor=43)
        client = FakeClient(offline=True)
        scanner = FakeScanner()
        clock = FakeClock()
        timeouts = []

        def wait_for_input(_readers, _writers, _errors, timeout):
            timeouts.append(timeout)
            clock.advance(1.0)
            if clock.now >= 4.0:
                return ([input_stream], [], [])
            return ([], [], [])

        with mock.patch(
            "sidecar.tui.select.select",
            side_effect=wait_for_input,
        ), mock.patch(
            "sidecar.tui.time.monotonic",
            new=clock,
        ), mock.patch.object(
            termios,
            "tcgetattr",
            return_value=["saved"],
        ), mock.patch.object(
            termios,
            "tcsetattr",
        ), mock.patch(
            "tty.setcbreak",
        ):
            code = run_tui(
                client=client,
                scanner=scanner,
                stdin=input_stream,
                stdout=output,
                refresh_interval=0.25,
                width=100,
                height=10,
            )

        self.assertEqual(0, code)
        self.assertEqual([0.25, 0.25, 0.25, 0.25], timeouts)
        self.assertEqual(4, client.calls)
        self.assertEqual(2, scanner.calls)
        self.assertIn("source: direct/offline", output.getvalue())


if __name__ == "__main__":
    unittest.main()
