import gc
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from unittest import mock

import sidecar.adapters.dsh as dsh_module
from sidecar.adapters.base import (
    as_mapping,
    content_block_events,
    epoch_seconds,
    local_timestamp,
    read_json_object,
)
from sidecar.adapters.dsh import DSHAdapter, ReplayPage, replay_dsh_events
from sidecar.model import Session, Status
from sidecar.tail import SessionTailer


OLD_REPLAY_BOUNDARY = 8 * 1024 * 1024
ZSTD = shutil.which("zstd")


def _write_record(stream, record):
    raw = json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
    stream.write(raw)
    return len(raw)


def _write_large_transcript(path):
    padding = "x" * (64 * 1024)
    seq = 0
    with path.open("wb") as stream:
        while stream.tell() <= OLD_REPLAY_BOUNDARY:
            seq += 1
            _write_record(
                stream,
                {
                    "seq": seq,
                    "type": "padding",
                    "data": {"text": padding},
                },
            )
        boundary_seq = seq
        for text in ("tail one", "tail two"):
            seq += 1
            _write_record(
                stream,
                {
                    "seq": seq,
                    "type": "user/message",
                    "time": 1_787_429_064_000 + seq,
                    "data": {"content": [{"type": "text", "text": text}]},
                },
            )
    return boundary_seq, (boundary_seq + 1, boundary_seq + 2)


