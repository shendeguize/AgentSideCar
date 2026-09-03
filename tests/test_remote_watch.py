import ast
import dataclasses
import io
import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sidecar import remote, remote_transport
from sidecar.process_runner import (
    BoundedLineStreamEndReason,
    BoundedLineStreamProcessError,
    BoundedLineStreamResult,
)
from sidecar.remote_watch_transport import (
    END_FRAME,
    PING_FRAME,
    READY_FRAME,
    RemoteWatchHostStream,
    RemoteWatchTransportError,
    open_remote_watch_host,
)
from sidecar.remote_watch_types import MAX_WATCH_LINE_BYTES


def event_payload(session_id="session", text="hello", extra=None):
    return {
        "ts": "2026-08-23T18:00:00+08:00",
        "agent": "claude",
        "session_id": session_id,
        "kind": "assistant",
        "text": text,
        "extra": {} if extra is None else extra,
    }


def event_line(**changes):
    value = event_payload()
    value.update(changes)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def completed(
    argv,
    version=(3, 11, 0),
    executable="/usr/bin/python3",
    returncode=0,
    stderr=b"",
):
    payload = (
        {"python": None}
        if version is None
        else {"python": list(version), "executable": executable}
    )
    return subprocess.CompletedProcess(
        argv,
        returncode,
        stdout=json.dumps(
            payload,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n",
        stderr=stderr,
    )


def process_exists(pid):
    if sys.platform.startswith("linux"):
        try:
            payload = Path("/proc/{}/stat".format(pid)).read_text(encoding="ascii")
        except FileNotFoundError:
            return False
        except OSError:
            pass
        else:
            suffix = payload.rsplit(")", 1)
            fields = suffix[1].split() if len(suffix) == 2 else ()
            if fields:
                # A killed non-child can remain a zombie under non-reaping PID 1.
                # It is terminated even though kill(pid, 0) still reports existence.
                return fields[0] != "Z"
    if sys.platform == "darwin":
        # Same zombie, no /proc to read it from: ask ps for the state so a
        # killed process does not read as a live one. An unusable ps falls
        # through to the signal probe.
        try:
            state = subprocess.run(
                ["/bin/ps", "-o", "state=", "-p", str(pid)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            state = None
        if state is not None:
            if state.returncode != 0:
                return False
            flags = state.stdout.decode("ascii", "replace").strip()
            return bool(flags) and not flags.startswith("Z")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid_when_ready(path, deadline):
    """Return the pid a child recorded, waiting for content and not the file.

    A child creates its pid file and then writes to it, so a reader that waits
    only for existence can still read an empty file on a loaded host.
    """

    while time.monotonic() < deadline:
        try:
            payload = path.read_text(encoding="ascii").strip()
        except OSError:
            payload = ""
        if payload.isdecimal():
            return int(payload)
        time.sleep(0.01)
    raise AssertionError("child never recorded its pid")


class ProcessLivenessTests(unittest.TestCase):
    def test_process_exists_distinguishes_linux_live_and_zombie_states(self):
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(
            Path,
            "read_text",
            side_effect=(
                "123 (live child) S 1 2 3\n",
                "124 (zombie child) Z 1 2 3\n",
            ),
        ), mock.patch.object(os, "kill") as kill:
            self.assertTrue(process_exists(123))
            self.assertFalse(process_exists(124))
        kill.assert_not_called()

        def ps_result(state, code=0):
            return subprocess.CompletedProcess([], code, stdout=state)

        for state, expected in ((b"S+\n", True), (b"Z\n", False), (b"\n", False)):
            with self.subTest(state=state):
                with mock.patch.object(
                    sys, "platform", "darwin"
                ), mock.patch.object(
                    subprocess, "run", return_value=ps_result(state)
                ), mock.patch.object(
                    os, "kill"
                ) as kill:
                    self.assertEqual(expected, process_exists(125))
                kill.assert_not_called()

        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(
            subprocess, "run", return_value=ps_result(b"", code=1)
        ):
            self.assertFalse(process_exists(126))

        # No usable ps leaves only the signal probe, which is still better
        # than reporting a live process as gone.
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(
            subprocess, "run", side_effect=OSError
        ), mock.patch.object(os, "kill") as kill:
            self.assertTrue(process_exists(127))
        kill.assert_called_once_with(127, 0)


class FakeLineStream:
    def __init__(self, lines):
        self.lines = iter(lines)
        self.ready = False
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        return next(self.lines)

    def mark_ready(self):
        self.ready = True

    def close(self):
        self.closed = True


class RemoteWatchTypeTests(unittest.TestCase):
    def test_items_are_frozen_extra_is_immutable_and_failure_is_explicit(self):
        ready = remote.RemoteWatchReady("edge")
        failure = remote.RemoteWatchFailure("edge", "unreachable")
        event = remote.RemoteWatchEvent(
            host="edge",
            **event_payload(extra={"nested": [1, {"two": True}]}),
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            ready.host = "other"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            event.text = "changed"
        with self.assertRaises(TypeError):
            event.extra["new"] = "value"
        with self.assertRaises(TypeError):
            event.extra["nested"][1]["new"] = "value"
        self.assertTrue(failure.terminal)
        self.assertTrue(failure.events_may_be_missed)
        self.assertEqual(
            {
                "type": "failure",
                "host": "edge",
                "code": "unreachable",
                "terminal": True,
                "events_may_be_missed": True,
            },
            failure.to_dict(),
        )
        self.assertEqual("edge", event.to_dict()["host"])
        self.assertEqual(
            {"nested": [1, {"two": True}]},
            event.to_dict()["extra"],
        )

    def test_event_schema_unicode_and_extra_bounds_are_strict(self):
        valid = event_payload(text="中文 👩‍💻\n")
        host_stream = RemoteWatchHostStream(
            remote.RemoteHost("edge", "ready"),
            FakeLineStream(
                [
                    READY_FRAME,
                    json.dumps(valid, ensure_ascii=False).encode("utf-8"),
                    END_FRAME,
                ]
            ),
        )
        host_stream.read_ready()
        self.assertEqual(valid["text"], next(host_stream).text)

        invalid_values = []
        missing = event_payload()
        missing.pop("extra")
        invalid_values.append(missing)
        additional = event_payload()
        additional["host"] = "forged"
        invalid_values.append(additional)
        invalid_values.append(dict(event_payload(), text="\ud800"))
        invalid_values.append(dict(event_payload(), extra=[]))
        invalid_values.append(dict(event_payload(), extra={"value": float("nan")}))
        for value in invalid_values:
            with self.subTest(keys=tuple(value)):
                raw = json.dumps(
                    value,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    allow_nan=True,
                ).encode("ascii")
                stream = RemoteWatchHostStream(
                    remote.RemoteHost("edge", "ready"),
                    FakeLineStream([READY_FRAME, raw]),
                )
                stream.read_ready()
                with self.assertRaises(RemoteWatchTransportError):
                    next(stream)

    def test_control_event_ambiguity_duplicate_and_nonfinite_are_rejected(self):
        frames = (
            b'{"type":"ready","ts":"forged"}',
            b'{"ts":"t","agent":"a","session_id":"s","kind":"k",'
            b'"text":"x","extra":{},"text":"again"}',
            b'{"ts":"t","agent":"a","session_id":"s","kind":"k",'
            b'"text":"x","extra":{"n":NaN}}',
        )
        for frame in frames:
            with self.subTest(frame=frame):
                stream = RemoteWatchHostStream(
                    remote.RemoteHost("edge", "ready"),
                    FakeLineStream([READY_FRAME, frame]),
                )
                stream.read_ready()
                with self.assertRaises(RemoteWatchTransportError) as raised:
                    next(stream)
                self.assertEqual("protocol", raised.exception.code)


class RemoteWatchTransportTests(unittest.TestCase):
    def test_strict_watch_ssh_argv_and_snapshot_watch_rejection(self):
        argv = remote.remote_watch_ssh_argv("edge.safe", from_start=True)

        self.assertEqual("ssh", argv[0])
        self.assertEqual("edge.safe", argv[-2])
        self.assertEqual("-T", argv[-3])
        self.assertIn("ControlMaster=no", argv)
        self.assertIn("ControlPath=none", argv)
        self.assertIn("ControlPersist=no", argv)
        self.assertNotIn("edge.safe", argv[-1])
        self.assertTrue(
            argv[-1].endswith("watch --all --from-start --json"),
            argv[-1],
        )
        self.assertNotIn("send", remote.REMOTE_WATCH_BOOTSTRAP)
        with self.assertRaises(ValueError):
            remote.remote_shell_command("watch")
        with self.assertRaises(ValueError):
            remote.remote_watch_ssh_argv("edge;touch")

    def test_bootstrap_never_waits_on_the_child_outside_the_tolerant_reaper(self):
        # Cleanup runs in a `finally`, so a `wait` that raises there replaces
        # the bootstrap's own exit status with a traceback and exit 1 — which
        # is what a loaded runner produced when a killed child took longer
        # than a second to reap.
        tree = ast.parse(remote.REMOTE_WATCH_BOOTSTRAP)
        reaper = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "reap"
            ),
            None,
        )
        self.assertIsNotNone(reaper, "the bootstrap has no reap() helper")
        absorbed = [
            handler
            for node in ast.walk(reaper)
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if isinstance(handler.type, ast.Attribute)
            and handler.type.attr == "TimeoutExpired"
        ]
        self.assertTrue(absorbed, "reap() must absorb TimeoutExpired")

        tolerated = {id(node) for node in ast.walk(reaper)}
        waits = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "wait"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "process"
        ]
        self.assertTrue(waits, "the bootstrap never waits for its child")
        for wait in waits:
            self.assertIn(
                id(wait),
                tolerated,
                "process.wait() outside reap() can raise during cleanup",
            )

    def test_watch_builders_default_to_python3_and_shell_quote_custom_executable(self):
        default_command = remote.remote_watch_shell_command()
        default_argv = remote.remote_watch_ssh_argv("edge")
        custom = "/opt/Python Builds/python3"
        custom_command = remote.remote_watch_shell_command(
            python_executable=custom,
        )
        custom_argv = remote.remote_watch_ssh_argv(
            "edge",
            python_executable=custom,
        )

        self.assertEqual("python3", shlex.split(default_command)[0])
        self.assertEqual("python3", shlex.split(default_argv[-1])[0])
        self.assertEqual(custom, shlex.split(custom_command)[0])
        self.assertEqual(custom, shlex.split(custom_argv[-1])[0])
        self.assertIn(shlex.quote(custom), custom_command)

    def test_first_frame_must_be_exact_ready_and_typed_error_is_sanitized(self):
        for frame in (b'{"type": "ready"}', b'{"type":"end"}', b"banner"):
            stream = RemoteWatchHostStream(
                remote.RemoteHost("edge", "ready"),
                FakeLineStream([frame]),
            )
            with self.subTest(frame=frame):
                with self.assertRaises(RemoteWatchTransportError) as raised:
                    stream.read_ready()
                self.assertEqual("protocol", raised.exception.code)

        stream = RemoteWatchHostStream(
            remote.RemoteHost("edge", "ready"),
            FakeLineStream([READY_FRAME, b'{"type":"error","code":"resource_limit"}']),
        )
        stream.read_ready()
        with self.assertRaises(RemoteWatchTransportError) as raised:
            next(stream)
        self.assertEqual("resource_limit", raised.exception.code)
        self.assertNotIn("stderr", repr(raised.exception))

        startup_failure = RemoteWatchHostStream(
            remote.RemoteHost("edge", "ready"),
            FakeLineStream([b'{"type":"error","code":"remote"}']),
        )
        with self.assertRaises(RemoteWatchTransportError) as raised:
            startup_failure.read_ready()
        self.assertEqual("remote", raised.exception.code)

    def test_exact_heartbeat_is_invisible_and_malformed_ping_fails(self):
        payload = event_line(session_id="after-heartbeats")
        stream = RemoteWatchHostStream(
            remote.RemoteHost("edge", "ready"),
            FakeLineStream(
                [READY_FRAME, PING_FRAME, PING_FRAME, payload, PING_FRAME, END_FRAME]
            ),
        )
        self.assertEqual("edge", stream.read_ready().host)
        self.assertEqual("after-heartbeats", next(stream).session_id)
        with self.assertRaises(StopIteration):
            next(stream)

        malformed = RemoteWatchHostStream(
            remote.RemoteHost("edge", "ready"),
            FakeLineStream([READY_FRAME, b'{"type": "ping"}']),
        )
        malformed.read_ready()
        with self.assertRaises(RemoteWatchTransportError) as raised:
            next(malformed)
        self.assertEqual("protocol", raised.exception.code)

        before_ready = RemoteWatchHostStream(
            remote.RemoteHost("edge", "ready"),
            FakeLineStream([PING_FRAME]),
        )
        with self.assertRaises(RemoteWatchTransportError) as raised:
            before_ready.read_ready()
        self.assertEqual("protocol", raised.exception.code)

    def test_probe_floor_accepts_38_and_exhaustion_stops_stream_transfer(self):
        cases = (
            ((3, 8, 19), True),
            (None, False),
        )
        for version, accepted in cases:
            with self.subTest(version=version):
                stream_calls = []

                def stream_factory(*args, **kwargs):
                    stream_calls.append((args, kwargs))
                    return FakeLineStream([READY_FRAME, END_FRAME])

                stream, failure = open_remote_watch_host(
                    remote.RemoteHost("edge", "ready"),
                    b"zipapp",
                    runner=lambda argv, version=version, **kwargs: completed(
                        argv,
                        version,
                    ),
                    stream_factory=stream_factory,
                )

                if accepted:
                    self.assertIsNotNone(stream)
                    self.assertIsNone(failure)
                    self.assertEqual(1, len(stream_calls))
                    stream.close()
                else:
                    self.assertIsNone(stream)
                    self.assertEqual("python_too_old", failure.code)
                    self.assertEqual([], stream_calls)

    def test_watch_uses_discovered_executable_and_host_bindings_do_not_bleed(self):
        probe_calls = []
        stream_calls = []
        executables = {
            "first": "/opt/first+python",
            "second": "/srv/second-python",
        }

        def runner(argv, **kwargs):
            del kwargs
            alias = argv[-2]
            probe_calls.append(alias)
            return completed(argv, executable=executables[alias])

        def stream_factory(argv, artifact, **kwargs):
            del artifact, kwargs
            stream_calls.append((argv[-2], shlex.split(argv[-1])[0]))
            return FakeLineStream([READY_FRAME, END_FRAME])

        for alias in ("first", "second", "first"):
            stream, failure = open_remote_watch_host(
                remote.RemoteHost(alias, "ready"),
                b"zipapp",
                runner=runner,
                stream_factory=stream_factory,
            )
            self.assertIsNone(failure)
            self.assertEqual(alias, stream.read_ready().host)
            stream.close()

        self.assertEqual(["first", "second", "first"], probe_calls)
        self.assertEqual(
            [
                ("first", executables["first"]),
                ("second", executables["second"]),
                ("first", executables["first"]),
            ],
            stream_calls,
        )

    def test_watch_uses_explicit_candidate_verbatim_for_probe_and_bootstrap(self):
        pinned = "/opt/pinned/python3.8"
        resolved = "/resolved/python3.8"
        probe_calls = []
        stream_calls = []

        def runner(argv, **kwargs):
            del kwargs
            probe_calls.append(tuple(argv))
            return completed(argv, executable=resolved)

        def stream_factory(argv, artifact, **kwargs):
            del kwargs
            stream_calls.append((tuple(argv), artifact))
            return FakeLineStream([READY_FRAME, END_FRAME])

        stream, failure = open_remote_watch_host(
            remote.RemoteHost("edge", "ready"),
            b"zipapp",
            python_candidates=(pinned,),
            runner=runner,
            stream_factory=stream_factory,
        )

        self.assertIsNone(failure)
        self.assertEqual("edge", stream.read_ready().host)
        stream.close()
        self.assertEqual(1, len(probe_calls))
        probe_tokens = tuple(shlex.split(probe_calls[0][-1]))
        self.assertEqual(
            ("sh", "-c", remote_transport._PROBE_SCRIPT, "sh", pinned),
            probe_tokens,
        )
        self.assertEqual(1, len(stream_calls))
        bootstrap_tokens = tuple(shlex.split(stream_calls[0][0][-1]))
        self.assertEqual(probe_tokens[-1], bootstrap_tokens[0])
        self.assertEqual(pinned, bootstrap_tokens[0])
        self.assertNotEqual(resolved, bootstrap_tokens[0])
        self.assertEqual(b"zipapp", stream_calls[0][1])

    def test_explicit_watch_probe_response_is_validated_without_fallback(self):
        pinned = "/opt/pinned/python3.8"
        malformed = (
            {"python": [3, 8, 0], "executable": "relative/python3"},
            {"python": [3, 7, 999], "executable": "/resolved/python3"},
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                probe_calls = []
                stream_calls = []

                def runner(argv, **kwargs):
                    del kwargs
                    probe_calls.append(tuple(argv))
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            payload,
                            separators=(",", ":"),
                        ).encode("ascii")
                        + b"\n",
                        stderr=b"",
                    )

                stream, failure = open_remote_watch_host(
                    remote.RemoteHost("edge", "ready"),
                    b"zipapp",
                    python_candidates=(pinned,),
                    runner=runner,
                    stream_factory=lambda *args, **kwargs: stream_calls.append(
                        (args, kwargs)
                    ),
                )

                self.assertIsNone(stream)
                self.assertEqual("protocol", failure.code)
                self.assertEqual(1, len(probe_calls))
                self.assertEqual(
                    (pinned,),
                    tuple(shlex.split(probe_calls[0][-1]))[4:],
                )
                self.assertEqual([], stream_calls)

    def test_explicit_watch_probe_exhaustion_never_opens_default_stream(self):
        pinned = "/missing/python3.8"
        probe_calls = []
        stream_calls = []

        stream, failure = open_remote_watch_host(
            remote.RemoteHost("edge", "ready"),
            b"zipapp",
            python_candidates=(pinned,),
            runner=lambda argv, **kwargs: (
                probe_calls.append(tuple(argv)) or completed(argv, version=None)
            ),
            stream_factory=lambda *args, **kwargs: stream_calls.append(
                (args, kwargs)
            ),
        )

        self.assertIsNone(stream)
        self.assertEqual("python_too_old", failure.code)
        self.assertEqual(1, len(probe_calls))
        self.assertEqual((pinned,), tuple(shlex.split(probe_calls[0][-1]))[4:])
        self.assertEqual([], stream_calls)

    def test_fragmented_local_process_lines_use_bounded_line_stream(self):
        code = (
            "import os,time;"
            'os.write(1,b\'{"type":"rea\');'
            "time.sleep(.02);"
            "os.write(1,b'dy\"}\\n');"
            "os.write(1," + repr(event_line(session_id="fragmented")[:30]) + ");"
            "time.sleep(.02);"
            "os.write(1,"
            + repr(event_line(session_id="fragmented")[30:] + b"\n" + END_FRAME + b"\n")
            + ")"
        )
        from sidecar.process_runner import BoundedLineStream

        bounded = BoundedLineStream(
            [sys.executable, "-c", code],
            line_limit=MAX_WATCH_LINE_BYTES,
            startup_timeout=2,
            ready_on_first_line=False,
        )
        stream = RemoteWatchHostStream(
            remote.RemoteHost("edge", "ready"),
            bounded,
        )
        with stream:
            self.assertEqual("edge", stream.read_ready().host)
            self.assertEqual("fragmented", next(stream).session_id)
            with self.assertRaises(StopIteration):
                next(stream)

    def test_sanitized_error_has_no_raw_exception_chain_or_arguments(self):
        secret = b"Permission denied SECRET-KEY-PATH"
        result = BoundedLineStreamResult(
            args=("ssh", "secret-alias"),
            returncode=255,
            stderr=secret,
            end_reason=BoundedLineStreamEndReason.NONZERO_EXIT,
            lines_yielded=0,
            stdout_bytes_read=0,
        )

        class SecretStream:
            def __next__(self):
                raise BoundedLineStreamProcessError("SECRET-DIAGNOSTIC", result)

            def close(self):
                return None

        stream = RemoteWatchHostStream(
            remote.RemoteHost("edge", "ready"),
            SecretStream(),
        )
        with self.assertRaises(RemoteWatchTransportError) as raised:
            stream.read_ready()

        error = raised.exception
        self.assertEqual(("auth",), error.args)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        rendered = "{} {} {}".format(repr(error), error.args, error.__dict__)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("secret-alias", rendered)


