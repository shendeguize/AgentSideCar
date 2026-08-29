import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sidecar import process
from sidecar import remote
from sidecar import kimi_identity
from sidecar import state
from sidecar import tail
from sidecar.remote import aggregate_remote
from sidecar.remote_types import (
    ProbeResult,
    RemoteHost,
    validate_remote_python_executable,
)
from sidecar.model import Status
from sidecar.tailer_pool import TailerPool


class FunctionalRegressionTests(unittest.TestCase):
    def test_process_inventory_ignores_malformed_rows_and_bounds_cwd_errors(self):
        rows = process.parse_ps_output(
            "malformed\n"
            "not-a-pid 00:01 /usr/bin/claude --help\n"
            "123 00:02 /usr/bin/claude --help\n",
            cwd_lookup=mock.Mock(side_effect=OSError("gone")),
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(123, rows[0]["pid"])
        self.assertEqual("", rows[0]["cwd"])
        self.assertEqual(
            "",
            process.parse_ps_output("123 00:02 /usr/bin/claude --help")[0]["cwd"],
        )

    def test_strict_process_inspection_fails_closed_for_kimi_like_malformed_rows(self):
        for payload in (
            b"kimi\n",
            b"not-a-pid 456 /usr/bin/kimi --help\n",
            b"0 0 /usr/bin/kimi --help\n",
        ):
            result = mock.Mock(
                returncode=0,
                overflow=None,
                cleanup_incomplete=False,
                stdout=payload,
            )
            with self.subTest(payload=payload), mock.patch.object(
                process, "_strict_run", return_value=result
            ):
                with self.assertRaises(Exception):
                    process._strict_process_rows(
                        deadline=process.time.monotonic() + 1
                    )
        for payload in (b"ordinary\n", b"not-a-pid 456 /usr/bin/python --help\n"):
            result = mock.Mock(
                returncode=0,
                overflow=None,
                cleanup_incomplete=False,
                stdout=payload,
            )
            with mock.patch.object(process, "_strict_run", return_value=result):
                self.assertEqual(
                    [],
                    process._strict_process_rows(
                        deadline=process.time.monotonic() + 1
                    ),
                )
        with mock.patch.object(
            process,
            "_strict_run",
            return_value=mock.Mock(
                returncode=0,
                overflow=None,
                cleanup_incomplete=False,
                stdout=b"ordinary\n",
            ),
        ), mock.patch.object(process, "_lexical_command_probes", return_value=()):
            with mock.patch.object(process, "_has_kimi_indicator", return_value=False):
                self.assertEqual(
                    [],
                    process._strict_process_rows(
                        deadline=process.time.monotonic() + 1
                    ),
                )
        ordinary_result = mock.Mock(
            returncode=0,
            overflow=None,
            cleanup_incomplete=False,
            stdout=(
                b"ordinary\n"
                b"1 1 /usr/bin/python --help\n"
                b"not-a-pid 2 /usr/bin/python --help\n"
                b"0 0 /usr/bin/python --help\n"
            ),
        )
        with mock.patch.object(process, "_strict_run", return_value=ordinary_result):
            with mock.patch.object(process, "_has_kimi_indicator", return_value=False):
                self.assertEqual(
                    [(1, 1, "/usr/bin/python", "--help")],
                    process._strict_process_rows(
                        deadline=process.time.monotonic() + 1
                    ),
                )

    def test_strict_macos_cwd_identity_requires_complete_verified_fields(self):
        result = mock.Mock(
            returncode=0,
            overflow=None,
            cleanup_incomplete=False,
            stdout=b"p1\0fcwd\0",
        )
        with mock.patch.object(process, "_strict_run", return_value=result):
            with self.assertRaises(Exception):
                process._strict_macos_cwd_identity(
                    1,
                    process.time.monotonic() + 1,
                )

        result = mock.Mock(
            returncode=0,
            overflow=None,
            cleanup_incomplete=False,
            stdout=b"p1\0D1\0i1\0n/tmp\0",
        )
        with mock.patch.object(process, "_strict_run", return_value=result):
            with self.assertRaises(Exception):
                process._strict_macos_cwd_identity(
                    1,
                    process.time.monotonic() + 1,
                )

        result = mock.Mock(
            returncode=0,
            overflow=None,
            cleanup_incomplete=False,
            stdout=b"p1\0fcwd\0D1\0i1\0n/tmp\n\0",
        )
        with mock.patch.object(process, "_strict_run", return_value=result):
            with self.assertRaises(Exception):
                process._strict_macos_cwd_identity(
                    1,
                    process.time.monotonic() + 1,
                )

    def test_process_candidate_parsing_has_bounded_and_attached_forms(self):
        with self.assertRaises(Exception):
            process._lexical_command_probes(" ".join(["kimi"] * 4098))
        self.assertEqual(
            ("/tmp/loader.mjs", "/tmp/entry.mjs"),
            process._node_candidate_tokens(
                ("node", "--loader=/tmp/loader.mjs", "--require", "/tmp/entry.mjs")
            ),
        )
        self.assertEqual(
            (),
            process._node_candidate_tokens(("node", "--unknown=/tmp/ignored")),
        )
        self.assertEqual(
            (),
            process._node_candidate_tokens(("node", "--loader=")),
        )

    def test_relative_candidate_identity_ignores_directories(self):
        metadata = mock.Mock(st_mode=process.stat.S_IFDIR)
        with mock.patch.object(process.os, "stat", return_value=metadata):
            self.assertIsNone(
                process._relative_candidate_token_identity("directory", 4)
            )

    def test_tail_state_ignores_blank_text_items(self):
        result = state._tail_state(
            [{"role": "assistant", "content": [{"type": "text", "text": " "}]}]
        )
        self.assertIsNone(result.activity_at)

    def test_unknown_platform_strict_cwd_fails_closed(self):
        with mock.patch.object(process.sys, "platform", "freebsd"):
            with self.assertRaises(Exception):
                process._strict_pid_cwd(1)

    def test_platform_dispatch_and_relative_candidate_failures_are_bounded(self):
        with mock.patch.object(
            process,
            "_strict_macos_pid_cwd",
            return_value="/tmp/functional-cwd",
        ) as macos_cwd:
            with mock.patch.object(process.sys, "platform", "darwin"):
                self.assertEqual("/tmp/functional-cwd", process._strict_pid_cwd(7))
            macos_cwd.assert_called_once_with(7)
        with mock.patch.object(
            process,
            "_strict_linux_pid_cwd",
            return_value="/tmp/linux-cwd",
        ) as linux_cwd, mock.patch.object(process.sys, "platform", "linux"):
            self.assertEqual("/tmp/linux-cwd", process._strict_pid_cwd(8))
            linux_cwd.assert_called_once_with(8)

        with mock.patch.object(process.os, "stat", side_effect=OSError("denied")):
            with self.assertRaises(Exception):
                process._relative_candidate_token_identity("entry.js", 3)

        with mock.patch.object(
            process.os,
            "stat",
            return_value=mock.Mock(st_mode=process.stat.S_IFREG),
        ), mock.patch.object(process.os, "open", side_effect=OSError("denied")):
            with self.assertRaises(Exception):
                process._relative_candidate_token_identity("entry.js", 3)

        result = mock.Mock(
            returncode=0,
            overflow=None,
            cleanup_incomplete=False,
            stdout=b"p1\0fcwd\0D1\0i1\0nbad\0",
        )
        with mock.patch.object(process, "_strict_run", return_value=result):
            with self.assertRaises(Exception):
                process._strict_macos_cwd_identity(
                    1,
                    process.time.monotonic() + 1,
                )

        result = mock.Mock(
            returncode=0,
            overflow=None,
            cleanup_incomplete=False,
            stdout=b"p1\0fcwd\0D1\0i1\0n/tmp\0",
        )
        with mock.patch.object(
            process,
            "_strict_run",
            return_value=result,
        ), mock.patch.object(process.os, "open", return_value=3), mock.patch.object(
            process.os,
            "fstat",
            side_effect=OSError("gone"),
        ), mock.patch.object(process.os, "close") as close:
            with self.assertRaises(Exception):
                process._strict_macos_cwd_identity(
                    1,
                    process.time.monotonic() + 1,
                )
            close.assert_called_once_with(3)

        with mock.patch.object(process.os, "open", return_value=3), mock.patch.object(
            process.os,
            "fstat",
            side_effect=OSError("gone"),
        ), mock.patch.object(process.os, "close") as close:
            with self.assertRaises(Exception):
                process._strict_linux_cwd_identity(1)
            close.assert_called_once_with(3)

        with mock.patch.object(
            process,
            "_strict_linux_cwd_identity",
            return_value=(1, 2, 3),
        ), mock.patch.object(
            process,
            "_inspection_remaining",
            side_effect=[None, Exception("deadline")],
        ), mock.patch.object(process.os, "close") as close, mock.patch.object(
            process.sys,
            "platform",
            "linux",
        ):
            with self.assertRaises(Exception):
                process._strict_candidate_cwd(1, 10.0)
            close.assert_called_once_with(3)

    def test_malformed_absolute_node_candidate_can_match_executable_identity(self):
        identity = (12, 34)
        with mock.patch.object(
            process,
            "_candidate_token_identity",
            return_value=identity,
        ):
            classification, relative = process._classify_node_kimi_candidate(
                "node /tmp/entry.js '",
                identity,
                process.time.monotonic() + 1,
            )
        self.assertEqual(process._NodeKimiClassification.DEFINITE, classification)
        self.assertIsNone(relative)

    def test_tailer_pool_bounds_repeated_emission_and_closes_uninspectable_followers(self):
        emitted = []
        pool = TailerPool(emitted.append, max_event_polls=2)
        tailer = mock.Mock(
            single_poll_per_refresh=False,
            poll=mock.Mock(return_value=(object(), {"type": "synthetic"})),
            has_pending_records=False,
        )
        self.assertTrue(pool._poll(("claude", "synthetic"), tailer))
        self.assertEqual(2, tailer.poll.call_count)
        self.assertEqual([{"type": "synthetic"}, {"type": "synthetic"}], emitted)

        class UninspectableFollower:
            @property
            def follower(self):
                raise RuntimeError("closed")

            def close(self):
                return None

        pool._close_tailer(("claude", "synthetic"), UninspectableFollower())

        class BareTailer:
            def poll(self):
                return []

        session = mock.Mock(
            transcript="/tmp/functional.jsonl",
            extra={},
            agent="claude",
            status=Status.WORKING,
        )
        resumed = TailerPool(
            lambda _event: None,
            tailer_factory=lambda _session, **_kwargs: BareTailer(),
        )
        key = ("claude", "checkpointed")
        resumed._checkpoints[key] = (
            "/tmp/functional.jsonl",
            {"offset": 1},
        )
        resumed._update(
            key,
            session,
            initial=False,
            new_session=False,
            now=0.0,
        )
        resumed._pending.add(("missing", "pending"))
        resumed.refresh([], changed_keys=(), initial=False, now=0.0)
        self.assertNotIn(("missing", "pending"), resumed._pending)
        existing = BareTailer()
        existing.session = None
        resumed._tailers[key] = existing
        resumed._paths[key] = session.transcript
        resumed._update(
            key,
            session,
            initial=False,
            new_session=False,
            now=0.0,
        )

    def test_session_transcript_binding_rejects_empty_and_unsafe_platform_paths(self):
        capture = kimi_identity._Capture()
        wire = kimi_identity.FileIdentity("/tmp/functional", 1, 1, 0, 0)
        with self.assertRaises(kimi_identity.KimiIdentityError):
            kimi_identity._bind_session_transcript(capture, "", wire)
        capture.close()

        with tempfile.NamedTemporaryFile() as temporary:
            path = temporary.name
            details = process.os.stat(path)
            file_identity = kimi_identity.FileIdentity(
                path,
                details.st_dev,
                details.st_ino,
                details.st_mode,
                details.st_uid,
            )
            capture = kimi_identity._Capture()
            with mock.patch.object(kimi_identity.os, "O_NOFOLLOW", 0):
                with self.assertRaises(kimi_identity.KimiIdentityError):
                    kimi_identity._bind_session_transcript(
                        capture,
                        path,
                        file_identity,
                    )
            capture.close()

            capture = kimi_identity._Capture()
            with mock.patch.object(
                kimi_identity,
                "_owner_safe",
                return_value=False,
            ):
                with self.assertRaises(kimi_identity.KimiIdentityError):
                    kimi_identity._bind_session_transcript(
                        capture,
                        path,
                        file_identity,
                    )
            capture.close()

            capture = kimi_identity._Capture()
            changed_identity = kimi_identity.FileIdentity(
                path,
                details.st_dev + 1,
                details.st_ino,
                details.st_mode,
                details.st_uid,
            )
            with mock.patch.object(kimi_identity, "_owner_safe", return_value=True):
                with self.assertRaises(kimi_identity.KimiIdentityError):
                    kimi_identity._bind_session_transcript(
                        capture,
                        path,
                        changed_identity,
                    )
            capture.close()

            capture = kimi_identity._Capture()
            with mock.patch.object(
                kimi_identity,
                "_owner_safe",
                return_value=True,
            ), mock.patch.object(
                kimi_identity,
                "_file_identity",
                return_value=kimi_identity.FileIdentity(
                    path,
                    details.st_dev + 1,
                    details.st_ino,
                    details.st_mode,
                    details.st_uid,
                ),
            ):
                with self.assertRaises(kimi_identity.KimiIdentityError):
                    kimi_identity._bind_session_transcript(
                        capture,
                        path,
                        file_identity,
                    )
            capture.close()

    def test_kimi_regular_file_reader_rejects_racing_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_bytes(b"x")
            parent_fd = process.os.open(temporary, process.os.O_RDONLY)
            capture = kimi_identity._Capture()
            parent = kimi_identity.DirectoryIdentity(temporary, 1, 1, 0, 0)
            details = mock.Mock(
                st_dev=1,
                st_ino=2,
                st_mode=process.stat.S_IFREG | 0o600,
                st_uid=process.os.geteuid(),
                st_size=1,
            )
            after = mock.Mock(**details.__dict__)
            after.st_size = 2
            try:
                with mock.patch.object(
                    kimi_identity.os,
                    "fstat",
                    side_effect=[details, after],
                ), mock.patch.object(
                    kimi_identity.os,
                    "stat",
                    return_value=details,
                ), mock.patch.object(
                    kimi_identity,
                    "_same_object",
                    return_value=True,
                ), mock.patch.object(
                    kimi_identity,
                    "_owner_safe",
                    return_value=True,
                ), mock.patch.object(
                    kimi_identity,
                    "_generation",
                    return_value=(1, 1, 1),
                ), mock.patch.object(
                    kimi_identity.os,
                    "read",
                    side_effect=[b"x", b""],
                ):
                    with self.assertRaises(kimi_identity.KimiIdentityError):
                        kimi_identity._open_regular_at(
                            capture,
                            parent_fd,
                            parent,
                            "state.json",
                            10,
                        )
            finally:
                capture.close()
                process.os.close(parent_fd)

    def test_kimi_directory_binding_rejects_owner_and_identity_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            capture = kimi_identity._Capture()
            with mock.patch.object(kimi_identity, "_owner_safe", return_value=False):
                with self.assertRaises(kimi_identity.KimiIdentityError):
                    kimi_identity._open_absolute_directory(
                        capture,
                        temporary,
                        reject_symlink_leaf=False,
                    )
            capture.close()

            capture = kimi_identity._Capture()
            with mock.patch.object(
                kimi_identity,
                "_same_object",
                side_effect=[True, False],
            ), mock.patch.object(kimi_identity, "_owner_safe", return_value=True):
                with self.assertRaises(kimi_identity.KimiIdentityError):
                    kimi_identity._open_absolute_directory(
                        capture,
                        temporary,
                        reject_symlink_leaf=True,
                    )
            capture.close()

            capture = kimi_identity._Capture()
            with mock.patch.object(
                kimi_identity.os.path,
                "realpath",
                side_effect=[temporary, "/different"],
            ), mock.patch.object(
                kimi_identity,
                "_same_object",
                return_value=True,
            ), mock.patch.object(kimi_identity, "_owner_safe", return_value=True):
                with self.assertRaises(kimi_identity.KimiIdentityError):
                    kimi_identity._open_absolute_directory(
                        capture,
                        temporary,
                        reject_symlink_leaf=False,
                    )
            capture.close()

    def test_watch_sessions_honors_cancel_during_bounded_wait(self):
        fake_tailer = mock.Mock(poll=mock.Mock(return_value=[]))
        with mock.patch.object(tail, "SessionTailer", return_value=fake_tailer):
            cancel = mock.Mock()
            cancel.is_set.side_effect = [False, False, False, False, True]
            cancel.wait.return_value = False
            list(
                tail.watch_sessions(
                    [object()],
                    poll_interval=0.01,
                    cancel_event=cancel,
                )
            )
            cancel.wait.assert_called_once()

        with mock.patch.object(tail, "SessionTailer", return_value=fake_tailer):
            cancel = mock.Mock()
            cancel.is_set.side_effect = [False, False, False, False]
            cancel.wait.return_value = True
            list(
                tail.watch_sessions(
                    [object()],
                    poll_interval=0.01,
                    cancel_event=cancel,
                )
            )
            cancel.wait.assert_called_once()

    def test_running_and_degraded_hosts_are_valid_observation_targets(self):
        self.assertEqual(
            ("running", "degraded"),
            tuple(
                RemoteHost(alias, phase).phase
                for alias, phase in (
                    ("running-host", "running"),
                    ("degraded-host", "degraded"),
                )
            ),
        )

    def test_remote_python_contract_accepts_bounded_absolute_executable(self):
        executable = validate_remote_python_executable("/usr/bin/python3.8")
        self.assertEqual("/usr/bin/python3.8", executable)
        probe = ProbeResult((3, 8, 19), executable)
        self.assertEqual((3, 8, 19), probe.version)
        with self.assertRaises(ValueError):
            validate_remote_python_executable("../python")

    def test_empty_remote_fleet_is_a_successful_read_only_result(self):
        result = aggregate_remote("status", hosts=(), artifact=b"")
        self.assertEqual(0, result.exit_code)
        self.assertFalse(result.partial)
        self.assertEqual([], result.to_dict()["failures"])

    def test_remote_host_failure_is_isolated_from_other_hosts(self):
        hosts = (RemoteHost("failing-host", "ready"),)
        with mock.patch.object(
            remote,
            "execute_remote_host",
            side_effect=RuntimeError("synthetic failure"),
        ):
            result = aggregate_remote("status", hosts=hosts, artifact=b"artifact")
        self.assertEqual(["failing-host"], [failure.host for failure in result.failures])
        self.assertEqual("remote", result.failures[0].code)
        self.assertEqual(3, result.exit_code)

    def test_remote_aggregation_discards_rows_after_resource_budget_overflow(self):
        hosts = (
            RemoteHost("overflow-host", "ready"),
            RemoteHost("overflow-host-2", "ready"),
            RemoteHost("overflow-host-3", "ready"),
        )
        with mock.patch.object(
            remote,
            "execute_remote_host",
            side_effect=[
                ([{"row": "synthetic"}], None),
                ([], None),
            ],
        ), mock.patch.object(remote, "MAX_ROWS", 1), mock.patch.object(
            remote,
            "MAX_AGGREGATE_BYTES",
            2,
        ):
            result = aggregate_remote(
                "status",
                hosts=hosts,
                artifact=b"artifact",
                max_workers=1,
            )
        self.assertEqual((), result.rows)

    def test_remote_deadline_cleanup_consumes_already_finished_future(self):
        clock_values = iter((0.0, 0.0, 999.0))

        def clock():
            return next(clock_values, 999.0)

        host = RemoteHost("finished-host", "ready")
        with mock.patch.object(
            remote,
            "execute_remote_host",
            return_value=([], None),
        ):
            result = aggregate_remote(
                "status",
                hosts=(host,),
                artifact=b"artifact",
                fleet_timeout=1.0,
                monotonic=clock,
            )
        self.assertEqual(("finished-host",), result.succeeded)


if __name__ == "__main__":
    unittest.main()