def _make_slow_binary(path):
    """Publish one record and a readiness handshake, then stall."""

    path.write_text(
        "#!/bin/sh\n"
        "printf '{\"seq\":1,\"type\":\"turn/start\"}\\n'\n"
        "printf ready > \"${AGENT_SIDECAR_TEST_REPLAY_READY:?}\"\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o755)


def _make_passthrough_binary(path):
    path.write_text(
        """#!/usr/bin/env python3
import sys

with open(sys.argv[-1], "rb") as source:
    while True:
        chunk = source.read(65536)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
""",
        encoding="utf-8",
    )
    os.chmod(path, 0o755)


def _make_discovery_passthrough_binary(path):
    path.write_text(
        "#!/bin/sh\n"
        "exec /bin/cat\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o755)


def _make_stalling_discovery_binary(path):
    path.write_text(
        "#!/bin/sh\n"
        "exec /bin/sleep 30\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o755)


def _write_durable_log(dsh_home, session_id, cwd="/fixture/project", **changes):
    header = {
        "type": "session",
        "version": 0,
        "id": session_id,
        "cwd": cwd,
        "createdAt": 1_787_429_064_000,
        "delegationDepth": 0,
    }
    header.update(changes)
    path = dsh_module._transcript_path(dsh_home, cwd, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        _write_record(stream, header)
        _write_record(
            stream,
            {
                "seq": 0,
                "type": "turn/end",
                "time": 1_787_429_064_100,
                "data": {"reason": {"kind": "completed"}},
            },
        )
    return path


def _write_projection_cache(dsh_home, session_id, cwd, title):
    path = dsh_home / "storages" / "session_projcache.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "tables": {
                    "sessions": {
                        session_id: {
                            "identity": {
                                "cwd": cwd,
                                "createdAt": 1_787_429_064_000,
                            },
                            "rows": {
                                "title": {"seq": 7, "val": title},
                                "sessionStats": {
                                    "seq": 7,
                                    "val": {"pendingCalls": {}},
                                },
                                "plan": {"seq": 7, "val": {}},
                            },
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )


class _PassthroughDSHAdapter(DSHAdapter):
    def __init__(self, binary):
        super().__init__(
            discovery_zstd_binary="agent-sidecar-definitely-missing-zstd"
        )
        self.binary = binary
        self.after_seqs = []

    def replay(self, session, after_seq=None, max_records=1024):
        self.after_seqs.append(after_seq)
        return replay_dsh_events(
            Path(session.transcript),
            after_seq=after_seq,
            max_records=max_records,
            zstd_binary=self.binary,
        )


class DSHDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.dsh_home = self.root / "configured-dsh"
        self.passthrough = self.root / "discovery-passthrough"
        _make_discovery_passthrough_binary(self.passthrough)
        self.adapter = DSHAdapter(discovery_zstd_binary=str(self.passthrough))

    def tearDown(self):
        self.adapter.close()
        self.temporary.cleanup()

    def discover(self, dsh_home=None, candidate_timeout=None):
        selected = self.dsh_home if dsh_home is None else dsh_home
        with mock.patch.dict(os.environ, {"DSH_HOME": str(selected)}):
            if candidate_timeout is None:
                return list(self.adapter.discover(self.home))
            with mock.patch.object(
                dsh_module,
                "_DISCOVERY_CANDIDATE_TIMEOUT_S",
                candidate_timeout,
            ):
                return list(self.adapter.discover(self.home))

    def test_cache_missing_headless_session_is_discovered_from_durable_store(self):
        transcript = _write_durable_log(
            self.dsh_home,
            "headless-only",
            cwd="/fixture/headless-project",
        )

        sessions = self.discover()

        self.assertEqual(["headless-only"], [session.session_id for session in sessions])
        session = sessions[0]
        self.assertEqual("/fixture/headless-project", session.project)
        self.assertEqual(str(transcript), session.transcript)
        self.assertEqual("headless-project", session.title)
        self.assertEqual(Status.IDLE, session.status)
        self.assertEqual("session_store", session.extra["source"])
        self.assertTrue(session.extra["durable_only"])

    def test_projection_cache_deduplicates_and_remains_authoritative(self):
        session_id = "cached-and-durable"
        cwd = "/fixture/cache-project"
        _write_durable_log(self.dsh_home, session_id, cwd=cwd)
        _write_projection_cache(self.dsh_home, session_id, cwd, "Cache title")

        with mock.patch.object(
            dsh_module,
            "_read_durable_header",
            wraps=dsh_module._read_durable_header,
        ) as decode:
            sessions = self.discover()

        self.assertEqual([session_id], [session.session_id for session in sessions])
        self.assertEqual("Cache title", sessions[0].title)
        self.assertEqual("session_projcache", sessions[0].extra["source"])
        self.assertNotIn("durable_only", sessions[0].extra)
        self.assertEqual(7, sessions[0].extra["seq"])
        self.assertEqual(0, decode.call_count)

    def test_fast_exit_decoder_pipe_is_drained_after_process_exit(self):
        payload = json.dumps(
            {
                "type": "session",
                "version": 0,
                "id": "fast-exit",
                "cwd": "/fixture/project",
                "createdAt": 1_787_429_064_000,
                "delegationDepth": 0,
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        source = self.root / "source"
        source.write_bytes(b"bound input")
        source_fd = os.open(str(source), os.O_RDONLY)
        selections = []

        class ControlledStdout:
            def fileno(self):
                return 101

            def close(self):
                return None

        class ExitedProcess:
            stdout = ControlledStdout()

            def poll(self):
                return 0

        class ControlledSelector:
            def register(self, *args, **kwargs):
                return None

            def select(self, timeout=None):
                selections.append(timeout)
                return [] if len(selections) == 1 else [(object(), None)]

            def close(self):
                return None

        process = ExitedProcess()
        bound = self.adapter._discovery_decoder
        self.assertIsNotNone(bound)
        executable_fd = bound.descriptor
        try:
            with mock.patch.object(
                dsh_module.subprocess,
                "Popen",
                return_value=process,
            ) as popen, mock.patch.object(
                dsh_module.selectors,
                "DefaultSelector",
                return_value=ControlledSelector(),
            ), mock.patch.object(
                dsh_module.os,
                "read",
                return_value=payload,
            ):
                decoded = dsh_module._read_durable_header(
                    source_fd,
                    bound,
                    1.0,
                )
        finally:
            self.adapter.close()
            os.close(source_fd)

        self.assertEqual(dsh_module._DecodeOutcome.SUCCESS, decoded.outcome)
        self.assertEqual("fast-exit", decoded.header["id"])
        self.assertEqual(2, len(selections))
        _, kwargs = popen.call_args
        self.assertNotEqual(
            str(self.adapter._discovery_zstd.path),
            kwargs["executable"],
        )
        if dsh_module.sys.platform.startswith("linux"):
            self.assertEqual((executable_fd,), kwargs["pass_fds"])
            self.assertEqual(
                "/dev/fd/{}".format(executable_fd),
                kwargs["executable"],
            )
        else:
            self.assertEqual((), kwargs["pass_fds"])
        with self.assertRaises(OSError):
            os.fstat(executable_fd)

    def test_abnormal_decoder_exit_is_transient(self):
        source = self.root / "source"
        source.write_bytes(b"bound input")
        source_fd = os.open(str(source), os.O_RDONLY)

        class ControlledStdout:
            def fileno(self):
                return 102

            def close(self):
                return None

        class FailedProcess:
            stdout = ControlledStdout()

            def poll(self):
                return 7

        class ControlledSelector:
            def register(self, *args, **kwargs):
                return None

            def select(self, timeout=None):
                return [(object(), None)]

            def close(self):
                return None

        bound = self.adapter._discovery_decoder
        self.assertIsNotNone(bound)
        try:
            with mock.patch.object(
                dsh_module.subprocess,
                "Popen",
                return_value=FailedProcess(),
            ), mock.patch.object(
                dsh_module.selectors,
                "DefaultSelector",
                return_value=ControlledSelector(),
            ), mock.patch.object(
                dsh_module.os,
                "read",
                return_value=b"{not-json}\n",
            ):
                decoded = dsh_module._read_durable_header(
                    source_fd,
                    bound,
                    1.0,
                )
        finally:
            self.adapter.close()
            os.close(source_fd)

        self.assertEqual(
            dsh_module._DecodeOutcome.TRANSIENT_FAILURE,
            decoded.outcome,
        )

    @unittest.skipUnless(ZSTD, "zstd binary is unavailable")
    def test_real_zstd_fast_exit_durable_header_is_discovered(self):
        transcript = _write_durable_log(self.dsh_home, "real-zstd")
        source = self.root / "real-zstd-source.jsonl"
        transcript.replace(source)
        subprocess.run(
            [ZSTD, "-q", "-f", str(source), "-o", str(transcript)],
            check=True,
            timeout=10,
        )
        adapter = DSHAdapter()
        self.addCleanup(adapter.close)

        with mock.patch.dict(
            os.environ,
            {"DSH_HOME": str(self.dsh_home)},
        ):
            sessions = list(adapter.discover(self.home))

        self.assertEqual(["real-zstd"], [session.session_id for session in sessions])

    @unittest.skipUnless(ZSTD, "zstd binary is unavailable")
    def test_real_zstd_50_fresh_adapter_identity_changes_have_no_misses(self):
        session_id = "real-zstd-cold-start"
        cwd = "/fixture/cold-start"
        transcript = dsh_module._transcript_path(
            self.dsh_home,
            cwd,
            session_id,
        )
        transcript.parent.mkdir(parents=True)
        source = self.root / "cold-start.jsonl"
        durations = []
        misses = []
        previous_identity = None

        self.assertEqual(3.5, dsh_module._DISCOVERY_CANDIDATE_TIMEOUT_S)
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.dsh_home)}):
            for round_number in range(50):
                with source.open("wb") as stream:
                    _write_record(
                        stream,
                        {
                            "type": "session",
                            "version": 0,
                            "id": session_id,
                            "cwd": cwd,
                            "createdAt": 1_787_429_064_000 + round_number,
                            "delegationDepth": 0,
                            "padding": "x" * round_number,
                        },
                    )
                staging = transcript.with_name(
                    "cold-start-{}.jsonl.zstd".format(round_number)
                )
                subprocess.run(
                    [ZSTD, "-q", "-f", str(source), "-o", str(staging)],
                    check=True,
                    timeout=10,
                )
                os.replace(staging, transcript)
                identity = dsh_module._file_identity(transcript.stat())
                if previous_identity is not None:
                    self.assertNotEqual(previous_identity, identity)
                previous_identity = identity

                started = time.monotonic()
                with mock.patch.object(
                    dsh_module,
                    "_bind_executable",
                    wraps=dsh_module._bind_executable,
                ) as bind:
                    adapter = DSHAdapter()
                decoder = adapter._discovery_decoder
                self.assertIsNotNone(decoder)
                try:
                    self.assertEqual(1, bind.call_count)
                    with mock.patch.object(
                        dsh_module,
                        "_bind_executable",
                        side_effect=AssertionError(
                            "cold discovery attempted to rebind zstd"
                        ),
                    ) as rebind:
                        sessions = list(adapter.discover(self.home))
                    self.assertEqual(0, rebind.call_count)
                finally:
                    adapter.close()
                self.assertTrue(decoder.closed)
                durations.append(time.monotonic() - started)
                if [session_id] != [row.session_id for row in sessions]:
                    misses.append(round_number)

        ordered = sorted(durations)
        p95 = ordered[((95 * len(ordered) + 99) // 100) - 1]
        maximum = max(ordered)
        print(
            "DSH real-zstd 50 cold dynamic adapters: "
            "misses={} p95={:.4f}s max={:.4f}s".format(
                len(misses),
                p95,
                maximum,
            )
        )
        self.assertEqual([], misses)
        self.assertLess(p95, 2.5)
        self.assertLess(maximum, 4.5)

    @unittest.skipUnless(ZSTD, "zstd binary is unavailable")
    def test_real_zstd_50_identity_changes_meet_production_cadence(self):
        session_id = "real-zstd-changing"
        cwd = "/fixture/performance"
        transcript = dsh_module._transcript_path(
            self.dsh_home,
            cwd,
            session_id,
        )
        transcript.parent.mkdir(parents=True)
        source = self.root / "changing.jsonl"
        durations = []
        misses = []
        previous_identity = None

        self.assertEqual(3.5, dsh_module._DISCOVERY_CANDIDATE_TIMEOUT_S)
        with mock.patch.object(
            dsh_module,
            "_bind_executable",
            wraps=dsh_module._bind_executable,
        ) as bind:
            adapter = DSHAdapter()
        self.addCleanup(adapter.close)
        decoder = adapter._discovery_decoder
        self.assertIsNotNone(decoder)
        launch_path = decoder.launch_path
        self.assertEqual(1, bind.call_count)

        with mock.patch.dict(
            os.environ,
            {"DSH_HOME": str(self.dsh_home)},
        ), mock.patch.object(
            dsh_module,
            "_bind_executable",
            side_effect=AssertionError("discovery attempted to rebind zstd"),
        ) as rebind:
            for round_number in range(50):
                with source.open("wb") as stream:
                    _write_record(
                        stream,
                        {
                            "type": "session",
                            "version": 0,
                            "id": session_id,
                            "cwd": cwd,
                            "createdAt": 1_787_429_064_000 + round_number,
                            "delegationDepth": 0,
                            "padding": "x" * round_number,
                        },
                    )
                staging = transcript.with_name(
                    "session-{}.jsonl.zstd".format(round_number)
                )
                subprocess.run(
                    [ZSTD, "-q", "-f", str(source), "-o", str(staging)],
                    check=True,
                    timeout=10,
                )
                os.replace(staging, transcript)
                identity = dsh_module._file_identity(transcript.stat())
                if previous_identity is not None:
                    self.assertNotEqual(previous_identity, identity)
                previous_identity = identity

                started = time.monotonic()
                sessions = list(adapter.discover(self.home))
                durations.append(time.monotonic() - started)
                if [session_id] != [row.session_id for row in sessions]:
                    misses.append(round_number)

        self.assertEqual(0, rebind.call_count)
        ordered = sorted(durations)
        p95 = ordered[((95 * len(ordered) + 99) // 100) - 1]
        maximum = max(ordered)
        print(
            "DSH real-zstd 50-round discovery: "
            "misses={} p95={:.4f}s max={:.4f}s".format(
                len(misses),
                p95,
                maximum,
            )
        )
        self.assertEqual([], misses)
        self.assertIs(decoder, adapter._discovery_decoder)
        self.assertEqual(launch_path, adapter._discovery_decoder.launch_path)
        self.assertLess(p95, 0.5)
        self.assertLess(maximum, 2.0)

    def test_decoder_is_bound_once_and_reused_across_file_identities(self):
        transcript = _write_durable_log(self.dsh_home, "reuse-binding")
        first_identity = dsh_module._file_identity(transcript.stat())

        with mock.patch.object(
            dsh_module,
            "_bind_executable",
            wraps=dsh_module._bind_executable,
        ) as bind:
            adapter = DSHAdapter(discovery_zstd_binary=str(self.passthrough))
        self.addCleanup(adapter.close)
        decoder = adapter._discovery_decoder
        self.assertIsNotNone(decoder)
        launch_path = decoder.launch_path

        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.dsh_home)}):
            first = list(adapter.discover(self.home))
            retained = self.root / "retained-transcript"
            transcript.replace(retained)
            _write_durable_log(
                self.dsh_home,
                "reuse-binding",
                createdAt=1_787_429_064_001,
            )
            second_identity = dsh_module._file_identity(transcript.stat())
            second = list(adapter.discover(self.home))

        self.assertNotEqual(first_identity, second_identity)
        self.assertEqual(["reuse-binding"], [row.session_id for row in first])
        self.assertEqual(["reuse-binding"], [row.session_id for row in second])
        self.assertEqual(1, bind.call_count)
        self.assertIs(decoder, adapter._discovery_decoder)
        self.assertEqual(launch_path, adapter._discovery_decoder.launch_path)

    def test_binding_failure_degrades_to_projection_cache_only(self):
        _write_projection_cache(
            self.dsh_home,
            "cache-only",
            "/fixture/cache-only",
            "Cache only",
        )
        _write_durable_log(self.dsh_home, "durable-needs-decoder")

        with mock.patch.object(
            dsh_module,
            "_bind_executable",
            side_effect=OSError("synthetic binding failure"),
        ) as bind:
            adapter = DSHAdapter(discovery_zstd_binary=str(self.passthrough))
        self.addCleanup(adapter.close)
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.dsh_home)}):
            sessions = list(adapter.discover(self.home))

        self.assertEqual(1, bind.call_count)
        self.assertIsNone(adapter._discovery_decoder)
        self.assertEqual(["cache-only"], [row.session_id for row in sessions])
        self.assertEqual("session_projcache", sessions[0].extra["source"])

    def test_bound_decoder_close_is_private_idempotent_and_explicit(self):
        adapter = DSHAdapter(discovery_zstd_binary=str(self.passthrough))
        decoder = adapter._discovery_decoder
        self.assertIsNotNone(decoder)
        descriptor = decoder.descriptor
        launch_path = Path(decoder.launch_path)

        if dsh_module.sys.platform == "darwin":
            self.assertEqual(
                0o700,
                dsh_module.stat.S_IMODE(launch_path.parent.stat().st_mode),
            )
            self.assertEqual(
                0o500,
                dsh_module.stat.S_IMODE(launch_path.stat().st_mode),
            )

        adapter.close()
        adapter.close()

        self.assertTrue(decoder.closed)
        if descriptor >= 0:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        if dsh_module.sys.platform == "darwin":
            self.assertFalse(launch_path.exists())

    def test_bound_decoder_finalizer_releases_unclosed_resource(self):
        adapter = DSHAdapter(discovery_zstd_binary=str(self.passthrough))
        decoder = adapter._discovery_decoder
        self.assertIsNotNone(decoder)
        descriptor = decoder.descriptor
        launch_path = Path(decoder.launch_path)
        adapter_ref = weakref.ref(adapter)
        decoder_ref = weakref.ref(decoder)

        del decoder
        del adapter
        for _ in range(3):
            gc.collect()
            if adapter_ref() is None and decoder_ref() is None:
                break

        self.assertIsNone(adapter_ref())
        self.assertIsNone(decoder_ref())
        if descriptor >= 0:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        if dsh_module.sys.platform == "darwin":
            self.assertFalse(launch_path.exists())

    def test_adapter_close_waits_for_active_discovery(self):
        _write_durable_log(self.dsh_home, "thread-safe-close")
        adapter = DSHAdapter(discovery_zstd_binary=str(self.passthrough))
        self.addCleanup(adapter.close)
        entered = threading.Event()
        release = threading.Event()
        close_started = threading.Event()
        close_finished = threading.Event()
        discovered = []

        def blocked_decode(source_fd, executable, timeout):
            del source_fd, executable, timeout
            entered.set()
            release.wait(2.0)
            return dsh_module._HeaderDecode(
                dsh_module._DecodeOutcome.TRANSIENT_FAILURE
            )

        def run_discovery():
            discovered.extend(adapter.discover(self.home))

        def run_close():
            close_started.set()
            adapter.close()
            close_finished.set()

        with mock.patch.dict(
            os.environ,
            {"DSH_HOME": str(self.dsh_home)},
        ), mock.patch.object(
            dsh_module,
            "_read_durable_header",
            side_effect=blocked_decode,
        ):
            discovery_thread = threading.Thread(target=run_discovery)
            discovery_thread.start()
            self.assertTrue(entered.wait(1.0))
            close_thread = threading.Thread(target=run_close)
            close_thread.start()
            self.assertTrue(close_started.wait(1.0))
            self.assertFalse(close_finished.wait(0.05))
            release.set()
            discovery_thread.join(2.0)
            close_thread.join(2.0)

        self.assertFalse(discovery_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertEqual([], discovered)
        self.assertTrue(adapter._discovery_decoder.closed)

    def test_bound_executable_fd_defeats_path_replacement(self):
        source = _write_durable_log(self.dsh_home, "bound-executable")
        executable = self.root / "bound-zstd"
        retained = self.root / "retained-zstd"
        marker = self.root / "attacker-ran"
        _make_discovery_passthrough_binary(executable)
        adapter = DSHAdapter(discovery_zstd_binary=str(executable))
        bound = adapter._discovery_decoder
        self.assertIsNotNone(bound)
        source_fd = os.open(str(source), os.O_RDONLY)
        real_popen = subprocess.Popen
        inherited_fds = []
        launch_paths = []

        def replace_before_exec(*args, **kwargs):
            executable.replace(retained)
            executable.write_text(
                "#!/bin/sh\n"
                "printf attacked > '{}'\n".format(
                    str(marker).replace("'", "'\"'\"'")
                ),
                encoding="utf-8",
            )
            os.chmod(executable, 0o755)
            inherited_fds.extend(kwargs.get("pass_fds", ()))
            launch_paths.append(kwargs["executable"])
            return real_popen(*args, **kwargs)

        with mock.patch.object(
            dsh_module.subprocess,
            "Popen",
            side_effect=replace_before_exec,
        ):
            try:
                decoded = dsh_module._read_durable_header(
                    source_fd,
                    bound,
                    4.0,
                )
            finally:
                adapter.close()
                os.close(source_fd)

        self.assertEqual(dsh_module._DecodeOutcome.SUCCESS, decoded.outcome)
        self.assertEqual("bound-executable", decoded.header["id"])
        self.assertFalse(marker.exists())
        self.assertEqual(1, len(launch_paths))
        self.assertNotEqual(str(executable), launch_paths[0])
        if dsh_module.sys.platform.startswith("linux"):
            self.assertEqual(1, len(inherited_fds))
            with self.assertRaises(OSError):
                os.fstat(inherited_fds[0])
        else:
            self.assertEqual([], inherited_fds)
            self.assertFalse(Path(launch_paths[0]).exists())

    def test_dsh_home_override_wins_and_blank_uses_home_fallback(self):
        _write_durable_log(self.dsh_home, "configured-session")
        fallback = self.home / ".dsh"
        _write_durable_log(fallback, "fallback-session")

        configured = self.discover()
        with mock.patch.dict(
            os.environ,
            {"DSH_HOME": "   "},
        ):
            blank = list(self.adapter.discover(self.home))

        self.assertEqual(["configured-session"], [row.session_id for row in configured])
        self.assertEqual(["fallback-session"], [row.session_id for row in blank])

    def test_relative_and_tilde_dsh_home_overrides_fail_closed(self):
        _write_durable_log(self.home / ".dsh", "must-not-leak")

        for configured in ("relative-dsh", "../escape", "~/.dsh"):
            with self.subTest(configured=configured):
                with mock.patch.dict(os.environ, {"DSH_HOME": configured}):
                    self.assertEqual([], list(self.adapter.discover(self.home)))

    def test_bad_candidates_do_not_block_good_and_children_are_skipped(self):
        bad = dsh_module._transcript_path(
            self.dsh_home,
            "/fixture/project",
            "bad-a",
        )
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"{not-json}\n")
        _write_durable_log(self.dsh_home, "empty-cwd", cwd="")
        _write_durable_log(self.dsh_home, "child-b", delegationDepth=1)
        _write_durable_log(
            self.dsh_home,
            "child-c",
            parentSession="synthetic-parent",
        )
        _write_durable_log(self.dsh_home, "good-z")

        sessions = self.discover()

        self.assertEqual(["good-z"], [session.session_id for session in sessions])

    def test_cwd_must_be_present_nonempty_and_absolute(self):
        missing = _write_durable_log(
            self.dsh_home,
            "missing-cwd",
            cwd="/fixture/missing",
        )
        header = {
            "type": "session",
            "version": 0,
            "id": "missing-cwd",
            "createdAt": 1_787_429_064_000,
            "delegationDepth": 0,
        }
        with missing.open("wb") as stream:
            _write_record(stream, header)
        _write_durable_log(
            self.dsh_home,
            "relative-cwd",
            cwd="fixture/relative",
        )
        _write_durable_log(self.dsh_home, "absolute-cwd")

        sessions = self.discover()

        self.assertEqual(["absolute-cwd"], [session.session_id for session in sessions])

    def test_symlink_nonregular_corrupt_and_oversized_candidates_are_skipped(self):
        symlink_path = dsh_module._transcript_path(
            self.dsh_home,
            "/fixture/project",
            "linked",
        )
        symlink_path.parent.mkdir(parents=True)
        target = self.root / "outside-log"
        target.write_bytes(b'{"type":"session"}\n')
        symlink_path.symlink_to(target)

        nonregular = dsh_module._transcript_path(
            self.dsh_home,
            "/fixture/project",
            "directory",
        )
        nonregular.mkdir(parents=True)

        corrupt = dsh_module._transcript_path(
            self.dsh_home,
            "/fixture/project",
            "corrupt",
        )
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"not a compressed session\n")

        _write_durable_log(self.dsh_home, "oversized")
        with mock.patch.object(dsh_module, "_DISCOVERY_COMPRESSED_BYTES", 32):
            sessions = self.discover()

        self.assertEqual([], sessions)

    def test_durable_candidate_limit_is_deterministic(self):
        for session_id in ("cap-a", "cap-b", "cap-c"):
            _write_durable_log(self.dsh_home, session_id)

        with mock.patch.object(dsh_module, "_DISCOVERY_MAX_CANDIDATES", 2):
            sessions = self.discover()

        self.assertEqual(
            ["cap-a", "cap-b"],
            sorted(session.session_id for session in sessions),
        )

    def test_duplicate_id_after_candidate_decode_limit_fails_closed_for_id(self):
        _write_durable_log(self.dsh_home, "duplicate", cwd="/fixture/a")
        _write_durable_log(self.dsh_home, "duplicate", cwd="/fixture/z")

        with mock.patch.object(dsh_module, "_DISCOVERY_MAX_CANDIDATES", 1):
            sessions = self.discover()

        self.assertEqual([], sessions)

    def test_entry_budget_truncation_does_not_accept_earlier_candidate(self):
        _write_durable_log(self.dsh_home, "entry-a")
        _write_durable_log(self.dsh_home, "entry-b")

        with mock.patch.object(dsh_module, "_DISCOVERY_MAX_ENTRIES", 2):
            sessions = self.discover()

        self.assertEqual([], sessions)

    def test_bound_descriptor_rejects_path_swap_to_external_header(self):
        session_id = "swap-target"
        cwd = "/fixture/swap"
        transcript = _write_durable_log(self.dsh_home, session_id, cwd=cwd)
        transcript.write_bytes(b"{not-json}\n")
        external = self.root / "external-session"
        with external.open("wb") as stream:
            _write_record(
                stream,
                {
                    "type": "session",
                    "version": 0,
                    "id": session_id,
                    "cwd": cwd,
                    "createdAt": 1_787_429_064_000,
                    "delegationDepth": 0,
                },
            )
        retained = self.root / "retained-session"
        read_header = dsh_module._read_durable_header

        def swap_while_decoding(source_fd, executable, timeout):
            transcript.replace(retained)
            transcript.symlink_to(external)
            try:
                return read_header(source_fd, executable, timeout)
            finally:
                transcript.unlink()
                retained.replace(transcript)

        with mock.patch.object(
            dsh_module,
            "_read_durable_header",
            side_effect=swap_while_decoding,
        ):
            sessions = self.discover()

        self.assertEqual([], sessions)

    def test_header_cache_avoids_repeat_decode_and_invalidates_on_change(self):
        transcript = _write_durable_log(self.dsh_home, "cache-header")
        with mock.patch.object(
            dsh_module,
            "_read_durable_header",
            wraps=dsh_module._read_durable_header,
        ) as decode:
            first = self.discover()
            second = self.discover()
            first_count = decode.call_count
            old_mtime = transcript.stat().st_mtime_ns
            _write_durable_log(
                self.dsh_home,
                "cache-header",
                createdAt=1_787_429_064_001,
            )
            os.utime(
                transcript,
                ns=(old_mtime, max(old_mtime + 1, transcript.stat().st_mtime_ns)),
            )
            changed = self.discover()

        self.assertEqual(["cache-header"], [row.session_id for row in first])
        self.assertEqual(["cache-header"], [row.session_id for row in second])
        self.assertEqual(["cache-header"], [row.session_id for row in changed])
        self.assertEqual(1, first_count)
        self.assertEqual(2, decode.call_count)

    def test_transient_decode_is_retried_then_success_is_cached(self):
        _write_durable_log(self.dsh_home, "retry-transient")
        real_popen = subprocess.Popen
        calls = 0

        def fail_spawn_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("synthetic transient spawn failure")
            return real_popen(*args, **kwargs)

        with mock.patch.object(
            dsh_module.subprocess,
            "Popen",
            side_effect=fail_spawn_once,
        ):
            first = self.discover()
            second = self.discover()
            third = self.discover()

        self.assertEqual([], first)
        self.assertEqual(["retry-transient"], [row.session_id for row in second])
        self.assertEqual(["retry-transient"], [row.session_id for row in third])
        self.assertEqual(2, calls)

    def test_deterministic_invalid_header_is_negative_cached(self):
        transcript = dsh_module._transcript_path(
            self.dsh_home,
            "/fixture/project",
            "cached-corrupt",
        )
        transcript.parent.mkdir(parents=True)
        transcript.write_bytes(b"{not-json}\n")

        class ControlledStdout:
            def fileno(self):
                return 103

            def close(self):
                return None

        class ExitedProcess:
            stdout = ControlledStdout()

            def poll(self):
                return 0

        class ControlledSelector:
            def register(self, *args, **kwargs):
                return None

            def select(self, timeout=None):
                return [(object(), None)]

            def close(self):
                return None

        real_read = os.read

        def read_controlled_pipe(descriptor, size):
            if descriptor == 103:
                return b"{not-json}\n"
            return real_read(descriptor, size)

        with mock.patch.object(
            dsh_module.subprocess,
            "Popen",
            return_value=ExitedProcess(),
        ) as popen, mock.patch.object(
            dsh_module.selectors,
            "DefaultSelector",
            return_value=ControlledSelector(),
        ), mock.patch.object(
            dsh_module.os,
            "read",
            side_effect=read_controlled_pipe,
        ):
            first = self.discover()
            second = self.discover()

        self.assertEqual([], first)
        self.assertEqual([], second)
        self.assertEqual(1, popen.call_count)
        self.assertEqual(
            dsh_module._DecodeOutcome.DETERMINISTIC_INVALID,
            next(iter(self.adapter._durable_header_cache.values())).outcome,
        )

    def test_ctime_invalidates_same_size_rewrite_with_restored_mtime(self):
        transcript = _write_durable_log(self.dsh_home, "cache-ctime")

        with mock.patch.object(
            dsh_module,
            "_read_durable_header",
            wraps=dsh_module._read_durable_header,
        ) as decode:
            first = self.discover()
            before = transcript.stat()
            for offset in range(1, 21):
                _write_durable_log(
                    self.dsh_home,
                    "cache-ctime",
                    createdAt=1_787_429_064_000 + offset,
                )
                os.utime(
                    transcript,
                    ns=(before.st_atime_ns, before.st_mtime_ns),
                )
                after = transcript.stat()
                if after.st_ctime_ns != before.st_ctime_ns:
                    break
                time.sleep(0.001)
            changed = self.discover()

        self.assertEqual(before.st_size, after.st_size)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
        self.assertEqual(["cache-ctime"], [row.session_id for row in first])
        self.assertEqual(["cache-ctime"], [row.session_id for row in changed])
        self.assertEqual(2, decode.call_count)

    def test_discovery_deadline_caps_all_candidate_timeouts(self):
        for session_id in ("deadline-a", "deadline-b", "deadline-c"):
            _write_durable_log(self.dsh_home, session_id)
        timeouts = []

        def slow_decode(source_fd, executable, timeout):
            del source_fd, executable
            timeouts.append(timeout)
            time.sleep(max(0.0, timeout))
            return dsh_module._HeaderDecode(
                dsh_module._DecodeOutcome.TRANSIENT_FAILURE
            )

        started = time.monotonic()
        with mock.patch.object(dsh_module, "_DISCOVERY_TIMEOUT_S", 0.06), mock.patch.object(
            dsh_module,
            "_read_durable_header",
            side_effect=slow_decode,
        ):
            sessions = self.discover(candidate_timeout=0.05)
        elapsed = time.monotonic() - started

        self.assertEqual([], sessions)
        self.assertLess(elapsed, 0.2)
        self.assertLessEqual(sum(timeouts), 0.08)
        self.assertEqual(0, len(self.adapter._durable_header_cache))

    def test_stalling_candidates_are_transient_bounded_and_decoder_closes(self):
        for session_id in ("stall-a", "stall-b", "stall-c"):
            _write_durable_log(self.dsh_home, session_id)
        stalling = self.root / "stalling-discovery"
        _make_stalling_discovery_binary(stalling)
        adapter = DSHAdapter(discovery_zstd_binary=str(stalling))
        decoder = adapter._discovery_decoder
        self.assertIsNotNone(decoder)
        descriptor = decoder.descriptor
        launch_path = Path(decoder.launch_path)
        outcomes = []
        read_header = dsh_module._read_durable_header

        def record_outcome(source_fd, executable, timeout):
            decoded = read_header(source_fd, executable, timeout)
            outcomes.append(decoded.outcome)
            return decoded

        started = time.monotonic()
        try:
            with mock.patch.dict(
                os.environ,
                {"DSH_HOME": str(self.dsh_home)},
            ), mock.patch.object(
                dsh_module,
                "_DISCOVERY_TIMEOUT_S",
                0.18,
            ), mock.patch.object(
                dsh_module,
                "_DISCOVERY_CANDIDATE_TIMEOUT_S",
                0.12,
            ), mock.patch.object(
                dsh_module,
                "_read_durable_header",
                side_effect=record_outcome,
            ):
                sessions = list(adapter.discover(self.home))
        finally:
            adapter.close()
            adapter.close()
        elapsed = time.monotonic() - started

        self.assertEqual([], sessions)
        self.assertTrue(outcomes)
        self.assertTrue(
            all(
                outcome is dsh_module._DecodeOutcome.TRANSIENT_FAILURE
                for outcome in outcomes
            )
        )
        self.assertLess(elapsed, 0.75)
        self.assertEqual(0, len(adapter._durable_header_cache))
        self.assertTrue(decoder.closed)
        if descriptor >= 0:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        if dsh_module.sys.platform == "darwin":
            self.assertFalse(launch_path.exists())

    def test_zstd_path_is_resolved_once_and_not_replaced_by_path_change(self):
        _write_durable_log(self.dsh_home, "path-swap")
        original_bin = self.root / "original-bin"
        replacement_bin = self.root / "replacement-bin"
        original_bin.mkdir()
        replacement_bin.mkdir()
        _make_discovery_passthrough_binary(original_bin / "zstd")
        _make_discovery_passthrough_binary(replacement_bin / "zstd")
        with mock.patch.dict(os.environ, {"PATH": str(original_bin)}):
            adapter = DSHAdapter(discovery_zstd_binary="zstd")
        self.addCleanup(adapter.close)
        observed = []

        def record_decoder(source_fd, executable, timeout):
            del source_fd, timeout
            observed.append(executable.path)
            return dsh_module._HeaderDecode(
                dsh_module._DecodeOutcome.TRANSIENT_FAILURE
            )

        with mock.patch.dict(
            os.environ,
            {"DSH_HOME": str(self.dsh_home), "PATH": str(replacement_bin)},
        ), mock.patch.object(
            dsh_module,
            "_read_durable_header",
            side_effect=record_decoder,
        ):
            pinned = list(adapter.discover(self.home))

        self.assertEqual([], pinned)
        self.assertEqual(
            [(original_bin / "zstd").resolve()],
            observed,
        )

    def test_relative_symlink_and_unsafe_zstd_binaries_fail_closed(self):
        _write_durable_log(self.dsh_home, "unsafe-decoder")
        unsafe = self.root / "unsafe-zstd"
        _make_discovery_passthrough_binary(unsafe)
        os.chmod(unsafe, 0o777)
        linked = self.root / "linked-zstd"
        linked.symlink_to(self.passthrough)

        for binary in ("relative/zstd", str(linked), str(unsafe), str(self.root)):
            with self.subTest(binary=Path(binary).name):
                adapter = DSHAdapter(discovery_zstd_binary=binary)
                with mock.patch.dict(
                    os.environ,
                    {"DSH_HOME": str(self.dsh_home)},
                ):
                    self.assertEqual([], list(adapter.discover(self.home)))


class SharedAdapterHelperTests(unittest.TestCase):
    def test_mapping_and_epoch_normalization_reject_malformed_values(self):
        value = {"key": "value"}

        self.assertIs(value, as_mapping(value))
        self.assertEqual({}, as_mapping(["not", "a", "mapping"]))
        self.assertIsNone(epoch_seconds(True))
        self.assertIsNone(epoch_seconds("1787429064000"))
        self.assertEqual(1787429064.0, epoch_seconds(1787429064000))
        self.assertEqual(1787429064.0, epoch_seconds(1787429064))

    def test_bounded_json_object_rejects_malformed_nonobject_and_oversized_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "value.json"

            path.write_bytes(b'{"valid":true}')
            self.assertEqual({"valid": True}, read_json_object(path, path.stat().st_size))

            path.write_bytes(b'{"invalid"')
            self.assertEqual({}, read_json_object(path, 1024))

            path.write_bytes(b'["not","an","object"]')
            self.assertEqual({}, read_json_object(path, 1024))

            path.write_bytes(b'{"too":"large"}')
            self.assertEqual({}, read_json_object(path, path.stat().st_size - 1))
            self.assertEqual({}, read_json_object(root / "missing.json", 1024))

    def test_content_event_builder_ignores_malformed_blocks_and_copies_extra(self):
        session = Session(
            agent="dsh",
            session_id="shared-helper",
            project="/tmp/project",
            transcript="/tmp/session.jsonl.zstd",
            updated_at=1787429064.0,
        )
        timestamp = local_timestamp(session.updated_at)
        extra = {"native_type": "fixture"}

        def render(block, default_kind):
            if block.get("type") == "text":
                return default_kind, block.get("text"), 4
            return None

        self.assertEqual(
            [],
            content_block_events(
                {"type": "text", "text": "mapping is not list content"},
                session,
                timestamp,
                "assistant",
                extra,
                render,
            ),
        )
        events = content_block_events(
            [None, 1, {}, {"type": "text", "text": ""}, {"type": "text", "text": "abcde"}],
            session,
            timestamp,
            "assistant",
            extra,
            render,
        )
        extra["native_type"] = "changed"

        self.assertEqual(["assistant"], [event.kind for event in events])
        self.assertEqual(["abc…"], [event.text for event in events])
        self.assertEqual({"native_type": "fixture"}, events[0].extra)


class DSHNormalizeTests(unittest.TestCase):
    def setUp(self):
        self.adapter = DSHAdapter()
        self.session = Session(
            agent="dsh",
            session_id="dsh-session",
            project="/tmp/project",
            transcript="/tmp/session.jsonl.zstd",
            updated_at=1787429064.0,
        )

    def tearDown(self):
        self.adapter.close()

    def test_assistant_content_blocks_preserve_kinds_text_and_extra_fields(self):
        record = {
            "seq": 17,
            "type": "assistant/message",
            "time": 1787429064000,
            "data": {
                "message": {
                    "content": [
                        {"type": "text", "text": "final answer"},
                        {"type": "reasoning", "text": "careful thought"},
                        {
                            "type": "tool-call",
                            "name": "Read",
                            "arguments": {"path": "fixture.txt"},
                        },
                        {
                            "type": "tool-result",
                            "content": {"output": "fixture\ncontents"},
                        },
                        {"type": "unknown", "text": "ignored"},
                    ]
                }
            },
        }

        events = list(self.adapter.normalize(record, self.session))

        self.assertEqual(
            ["assistant", "thinking", "tool_call", "tool_result"],
            [event.kind for event in events],
        )
        self.assertEqual(
            [
                "final answer",
                "careful thought",
                "Read {'path': 'fixture.txt'}",
                "fixture contents",
            ],
            [event.text for event in events],
        )
        self.assertEqual(
            [local_timestamp(record["time"])] * 4,
            [event.ts for event in events],
        )
        self.assertEqual(
            [{"native_type": "assistant/message", "seq": 17}] * 4,
            [event.extra for event in events],
        )

    def test_content_block_truncation_limits_are_preserved(self):
        record = {
            "type": "assistant/message",
            "data": {
                "message": {
                    "content": [
                        {"type": "text", "text": "a" * 121},
                        {"type": "reasoning", "text": "b" * 81},
                        {
                            "type": "tool-call",
                            "name": "Read",
                            "arguments": "c" * 121,
                        },
                        {"type": "tool-result", "content": "d" * 81},
                    ]
                }
            },
        }

        events = list(self.adapter.normalize(record, self.session))

        self.assertEqual([120, 80, 120, 80], [len(event.text) for event in events])
        self.assertTrue(all(event.text.endswith("…") for event in events))


class DSHReplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.passthrough = self.root / "passthrough.py"
        _make_passthrough_binary(self.passthrough)

    def tearDown(self):
        self.temporary.cleanup()

    def test_watcher_advances_after_seq_beyond_old_eight_mib_boundary(self):
        transcript = self.root / "session.jsonl.zstd"
        boundary_seq, tail_seqs = _write_large_transcript(transcript)
        adapter = _PassthroughDSHAdapter(str(self.passthrough))
        session = Session(
            agent="dsh",
            session_id="long-session",
            project="/tmp/project",
            transcript=str(transcript),
            updated_at=1_787_429_064.0,
            extra={"seq": boundary_seq},
        )
        tailer = SessionTailer(session, adapter=adapter, max_records=1)

        first = tailer.poll()
        second = tailer.poll()

        self.assertEqual(["tail one"], [event.text for event in first])
        self.assertEqual(["tail two"], [event.text for event in second])
        self.assertEqual([boundary_seq, tail_seqs[0]], adapter.after_seqs)
        self.assertEqual(tail_seqs, (first[0].extra["seq"], second[0].extra["seq"]))

    def test_scan_ceiling_remains_configurable_and_bounded(self):
        transcript = self.root / "bounded.jsonl.zstd"
        boundary_seq, _ = _write_large_transcript(transcript)

        records = replay_dsh_events(
            transcript,
            after_seq=boundary_seq,
            max_output_bytes=OLD_REPLAY_BOUNDARY,
            zstd_binary=str(self.passthrough),
        )

        self.assertEqual([], records)
        # The scan-byte ceiling stopped before the end of the stream.
        self.assertFalse(records.exhausted)

    def test_retained_bytes_bound_returns_progressive_pages(self):
        transcript = self.root / "retained.jsonl.zstd"
        with transcript.open("wb") as stream:
            first_size = _write_record(stream, {"seq": 1, "value": "a" * 128})
            _write_record(stream, {"seq": 2, "value": "b" * 128})

        first = replay_dsh_events(
            transcript,
            max_retained_bytes=first_size,
            zstd_binary=str(self.passthrough),
        )
        second = replay_dsh_events(
            transcript,
            after_seq=1,
            max_retained_bytes=first_size,
            zstd_binary=str(self.passthrough),
        )

        self.assertEqual([1], [record["seq"] for record in first])
        self.assertEqual([2], [record["seq"] for record in second])
        # The first page stopped early because the second record exceeded its
        # remaining byte budget; the second page then read to end-of-stream.
        self.assertFalse(first.exhausted)
        self.assertTrue(second.exhausted)

    def test_replay_page_reports_exhausted_only_at_true_end(self):
        transcript = self.root / "exhausted.jsonl.zstd"
        with transcript.open("wb") as stream:
            first_size = _write_record(stream, {"seq": 1, "value": "a" * 128})
            _write_record(stream, {"seq": 2, "value": "b" * 128})

        stopped = replay_dsh_events(
            transcript,
            max_retained_bytes=first_size,
            zstd_binary=str(self.passthrough),
        )
        final = replay_dsh_events(
            transcript,
            after_seq=1,
            zstd_binary=str(self.passthrough),
        )
        empty = replay_dsh_events(
            transcript,
            after_seq=2,
            zstd_binary=str(self.passthrough),
        )

        self.assertIsInstance(stopped, ReplayPage)
        self.assertEqual([1], [record["seq"] for record in stopped])
        self.assertFalse(stopped.exhausted)
        # Paging on from the byte-budget stop retrieves the remaining record
        # and only the page that read to end-of-stream reports exhaustion.
        self.assertEqual([2], [record["seq"] for record in final])
        self.assertTrue(final.exhausted)
        self.assertEqual([], list(empty))
        self.assertTrue(empty.exhausted)

    def test_timeout_early_stop_reports_not_exhausted(self):
        transcript = self.root / "slow.jsonl.zstd"
        transcript.write_bytes(b"placeholder\n")
        slow = self.root / "slow.py"
        ready = self.root / "slow-ready"
        _make_slow_binary(slow)
        real_popen = subprocess.Popen
        spawned = []
        handshake_times = []

        def spawn_after_first_record(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            startup_deadline = time.monotonic() + 10.0
            while not ready.exists() and process.poll() is None:
                if time.monotonic() >= startup_deadline:
                    dsh_module._stop_process(process)
                    self.fail("slow replay helper missed its readiness handshake")
                time.sleep(0.01)
            if not ready.exists():
                dsh_module._stop_process(process)
                self.fail(
                    "slow replay helper exited before its readiness handshake"
                )
            handshake_times.append(time.monotonic())
            return process

        replay_timeout = 2.0
        with mock.patch.dict(
            os.environ,
            {"AGENT_SIDECAR_TEST_REPLAY_READY": str(ready)},
        ), mock.patch.object(
            dsh_module.subprocess,
            "Popen",
            side_effect=spawn_after_first_record,
        ):
            records = replay_dsh_events(
                transcript,
                timeout=replay_timeout,
                zstd_binary=str(slow),
            )
        elapsed_after_handshake = time.monotonic() - handshake_times[0]

        self.assertEqual([1], [record["seq"] for record in records])
        self.assertFalse(records.exhausted)
        self.assertGreaterEqual(elapsed_after_handshake, replay_timeout * 0.8)
        self.assertLess(elapsed_after_handshake, 5.0)
        self.assertEqual(1, len(spawned))
        self.assertIsNotNone(spawned[0].poll())

    def test_oversized_line_is_dropped_without_blocking_later_records(self):
        transcript = self.root / "oversized.jsonl.zstd"
        with transcript.open("wb") as stream:
            stream.write(b"x" * (2 * 1024 * 1024) + b"\n")
            _write_record(stream, {"seq": 2, "type": "turn/end"})

        records = replay_dsh_events(
            transcript,
            zstd_binary=str(self.passthrough),
        )

        self.assertEqual([2], [record["seq"] for record in records])
        # Dropping an oversized line is not an early budget stop.
        self.assertTrue(records.exhausted)

    def test_incomplete_tail_degrades_to_complete_records(self):
        transcript = self.root / "incomplete.jsonl.zstd"
        with transcript.open("wb") as stream:
            _write_record(stream, {"seq": 1, "type": "turn/start"})
            stream.write(b'{"seq":2,"type":"turn/end"')

        records = replay_dsh_events(
            transcript,
            zstd_binary=str(self.passthrough),
        )

        self.assertEqual([1], [record["seq"] for record in records])
        # The stream itself was fully read; the dangling partial line is not
        # retrievable by another page, so the page counts as exhausted.
        self.assertTrue(records.exhausted)

    def test_missing_zstd_degrades_to_no_records(self):
        transcript = self.root / "missing.jsonl.zstd"
        transcript.write_bytes(b'{"seq":1}\n')

        records = replay_dsh_events(
            transcript,
            zstd_binary="agent-sidecar-definitely-missing-zstd",
        )

        self.assertEqual([], records)
        # Degraded sources have nothing retrievable, so paging on is futile.
        self.assertTrue(records.exhausted)

    @unittest.skipUnless(ZSTD, "zstd binary is unavailable")
    def test_real_zstd_fixture_reaches_records_after_eight_mib(self):
        source = self.root / "large.jsonl"
        compressed = self.root / "large.jsonl.zstd"
        boundary_seq, tail_seqs = _write_large_transcript(source)
        subprocess.run(
            [ZSTD, "-q", "-f", str(source), "-o", str(compressed)],
            check=True,
            timeout=10,
        )

        records = replay_dsh_events(
            compressed,
            after_seq=boundary_seq,
            max_records=2,
        )

        self.assertEqual(list(tail_seqs), [record["seq"] for record in records])
        # A record-budget stop cannot prove the transcript ended with it.
        self.assertFalse(records.exhausted)


if __name__ == "__main__":
    unittest.main()