class RemoteWatchFleetTests(unittest.TestCase):
    def _runner(self, argv, **kwargs):
        del kwargs
        return completed(argv)

    def test_all_hosts_start_and_events_survive_bounded_backpressure(self):
        hosts = tuple(
            remote.RemoteHost("edge-{}".format(index), "ready") for index in range(6)
        )
        started = set()
        lock = threading.Lock()

        def stream_factory(argv, artifact, **kwargs):
            self.assertEqual(b"zipapp", artifact)
            self.assertFalse(kwargs["cancel_event"].is_set())
            alias = argv[-2]
            with lock:
                started.add(alias)
            return FakeLineStream(
                [
                    READY_FRAME,
                    PING_FRAME,
                    event_line(session_id=alias + "-1"),
                    PING_FRAME,
                    event_line(session_id=alias + "-2"),
                    PING_FRAME,
                    END_FRAME,
                ]
            )

        with remote.watch_remote(
            hosts=hosts,
            runner=self._runner,
            stream_factory=stream_factory,
            artifact=b"zipapp",
            queue_items=1,
        ) as session:
            items = []
            for item in session:
                items.append(item)
                time.sleep(0.001)

        self.assertEqual({host.alias for host in hosts}, started)
        self.assertEqual(
            6, sum(isinstance(item, remote.RemoteWatchReady) for item in items)
        )
        events = [item for item in items if isinstance(item, remote.RemoteWatchEvent)]
        self.assertEqual(12, len(events))
        self.assertEqual(
            {
                (host.alias, host.alias + "-{}".format(index))
                for host in hosts
                for index in (1, 2)
            },
            {(event.host, event.session_id) for event in events},
        )

    def test_watch_facade_threads_one_explicit_candidate_to_every_host(self):
        pinned = ("/opt/pinned/python3.8",)
        calls = []

        def opener(host, artifact, **kwargs):
            calls.append(
                (
                    host.alias,
                    artifact,
                    kwargs["python_candidates"],
                )
            )
            return (
                RemoteWatchHostStream(
                    host,
                    FakeLineStream([READY_FRAME, END_FRAME]),
                ),
                None,
            )

        hosts = (
            remote.RemoteHost("first", "ready"),
            remote.RemoteHost("second", "ready"),
        )
        with remote.watch_remote(
            hosts=hosts,
            python_candidates=pinned,
            host_opener=opener,
            artifact=b"zipapp",
        ) as session:
            items = list(session)

        self.assertEqual(
            {"first", "second"},
            {
                item.host
                for item in items
                if isinstance(item, remote.RemoteWatchReady)
            },
        )
        self.assertEqual(
            [
                ("first", b"zipapp", pinned),
                ("second", b"zipapp", pinned),
            ],
            sorted(calls),
        )

    def test_round_robin_and_priority_prevent_hot_host_starvation(self):
        hosts = (
            remote.RemoteHost("a-hot", "ready"),
            remote.RemoteHost("b-cold", "ready"),
        )

        def stream_factory(argv, artifact, **kwargs):
            del artifact, kwargs
            alias = argv[-2]
            if alias == "a-hot":
                lines = [READY_FRAME]
                lines.extend(
                    event_line(session_id="hot-{}".format(index)) for index in range(20)
                )
                lines.append(END_FRAME)
                return FakeLineStream(lines)
            return FakeLineStream(
                [READY_FRAME, event_line(session_id="cold"), END_FRAME]
            )

        with remote.watch_remote(
            hosts=hosts,
            runner=self._runner,
            stream_factory=stream_factory,
            artifact=b"zipapp",
            queue_items=4,
        ) as session:
            items = list(session)

        cold_ready = next(
            index
            for index, item in enumerate(items)
            if isinstance(item, remote.RemoteWatchReady) and item.host == "b-cold"
        )
        hot_before_ready = sum(
            isinstance(item, remote.RemoteWatchEvent) and item.host == "a-hot"
            for item in items[:cold_ready]
        )
        self.assertLessEqual(hot_before_ready, 4)
        event_hosts = [
            item.host for item in items if isinstance(item, remote.RemoteWatchEvent)
        ]
        cold_event = event_hosts.index("b-cold")
        self.assertLessEqual(cold_event, 6)
        self.assertEqual(20, event_hosts.count("a-hot"))

    def test_partial_and_all_failure_are_observable_without_stopping_peers(self):
        hosts = (
            remote.RemoteHost("good", "ready"),
            remote.RemoteHost("old", "ready"),
        )

        def runner(argv, **kwargs):
            del kwargs
            version = None if argv[-2] == "old" else (3, 11, 0)
            return completed(argv, version)

        def stream_factory(argv, artifact, **kwargs):
            del artifact, kwargs
            return FakeLineStream(
                [READY_FRAME, event_line(session_id=argv[-2]), END_FRAME]
            )

        with remote.watch_remote(
            hosts=hosts,
            runner=runner,
            stream_factory=stream_factory,
            artifact=b"zipapp",
        ) as session:
            items = list(session)
            self.assertEqual(("good",), session.ready_hosts)
            self.assertFalse(session.all_failed)
        self.assertEqual(
            [("old", "python_too_old")],
            [
                (item.host, item.code)
                for item in items
                if isinstance(item, remote.RemoteWatchFailure)
            ],
        )
        self.assertTrue(
            any(
                isinstance(item, remote.RemoteWatchEvent) and item.host == "good"
                for item in items
            )
        )

        with remote.watch_remote(
            hosts=(remote.RemoteHost("old", "ready"),),
            runner=runner,
            stream_factory=stream_factory,
            artifact=b"zipapp",
        ) as session:
            failures = list(session)
            self.assertTrue(session.all_failed)
        self.assertEqual("python_too_old", failures[0].code)

    def test_empty_selected_and_from_start_are_observable(self):
        calls = []
        with remote.watch_remote(
            hosts=(),
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            artifact=b"ignored",
        ) as empty:
            self.assertTrue(empty.empty)
            self.assertEqual([], list(empty))
        self.assertEqual([], calls)

        captured = []

        def stream_factory(argv, artifact, **kwargs):
            del artifact, kwargs
            captured.append(argv)
            return FakeLineStream([READY_FRAME, END_FRAME])

        with remote.watch_remote(
            hosts=(remote.RemoteHost("edge", "ready"),),
            from_start=True,
            runner=self._runner,
            stream_factory=stream_factory,
            artifact=b"zipapp",
        ) as session:
            list(session)
        self.assertTrue(captured[0][-1].endswith("watch --all --from-start --json"))

    def test_early_close_and_external_cancel_leave_no_watch_workers(self):
        cancel = threading.Event()

        class BlockingStream(FakeLineStream):
            def __init__(self, cancel_event):
                super().__init__([READY_FRAME])
                self.cancel_event = cancel_event

            def __next__(self):
                try:
                    return super().__next__()
                except StopIteration:
                    while not self.cancel_event.is_set() and not self.closed:
                        time.sleep(0.01)
                    raise

        streams = []

        def factory(argv, artifact, **kwargs):
            del argv, artifact
            stream = BlockingStream(kwargs["cancel_event"])
            streams.append(stream)
            return stream

        session = remote.watch_remote(
            hosts=(remote.RemoteHost("edge", "ready"),),
            runner=self._runner,
            stream_factory=factory,
            artifact=b"zipapp",
            cancel_event=cancel,
        )
        self.assertIsInstance(next(session), remote.RemoteWatchReady)
        cancel.set()
        with self.assertRaises(StopIteration):
            next(session)
        self.assertTrue(session.closed)
        self.assertTrue(streams[0].closed)
        self.assertFalse(
            any(
                thread.name.startswith("sidecar-remote-watch")
                for thread in threading.enumerate()
            )
        )

    def test_simultaneous_sessions_and_cross_thread_close_are_independent(self):
        class PushStream:
            def __init__(self):
                self.items = queue.Queue()
                self.items.put(READY_FRAME)
                self.closed = False

            def __next__(self):
                item = self.items.get(timeout=3)
                if item is None:
                    raise StopIteration
                return item

            def mark_ready(self):
                return None

            def close(self):
                if not self.closed:
                    self.closed = True
                    self.items.put(None)

        streams = {}

        def factory(argv, artifact, **kwargs):
            del argv, kwargs
            stream = PushStream()
            streams[artifact] = stream
            return stream

        previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGTERM, signal.SIGHUP)
        }
        first = remote.watch_remote(
            hosts=(remote.RemoteHost("first", "ready"),),
            runner=self._runner,
            stream_factory=factory,
            artifact=b"first",
        )
        second = remote.watch_remote(
            hosts=(remote.RemoteHost("second", "ready"),),
            runner=self._runner,
            stream_factory=factory,
            artifact=b"second",
        )
        self.assertIsInstance(next(first), remote.RemoteWatchReady)
        self.assertIsInstance(next(second), remote.RemoteWatchReady)

        close_errors = []

        def close_first():
            try:
                first.close()
            except BaseException as error:
                close_errors.append(error)

        closer = threading.Thread(target=close_first)
        closer.start()
        closer.join(timeout=3)
        self.assertFalse(closer.is_alive())
        self.assertEqual([], close_errors)
        self.assertTrue(streams[b"first"].closed)
        self.assertFalse(streams[b"second"].closed)
        streams[b"second"].items.put(event_line(session_id="survives"))
        survivor = next(second)
        self.assertEqual("survives", survivor.session_id)
        second.close()
        for signum, handler in previous.items():
            self.assertIs(handler, signal.getsignal(signum))

    def test_thread_start_failure_preserves_original_and_cleans_started_workers(self):
        class StartFailure(RuntimeError):
            pass

        original = StartFailure("original-start-failure")
        real_start = threading.Thread.start
        calls = 0

        def fail_second(thread):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise original
            return real_start(thread)

        with mock.patch(
            "sidecar.remote_watch.threading.Thread.start",
            new=fail_second,
        ):
            with self.assertRaises(StartFailure) as raised:
                remote.watch_remote(
                    hosts=(
                        remote.RemoteHost("one", "ready"),
                        remote.RemoteHost("two", "ready"),
                    ),
                    runner=self._runner,
                    stream_factory=lambda *args, **kwargs: FakeLineStream(
                        [READY_FRAME, END_FRAME]
                    ),
                    artifact=b"zipapp",
                )

        self.assertIs(original, raised.exception)
        self.assertFalse(
            any(
                thread.name.startswith("sidecar-remote-watch")
                for thread in threading.enumerate()
            )
        )

    @unittest.skipUnless(os.name == "posix", "signals require POSIX")
    def test_exit_signal_kills_fake_ssh_group_and_joins_workers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_path = root / "ssh.pid"
            descendant_path = root / "descendant.pid"
            executable = root / "ssh"
            executable.write_text(
                "#!{}\n"
                "import json,os,subprocess,sys,time\n"
                "if 'watch --all' not in sys.argv[-1]:\n"
                "    print(json.dumps({{'python':[3,9,6],"
                "'executable':'/usr/bin/python3'}},separators=(',',':')))\n"
                "    raise SystemExit(0)\n"
                "sys.stdin.buffer.read()\n"
                "code=('import os,sys,time;'\n"
                "      \"open(sys.argv[1],'w').write(str(os.getpid()));\"\n"
                "      'time.sleep(60)')\n"
                "subprocess.Popen([sys.executable,'-c',code,"
                "os.environ['WATCH_DESCENDANT_PID']])\n"
                "open(os.environ['WATCH_SSH_PID'],'w').write(str(os.getpid()))\n"
                'os.write(1,b\'{{"type":"ready"}}\\n\')\n'
                "time.sleep(60)\n".format(sys.executable),
                encoding="utf-8",
            )
            executable.chmod(0o755)
            parent_code = (
                "import signal,threading\n"
                "from sidecar.remote import RemoteHost,watch_remote\n"
                "from sidecar.process_runner import bounded_execution_signal_guard\n"
                "try:\n"
                "    with bounded_execution_signal_guard():\n"
                "        with watch_remote(hosts=(RemoteHost('edge','ready'),),"
                "artifact=b'zipapp') as session:\n"
                "            next(session)\n"
                "            next(session)\n"
                "except KeyboardInterrupt:\n"
                "    if any(t.name.startswith('sidecar-remote-watch') "
                "for t in threading.enumerate()):\n"
                "        raise SystemExit(8)\n"
                "    raise SystemExit(130)\n"
            )
            environment = os.environ.copy()
            environment["PATH"] = str(root) + os.pathsep + environment["PATH"]
            environment["WATCH_SSH_PID"] = str(child_path)
            environment["WATCH_DESCENDANT_PID"] = str(descendant_path)
            parent = subprocess.Popen(
                [sys.executable, "-c", parent_code],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            child_pid = None
            descendant_pid = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and (
                    not child_path.exists() or not descendant_path.exists()
                ):
                    if parent.poll() is not None:
                        break
                    time.sleep(0.01)
                diagnostic = (
                    parent.stderr.read().decode("utf-8", "replace")
                    if parent.poll() is not None and parent.stderr is not None
                    else ""
                )
                self.assertTrue(child_path.exists(), diagnostic)
                self.assertTrue(descendant_path.exists(), diagnostic)
                # Existence is not content: both files are created and then
                # written, so read them as records rather than as files.
                read_deadline = time.monotonic() + 5
                child_pid = read_pid_when_ready(child_path, read_deadline)
                descendant_pid = read_pid_when_ready(
                    descendant_path,
                    read_deadline,
                )

                os.kill(parent.pid, signal.SIGTERM)
                self.assertEqual(130, parent.wait(timeout=4))
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline and (
                    process_exists(child_pid) or process_exists(descendant_pid)
                ):
                    time.sleep(0.01)
                self.assertFalse(process_exists(child_pid))
                self.assertFalse(process_exists(descendant_pid))
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait()
                if parent.stderr is not None:
                    parent.stderr.close()
                for pid in (child_pid, descendant_pid):
                    if pid is not None and process_exists(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


class RemoteWatchBootstrapTests(unittest.TestCase):
    def _zipapp(self, source):
        output = io.BytesIO()
        with zipfile.ZipFile(
            output, mode="w", compression=zipfile.ZIP_STORED
        ) as archive:
            archive.writestr("__main__.py", source)
        return output.getvalue()

    def _run(self, root, source, args=None, timeout=5):
        environment = os.environ.copy()
        environment["TMPDIR"] = str(root)
        return subprocess.run(
            [sys.executable, "-I", "-c", remote.REMOTE_WATCH_BOOTSTRAP]
            + (["watch", "--all", "--json"] if args is None else list(args)),
            input=self._zipapp(source),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
            timeout=timeout,
        )

    def test_real_bootstrap_ready_fragmented_event_end_and_cleanup(self):
        payload = event_line(session_id="real")
        source = (
            "import os,sys,time\n"
            "assert sys.argv[1:] == "
            "['watch','--all','--json','--stream-ready']\n"
            'os.write(1,b\'{"type":"ready"}\\n\')\n'
            "os.write(1," + repr(payload[:25]) + ")\n"
            "time.sleep(.02)\n"
            "os.write(1," + repr(payload[25:] + b"\n") + ")\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self._run(root, source)
            files = list(root.glob("agent-sidecar-watch-*.pyz"))

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [READY_FRAME, payload, END_FRAME],
            result.stdout.splitlines(),
        )
        self.assertEqual([], files)

    def test_real_bootstrap_requires_exact_internal_ready_ordering(self):
        payload = event_line(session_id="too-early")
        invalid_first_lines = (
            payload,
            b'{"type": "ready"}',
            PING_FRAME,
            END_FRAME,
            b'{"type":"error","code":"remote"}',
            b'{"type":"ready","extra":{}}',
            b"not-json",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for first_line in invalid_first_lines:
                with self.subTest(first_line=first_line):
                    source = "import os;os.write(1," + repr(first_line + b"\n") + ")"
                    result = self._run(root, source)
                    self.assertEqual(
                        [b'{"type":"error","code":"protocol"}'],
                        result.stdout.splitlines(),
                    )

            duplicate = self._run(
                root,
                'import os;os.write(1,b\'{"type":"ready"}\\n{"type":"ready"}\\n\')',
            )

        self.assertEqual(
            [READY_FRAME, b'{"type":"error","code":"protocol"}'],
            duplicate.stdout.splitlines(),
        )

    def test_real_delayed_empty_child_never_infers_ready(self):
        source = "import time;time.sleep(1.25);raise SystemExit(1)"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = time.monotonic()
            result = self._run(root, source, timeout=4)
            elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 1.0)
        self.assertEqual(
            [b'{"type":"error","code":"remote"}'],
            result.stdout.splitlines(),
        )

    def test_real_bootstrap_from_start_argv_invalid_and_oversize(self):
        source = (
            "import json,os,sys;"
            'os.write(1,b\'{"type":"ready"}\\n\');'
            "print(json.dumps({"
            "'ts':'t','agent':'a','session_id':'s','kind':'k','text':"
            "str(sys.argv[1:]),'extra':{}},separators=(',',':')))"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = self._run(
                root,
                source,
                args=("watch", "--all", "--from-start", "--json"),
            )
            rejected = self._run(
                root,
                source,
                args=("watch", "--all", "--json", "injected"),
            )
            hidden_external = self._run(
                root,
                source,
                args=("watch", "--all", "--json", "--stream-ready"),
            )
            oversized = self._run(
                root,
                "import os;"
                'os.write(1,b\'{{"type":"ready"}}\\n\');'
                "os.write(1,b'x'*({}+1)+b'\\n')".format(MAX_WATCH_LINE_BYTES),
            )

        self.assertEqual(READY_FRAME, accepted.stdout.splitlines()[0])
        accepted_event = json.loads(accepted.stdout.splitlines()[1])
        self.assertEqual(
            "['watch', '--all', '--from-start', '--json', '--stream-ready']",
            accepted_event["text"],
        )
        self.assertEqual(END_FRAME, accepted.stdout.splitlines()[-1])
        self.assertEqual(
            {"type": "error", "code": "protocol"},
            json.loads(rejected.stdout),
        )
        self.assertEqual(
            {"type": "error", "code": "protocol"},
            json.loads(hidden_external.stdout),
        )
        self.assertEqual(
            {"type": "error", "code": "resource_limit"},
            json.loads(oversized.stdout.splitlines()[-1]),
        )

    def test_real_bootstrap_flushes_bounded_heartbeat_while_silent(self):
        source = 'import os,time;os.write(1,b\'{"type":"ready"}\\n\');time.sleep(2)'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = os.environ.copy()
            environment["TMPDIR"] = str(root)
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", remote.REMOTE_WATCH_BOOTSTRAP]
                + ["watch", "--all", "--json"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            stderr = b""
            try:
                assert process.stdin is not None
                process.stdin.write(self._zipapp(source))
                process.stdin.close()
                assert process.stdout is not None
                self.assertEqual(READY_FRAME + b"\n", process.stdout.readline())
                started = time.monotonic()
                self.assertEqual(PING_FRAME + b"\n", process.stdout.readline())
                elapsed = time.monotonic() - started
                remaining = process.stdout.read().splitlines()
                returncode = process.wait(timeout=3)
                assert process.stderr is not None
                stderr = process.stderr.read()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

            self.assertEqual(0, returncode, stderr)
            self.assertGreaterEqual(elapsed, 0.4)
            self.assertLessEqual(elapsed, 1.1)
            self.assertEqual(END_FRAME, remaining[-1])
            self.assertTrue(remaining[:-1])
            self.assertLessEqual(len(remaining[:-1]), 2)
            self.assertTrue(all(frame == PING_FRAME for frame in remaining[:-1]))
            self.assertEqual([], list(root.glob("agent-sidecar-watch-*.pyz")))

    @unittest.skipUnless(os.name == "posix", "pipe cleanup requires POSIX")
    def test_real_silent_pipe_disconnect_kills_child_and_unlinks_temp(self):
        source = (
            "import os,time;"
            'os.write(1,b\'{"type":"ready"}\\n\');'
            "open(os.environ['WATCH_CHILD_PID'],'w').write(str(os.getpid()));"
            "time.sleep(60)"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_path = root / "disconnect-child.pid"
            environment = os.environ.copy()
            environment["TMPDIR"] = str(root)
            environment["WATCH_CHILD_PID"] = str(pid_path)
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", remote.REMOTE_WATCH_BOOTSTRAP]
                + ["watch", "--all", "--json"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            child_pid = None
            stderr = b""
            try:
                assert process.stdin is not None
                process.stdin.write(self._zipapp(source))
                process.stdin.close()
                assert process.stdout is not None
                self.assertEqual(READY_FRAME + b"\n", process.stdout.readline())
                child_pid = read_pid_when_ready(pid_path, time.monotonic() + 1)

                disconnected_at = time.monotonic()
                process.stdout.close()
                returncode = process.wait(timeout=3)
                elapsed = time.monotonic() - disconnected_at
                assert process.stderr is not None
                stderr = process.stderr.read()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                if child_pid is not None and process_exists(child_pid):
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

            self.assertEqual(0, returncode, stderr)
            self.assertLessEqual(elapsed, 2.5)
            self.assertFalse(process_exists(child_pid))
            self.assertEqual([], list(root.glob("agent-sidecar-watch-*.pyz")))

    @unittest.skipUnless(os.name == "posix", "signals require POSIX")
    def test_real_cancel_before_ready_kills_child_and_unlinks_temp(self):
        source = (
            "import os,time;"
            "open(os.environ['WATCH_CHILD_PID'],'w').write(str(os.getpid()));"
            "time.sleep(60)"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_path = root / "pre-ready-child.pid"
            environment = os.environ.copy()
            environment["TMPDIR"] = str(root)
            environment["WATCH_CHILD_PID"] = str(pid_path)
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", remote.REMOTE_WATCH_BOOTSTRAP]
                + ["watch", "--all", "--json"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            child_pid = None
            stdout = b""
            stderr = b""
            try:
                assert process.stdin is not None
                process.stdin.write(self._zipapp(source))
                process.stdin.close()
                child_pid = read_pid_when_ready(pid_path, time.monotonic() + 2)
                self.assertTrue(list(root.glob("agent-sidecar-watch-*.pyz")))

                os.kill(process.pid, signal.SIGTERM)
                returncode = process.wait(timeout=3)
                assert process.stdout is not None
                stdout = process.stdout.read()
                assert process.stderr is not None
                stderr = process.stderr.read()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                if child_pid is not None and process_exists(child_pid):
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

            self.assertEqual(0, returncode, stderr)
            self.assertEqual(b"", stdout)
            self.assertFalse(process_exists(child_pid))
            self.assertEqual([], list(root.glob("agent-sidecar-watch-*.pyz")))

    @unittest.skipUnless(os.name == "posix", "signals require POSIX")
    def test_real_bootstrap_signal_kills_child_and_unlinks_temp(self):
        source = (
            "import os,time;"
            'os.write(1,b\'{"type":"ready"}\\n\');'
            "open(os.environ['WATCH_CHILD_PID'],'w').write(str(os.getpid()));"
            "time.sleep(60)"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pid_path = root / "child.pid"
            environment = os.environ.copy()
            environment["TMPDIR"] = str(root)
            environment["WATCH_CHILD_PID"] = str(pid_path)
            process = subprocess.Popen(
                [sys.executable, "-I", "-c", remote.REMOTE_WATCH_BOOTSTRAP]
                + ["watch", "--all", "--json"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            stdout = b""
            stderr = b""
            try:
                assert process.stdin is not None
                process.stdin.write(self._zipapp(source))
                process.stdin.close()
                assert process.stdout is not None
                self.assertEqual(READY_FRAME + b"\n", process.stdout.readline())
                child_pid = read_pid_when_ready(pid_path, time.monotonic() + 2)
                for index in range(60):
                    signum = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)[index % 3]
                    try:
                        os.kill(process.pid, signum)
                    except ProcessLookupError:
                        break
                returncode = process.wait(timeout=3)
                stdout = process.stdout.read()
                assert process.stderr is not None
                stderr = process.stderr.read()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()

            self.assertEqual(0, returncode, stderr)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
            self.assertEqual(b"", stdout)
            self.assertEqual([], list(root.glob("agent-sidecar-watch-*.pyz")))

    def test_real_zipapp_clean_home_fails_without_false_ready(self):
        artifact = remote.build_zipapp_bytes()
        with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
            members = set(archive.namelist())
            self.assertTrue(
                {
                    "sidecar/cli.py",
                    "sidecar/client.py",
                    "sidecar/tail.py",
                }.issubset(members)
            )
            self.assertIn(
                b"--stream-ready",
                archive.read("sidecar/cli.py"),
            )
            self.assertIn(b"on_ready", archive.read("sidecar/client.py"))
            self.assertIn(b"on_ready", archive.read("sidecar/tail.py"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = os.environ.copy()
            environment["HOME"] = str(root / "home")
            environment["TMPDIR"] = str(root)
            for run in range(20):
                result = subprocess.run(
                    [sys.executable, "-I", "-c", remote.REMOTE_WATCH_BOOTSTRAP]
                    + ["watch", "--all", "--json"],
                    input=artifact,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    check=False,
                    timeout=15,
                )

                self.assertEqual(0, result.returncode, (run, result.stderr))
                self.assertEqual(
                    [b'{"type":"error","code":"remote"}'],
                    result.stdout.splitlines(),
                    run,
                )
            self.assertEqual([], list(root.glob("agent-sidecar-watch-*.pyz")))


if __name__ == "__main__":
    unittest.main()
