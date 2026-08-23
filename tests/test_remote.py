import dataclasses
import io
import json
import os
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

from sidecar import remote
from sidecar.process_runner import BoundedProcessResult


REPO_ROOT = Path(__file__).resolve().parents[1]


def completed(argv, returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(
        argv,
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def inventory_row(
    name,
    *,
    phase="ready",
    enabled=True,
    local=False,
    orphaned=False,
):
    return {
        "name": name,
        "phase": phase,
        "local": local,
        "orphaned": orphaned,
        "config": {
            "enabled": enabled,
            "local": local,
            "workdir": "/sensitive/path",
            "inject": {"env": {"TOKEN": "secret"}, "extraArgs": ["secret"]},
        },
        "sshInfo": {"hostName": "secret.example", "user": "secret"},
        "probe": {"errorSummary": "secret failure", "dshHome": "/secret"},
    }


def json_completed(argv, value):
    return completed(
        argv,
        stdout=json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n",
    )


def probe_completed(argv, version=(3, 11, 0)):
    return json_completed(argv, {"python": list(version)})


def execution_completed(argv, rows):
    return json_completed(argv, {"ok": True, "rows": rows})


def session_row(session_id="session", *, status="waiting", agent="claude"):
    return {
        "agent": agent,
        "session_id": session_id,
        "project": "/remote/project",
        "transcript": "/remote/session.jsonl",
        "updated_at": 100.0,
        "title": "remote title",
        "status": status,
        "extra": {},
        "parent_id": None,
    }


def process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RemoteDataModelTests(unittest.TestCase):
    def test_models_are_frozen_and_failure_contains_only_code(self):
        host = remote.RemoteHost("edge-1", "ready")
        failure = remote.RemoteFailure("edge-1", "auth")
        aggregate = remote.RemoteAggregate(
            "status",
            failures=(failure,),
            hosts=("edge-1",),
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            host.alias = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            failure.code = "remote"
        self.assertFalse(hasattr(host, "host"))
        self.assertFalse(hasattr(host, "label"))
        self.assertEqual({"host": "edge-1", "code": "auth"}, failure.to_dict())
        self.assertNotIn("stderr", aggregate.to_dict()["failures"][0])
        self.assertEqual(remote.EXIT_NO_SUCCESS, aggregate.exit_code)
        self.assertNotIn("build_zipapp", remote.__all__)

    def test_partial_and_exit_policy_distinguish_outcomes(self):
        row = session_row()
        row["host"] = "good"
        partial = remote.RemoteAggregate(
            "status",
            rows=(row,),
            failures=(remote.RemoteFailure("bad", "timeout"),),
            hosts=("bad", "good"),
            succeeded=("good",),
        )
        complete = remote.RemoteAggregate(
            "status",
            rows=(row,),
            hosts=("good",),
            succeeded=("good",),
        )

        self.assertTrue(partial.partial)
        self.assertEqual(remote.EXIT_OK, partial.exit_code)
        self.assertFalse(complete.partial)
        self.assertEqual(remote.EXIT_OK, complete.exit_code)
        self.assertEqual(
            remote.EXIT_INVALID_INVENTORY,
            remote.RemoteInventoryError.exit_code,
        )

    def test_alias_and_failure_codes_are_allowlisted(self):
        for alias in ("-option", "host name", "host;touch", "", "é"):
            with self.subTest(alias=alias):
                with self.assertRaises(ValueError):
                    remote.RemoteHost(alias, "ready")
        with self.assertRaises(ValueError):
            remote.RemoteFailure("edge", "permission denied: secret")
        with self.assertRaises(ValueError):
            remote.RemoteHost("edge", "probing")
        for alias in ("local", "LOCAL", "Local"):
            with self.subTest(alias=alias):
                with self.assertRaises(ValueError):
                    remote.RemoteHost(alias, "ready")


class BoundedJSONTests(unittest.TestCase):
    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        with self.assertRaises(ValueError):
            remote.parse_bounded_json(b'{"host":"a","host":"b"}', max_bytes=100)
        with self.assertRaises(ValueError):
            remote.parse_bounded_json(b'{"value":NaN}', max_bytes=100)

    def test_payload_size_depth_and_string_limits_are_bounded(self):
        with self.assertRaises(ValueError):
            remote.parse_bounded_json(b"[]" * 10, max_bytes=5)
        nested = "[" * (remote.MAX_JSON_DEPTH + 2) + "0" + "]" * (
            remote.MAX_JSON_DEPTH + 2
        )
        with self.assertRaises(ValueError):
            remote.parse_bounded_json(nested, max_bytes=len(nested.encode("utf-8")))
        oversized_string = json.dumps("x" * (remote.MAX_JSON_STRING_BYTES + 1))
        with self.assertRaises(ValueError):
            remote.parse_bounded_json(
                oversized_string,
                max_bytes=len(oversized_string.encode("utf-8")),
            )

    def test_parser_converts_recursion_and_invalid_unicode_to_value_error(self):
        deeply_nested = "[" * 5000 + "0" + "]" * 5000
        invalid_values = (
            deeply_nested,
            '"\ud800"',
            b'"\\ud800"',
            b'"\xff"',
        )

        for payload in invalid_values:
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(ValueError):
                    remote.parse_bounded_json(
                        payload,
                        max_bytes=max(
                            10001,
                            len(payload) if isinstance(payload, bytes) else len(payload),
                        ),
                    )


class InventoryTests(unittest.TestCase):
    @mock.patch.object(remote, "_run_bounded")
    def test_default_inventory_probe_uses_canonical_bounded_runner(self, run_bounded):
        argv = ("dshc", "ls", "--json")
        payload = json.dumps([inventory_row("edge")]).encode("utf-8")
        run_bounded.return_value = BoundedProcessResult(
            args=argv,
            returncode=0,
            stdout=payload,
            stderr=b"",
        )
        environment = {"HOME": "/unused", "PATH": "/bin"}

        hosts = remote.load_remote_hosts(env=environment, timeout=3)

        self.assertEqual(["edge"], [host.alias for host in hosts])
        run_bounded.assert_called_once_with(
            argv,
            input_limit=1,
            stdout_limit=remote.MAX_INVENTORY_BYTES,
            stderr_limit=remote.MAX_STDERR_BYTES,
            timeout=3.0,
            env=environment,
        )

    @unittest.skipUnless(os.name == "posix", "executable fixture requires POSIX")
    def test_default_inventory_probe_caps_flood_and_uses_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inventory_root = root / "inventory"
            inventory_root.mkdir()
            config = {"hosts": {"fallback": {"enabled": True, "local": False}}}
            state = {
                "hosts": {
                    "fallback": {"phase": "ready", "orphaned": False}
                }
            }
            (inventory_root / "config.json").write_text(
                json.dumps(config),
                encoding="utf-8",
            )
            (inventory_root / "state.json").write_text(
                json.dumps(state),
                encoding="utf-8",
            )
            pid_path = root / "dshc.pid"
            executable = root / "dshc"
            executable.write_text(
                "#!{}\n"
                "import os\n"
                "open(os.environ['DSHC_TEST_PID'],'w').write(str(os.getpid()))\n"
                "chunk=b'x'*65536\n"
                "while True: os.write(1,chunk)\n".format(sys.executable),
                encoding="utf-8",
            )
            executable.chmod(0o755)
            environment = {
                "DSHC_HOME": str(inventory_root),
                "DSHC_TEST_PID": str(pid_path),
                "PATH": str(root),
            }
            started = time.monotonic()

            with mock.patch.object(remote, "MAX_INVENTORY_BYTES", 65536):
                hosts = remote.load_remote_hosts(env=environment, timeout=3)

            self.assertLess(time.monotonic() - started, 2.8)
            self.assertEqual(["fallback"], [host.alias for host in hosts])
            pid = int(pid_path.read_text(encoding="ascii"))
            self.assertFalse(process_exists(pid))

    def test_default_inventory_reader_requests_only_limit_plus_one(self):
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.read.return_value = b"{}"
        path = Path("/inventory/config.json")

        with mock.patch.object(Path, "open", return_value=stream) as path_open:
            payload = remote._read_bounded_inventory_file(path)

        self.assertEqual(b"{}", payload)
        path_open.assert_called_once_with("rb")
        stream.read.assert_called_once_with(remote.MAX_INVENTORY_BYTES + 1)

    def test_large_sparse_default_inventory_file_is_rejected_promptly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            with config_path.open("wb") as stream:
                stream.seek(remote.MAX_INVENTORY_BYTES * 32)
                stream.write(b"x")
            (root / "state.json").write_text(
                '{"hosts":{}}',
                encoding="utf-8",
            )
            started = time.monotonic()

            with self.assertRaises(remote.RemoteInventoryError):
                remote.load_remote_hosts(
                    runner=lambda argv, **kwargs: completed(argv, returncode=1),
                    env={"DSHC_HOME": str(root)},
                )

            self.assertLess(time.monotonic() - started, 1)

    def test_canonical_inventory_filters_and_exposes_no_sensitive_fields(self):
        rows = [
            inventory_row("Ready.Remote"),
            inventory_row("No_DSH", phase="no_dsh"),
            inventory_row("disabled", enabled=False),
            inventory_row("local", local=True),
            inventory_row("orphan", orphaned=True),
            inventory_row("probing", phase="probing"),
        ]
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return json_completed(argv, rows)

        hosts = remote.load_remote_hosts(runner=runner, env={"HOME": "/unused"})

        self.assertEqual(
            [("No_DSH", "no_dsh"), ("Ready.Remote", "ready")],
            [(host.alias, host.phase) for host in hosts],
        )
        self.assertEqual([["dshc", "ls", "--json"]], [call[0] for call in calls])
        serialized = repr(hosts)
        for secret in ("sshInfo", "TOKEN", "workdir", "secret.example"):
            self.assertNotIn(secret, serialized)

    def test_nonzero_ls_uses_merged_files_not_manager_status(self):
        calls = []
        config = {"hosts": {"edge": {"enabled": True, "local": False}}}
        state = {"hosts": {"edge": {"phase": "ready", "orphaned": False}}}

        def runner(argv, **kwargs):
            del kwargs
            calls.append(argv)
            return completed(argv, returncode=1, stderr=b"not running")

        def reader(path):
            value = config if path.name == "config.json" else state
            return json.dumps(value).encode("utf-8")

        hosts = remote.load_remote_hosts(
            runner=runner,
            env={"DSHC_HOME": "/inventory"},
            file_reader=reader,
        )

        self.assertEqual(["edge"], [host.alias for host in hosts])
        self.assertEqual([["dshc", "ls", "--json"]], calls)

    def test_manager_status_shape_is_never_accepted_as_host_inventory(self):
        status = {
            "running": True,
            "hosts": [inventory_row("manager-row")],
        }

        def runner(argv, **kwargs):
            del kwargs
            if argv[1] == "status":
                return json_completed(argv, status)
            return completed(argv, returncode=1)

        def reader(path):
            value = (
                {"hosts": {"fallback": {"enabled": True, "local": False}}}
                if path.name == "config.json"
                else {
                    "hosts": {
                        "fallback": {"phase": "ready", "orphaned": False}
                    }
                }
            )
            return json.dumps(value).encode("utf-8")

        hosts = remote.load_remote_hosts(
            runner=runner,
            env={"DSHC_HOME": "/inventory"},
            file_reader=reader,
        )

        self.assertEqual(["fallback"], [host.alias for host in hosts])

    def test_successful_malformed_canonical_output_is_rejected(self):
        def runner(argv, **kwargs):
            del kwargs
            return completed(argv, stdout=b'{"hosts":[{"name":"edge"}]}')

        with self.assertRaises(remote.RemoteInventoryError):
            remote.load_remote_hosts(runner=runner)

    def test_deep_and_surrogate_inventory_fail_as_inventory_error(self):
        payloads = (
            ("[" * 5000 + "0" + "]" * 5000).encode("ascii"),
            b'[{"name":"\\ud800"}]',
        )
        for payload in payloads:
            with self.subTest(size=len(payload)):
                with self.assertRaises(remote.RemoteInventoryError):
                    remote.load_remote_hosts(
                        runner=lambda argv, payload=payload, **kwargs: completed(
                            argv,
                            stdout=payload,
                        )
                    )

    def test_alias_collision_is_rejected_before_eligibility_filtering(self):
        rows = [
            inventory_row("Edge", enabled=False),
            inventory_row("edge", phase="probing"),
        ]

        with self.assertRaises(remote.RemoteInventoryError):
            remote.load_remote_hosts(
                runner=lambda argv, **kwargs: json_completed(argv, rows)
            )

    def test_alias_shape_and_oversized_inventory_are_rejected(self):
        for alias in ("-proxy", "host;echo", "white space"):
            with self.subTest(alias=alias):
                with self.assertRaises(remote.RemoteInventoryError):
                    remote.load_remote_hosts(
                        runner=lambda argv, alias=alias, **kwargs: json_completed(
                            argv,
                            [inventory_row(alias)],
                        )
                    )
        with self.assertRaises(remote.RemoteInventoryError):
            remote.load_remote_hosts(
                runner=lambda argv, **kwargs: completed(
                    argv,
                    stdout=b"x" * (remote.MAX_INVENTORY_BYTES + 1),
                )
            )

    def test_file_fallback_merges_config_and_state_and_ignores_state_only(self):
        config = {
            "configVersion": 1,
            "hosts": {
                "good": {"enabled": True, "local": False},
                "disabled": {"enabled": False, "local": False},
                "local": {"enabled": True, "local": True},
                "config-only": {"enabled": True, "local": False},
            },
        }
        state = {
            "hosts": {
                "good": {"phase": "ready", "orphaned": False},
                "disabled": {"phase": "ready"},
                "local": {"phase": "no_dsh"},
                "state-only": {"phase": "ready"},
            }
        }
        reads = []

        def runner(argv, **kwargs):
            del kwargs
            return completed(argv, returncode=1)

        def reader(path):
            reads.append(path)
            value = config if path.name == "config.json" else state
            return json.dumps(value).encode("utf-8")

        hosts = remote.load_remote_hosts(
            runner=runner,
            env={"DSHC_HOME": "/inventory"},
            file_reader=reader,
        )

        self.assertEqual(["good"], [host.alias for host in hosts])
        self.assertEqual(
            [Path("/inventory/config.json"), Path("/inventory/state.json")],
            reads,
        )

    def test_fallback_never_accepts_state_without_config(self):
        def runner(argv, **kwargs):
            del kwargs
            return completed(argv, returncode=1)

        def reader(path):
            if path.name == "config.json":
                raise FileNotFoundError(path)
            return b'{"hosts":{"state-only":{"phase":"ready"}}}'

        with self.assertRaises(remote.RemoteInventoryError):
            remote.load_remote_hosts(
                runner=runner,
                env={"DSHC_HOME": "/inventory"},
                file_reader=reader,
            )

    def test_fallback_requires_explicit_non_orphaned_state(self):
        config = {"hosts": {"edge": {"enabled": True, "local": False}}}

        def runner(argv, **kwargs):
            del kwargs
            return completed(argv, returncode=1)

        for orphaned in (None, 0, "false"):
            with self.subTest(orphaned=orphaned):
                state_row = {"phase": "ready"}
                if orphaned is not None:
                    state_row["orphaned"] = orphaned

                def reader(path, state_row=state_row):
                    value = (
                        config
                        if path.name == "config.json"
                        else {"hosts": {"edge": state_row}}
                    )
                    return json.dumps(value).encode("utf-8")

                with self.assertRaises(remote.RemoteInventoryError):
                    remote.load_remote_hosts(
                        runner=runner,
                        env={"DSHC_HOME": "/inventory"},
                        file_reader=reader,
                    )

    def test_canonical_inventory_skips_reserved_local_identity(self):
        rows = [
            {"name": "LOCAL", "local": True, "config": {}},
            inventory_row("edge", local=False),
        ]

        hosts = remote.load_remote_hosts(
            runner=lambda argv, **kwargs: json_completed(argv, rows)
        )

        self.assertEqual(["edge"], [host.alias for host in hosts])

    def test_inventory_rejects_more_than_bounded_fleet(self):
        rows = [
            inventory_row("edge-{}".format(index))
            for index in range(remote.MAX_HOSTS + 1)
        ]

        with self.assertRaises(remote.RemoteInventoryError):
            remote.load_remote_hosts(
                runner=lambda argv, **kwargs: json_completed(argv, rows)
            )


class ZipappTests(unittest.TestCase):
    def test_archive_is_reproducible_stored_and_source_only(self):
        first = remote.build_zipapp_bytes()
        second = remote.build_zipapp_bytes()

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), remote.MAX_ARTIFACT_BYTES)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            self.assertEqual(sorted(names), names)
            self.assertIn("__main__.py", names)
            self.assertIn("sidecar/__init__.py", names)
            self.assertIn("sidecar/remote.py", names)
            self.assertTrue(all(name.endswith(".py") for name in names))
            self.assertFalse(any("tests/" in name or ".git/" in name for name in names))
            self.assertTrue(
                all(info.compress_type == zipfile.ZIP_STORED for info in infos)
            )
            self.assertTrue(
                all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
            )
            self.assertTrue(
                all(((info.external_attr >> 16) & 0o777) == 0o644 for info in infos)
            )

    def test_atomic_path_build_runs_version_and_list_with_isolated_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_path = root / "dist" / "agent-sidecar.pyz"
            result_path = remote.build_zipapp_to_path(artifact_path)
            environment = os.environ.copy()
            environment["HOME"] = str(root / "home")
            for variable in (
                "DSH_HOME",
                "DSHC_HOME",
                "KIMI_CODE_HOME",
                "CODEX_HOME",
                "CLAUDE_CONFIG_DIR",
            ):
                environment.pop(variable, None)

            version = subprocess.run(
                [sys.executable, "-I", str(result_path), "--version"],
                cwd=str(root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
            listing = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(result_path),
                    "list",
                    "--json",
                    "--all",
                ],
                cwd=str(root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(artifact_path, result_path)
            self.assertEqual(0, version.returncode, version.stderr)
            self.assertIn("0.3.0", version.stdout)
            self.assertEqual(0, listing.returncode, listing.stderr)
            self.assertIsInstance(json.loads(listing.stdout), list)
            self.assertEqual(
                remote.build_zipapp_bytes(),
                artifact_path.read_bytes(),
            )


class SSHExecutionTests(unittest.TestCase):
    def test_ssh_argv_has_fixed_options_no_pty_and_alias_as_data(self):
        argv = remote.ssh_argv("edge.safe", command="list")

        self.assertEqual(
            (
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=4",
                "-o",
                "ConnectionAttempts=1",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "UpdateHostKeys=no",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
                "-o",
                "ControlPersist=no",
                "-T",
                "edge.safe",
            ),
            argv[:-1],
        )
        self.assertNotIn("edge.safe", argv[-1])
        self.assertIn("list --json --all", argv[-1])
        with self.assertRaises(ValueError):
            remote.ssh_argv("edge; touch /tmp/pwned", command="status")
        with self.assertRaises(ValueError):
            remote.remote_shell_command("watch")

    def test_old_python_is_gated_before_artifact_transfer(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return probe_completed(argv, (3, 8, 19))

        rows, failure = remote.execute_remote_host(
            remote.RemoteHost("old", "ready"),
            "status",
            b"zipapp",
            runner=runner,
        )

        self.assertIsNone(rows)
        self.assertEqual("python_too_old", failure.code)
        self.assertEqual(1, len(calls))
        self.assertNotIn("input", calls[0][1])

    def test_auth_and_host_key_errors_are_codes_without_stderr(self):
        cases = (
            (b"Permission denied (publickey). TOP SECRET", "auth"),
            (b"Host key verification failed. TOP SECRET", "host_key"),
        )
        for stderr, expected in cases:
            with self.subTest(expected=expected):
                def runner(argv, **kwargs):
                    del kwargs
                    return completed(argv, returncode=255, stderr=stderr)

                rows, failure = remote.execute_remote_host(
                    remote.RemoteHost("edge", "ready"),
                    "status",
                    b"zipapp",
                    runner=runner,
                )

                self.assertIsNone(rows)
                self.assertEqual(expected, failure.code)
                self.assertNotIn("SECRET", repr(failure))

    def test_timeout_and_unreachable_are_sanitized(self):
        def timeout_runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        rows, failure = remote.execute_remote_host(
            remote.RemoteHost("slow", "ready"),
            "status",
            b"zipapp",
            runner=timeout_runner,
        )
        self.assertIsNone(rows)
        self.assertEqual("timeout", failure.code)

        rows, failure = remote.execute_remote_host(
            remote.RemoteHost("offline", "ready"),
            "status",
            b"zipapp",
            runner=lambda argv, **kwargs: completed(
                argv,
                returncode=255,
                stderr=b"ssh: connect to host: No route to host",
            ),
        )
        self.assertIsNone(rows)
        self.assertEqual("unreachable", failure.code)

    def test_transport_overflow_is_always_protocol(self):
        probe_overflow = BoundedProcessResult(
            args=("ssh",),
            returncode=0,
            stdout=b"x" * 1024,
            stderr=b"",
            overflow="stdout",
        )
        rows, failure = remote.execute_remote_host(
            remote.RemoteHost("edge", "ready"),
            "status",
            b"zipapp",
            runner=lambda argv, **kwargs: probe_overflow,
        )
        self.assertIsNone(rows)
        self.assertEqual("protocol", failure.code)

        execution_overflow = BoundedProcessResult(
            args=("ssh",),
            returncode=255,
            stdout=b"",
            stderr=b"x" * remote.MAX_STDERR_BYTES,
            overflow="stderr",
        )
        calls = []

        def runner(argv, **kwargs):
            del kwargs
            calls.append(argv)
            return probe_completed(argv) if len(calls) == 1 else execution_overflow

        rows, failure = remote.execute_remote_host(
            remote.RemoteHost("edge", "ready"),
            "status",
            b"zipapp",
            runner=runner,
        )
        self.assertIsNone(rows)
        self.assertEqual("protocol", failure.code)
        self.assertEqual(2, len(calls))

    def test_banner_duplicate_line_and_oversize_probe_are_protocol_failures(self):
        payloads = (
            b'Welcome\n{"python":[3,11,0]}\n',
            b'{"python":[3,11,0]}\n\n',
            b"x" * 1025,
            b'{"python":["\\ud800",11,0]}',
            ("[" * 500 + "0" + "]" * 500).encode("ascii"),
        )
        for payload in payloads:
            with self.subTest(size=len(payload)):
                rows, failure = remote.execute_remote_host(
                    remote.RemoteHost("edge", "ready"),
                    "status",
                    b"zipapp",
                    runner=lambda argv, payload=payload, **kwargs: completed(
                        argv,
                        stdout=payload,
                    ),
                )
                self.assertIsNone(rows)
                self.assertEqual("protocol", failure.code)

    def test_success_transfers_bytes_and_validates_annotated_rows(self):
        calls = []
        artifact = b"zipapp-bytes"

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if len(calls) == 1:
                return probe_completed(argv)
            return execution_completed(argv, [session_row("b"), session_row("a")])

        rows, failure = remote.execute_remote_host(
            remote.RemoteHost("edge", "ready"),
            "list",
            artifact,
            runner=runner,
        )

        self.assertIsNone(failure)
        self.assertEqual(["b", "a"], [row["session_id"] for row in rows])
        self.assertTrue(all(row["host"] == "edge" for row in rows))
        self.assertEqual(artifact, calls[1][1]["input"])
        self.assertNotIn("shell", calls[0][1])
        self.assertNotIn("shell", calls[1][1])

    def test_oversize_or_malformed_execution_response_is_protocol_failure(self):
        payloads = (
            b"x" * (remote.MAX_PROTOCOL_BYTES + 1),
            json.dumps(
                {"ok": True, "rows": [{"agent": "claude", "status": "waiting"}]}
            ).encode("utf-8"),
            json.dumps(
                {
                    "ok": True,
                    "rows": [
                        {
                            "agent": "claude",
                            "session_id": "one",
                            "status": "unknown",
                        }
                    ],
                }
            ).encode("utf-8"),
        )
        for payload in payloads:
            calls = []

            def runner(argv, payload=payload, **kwargs):
                del kwargs
                calls.append(argv)
                if len(calls) == 1:
                    return probe_completed(argv)
                return completed(argv, stdout=payload)

            rows, failure = remote.execute_remote_host(
                remote.RemoteHost("edge", "ready"),
                "status",
                b"zipapp",
                runner=runner,
            )
            self.assertIsNone(rows)
            self.assertEqual("protocol", failure.code)

    def test_session_rows_require_exact_complete_schema_and_types(self):
        cases = []
        missing = session_row()
        missing.pop("title")
        cases.append(missing)
        extra_key = session_row()
        extra_key["unexpected"] = True
        cases.append(extra_key)
        invalid_values = (
            ("agent", ""),
            ("agent", "x" * 257),
            ("session_id", ""),
            ("project", 7),
            ("transcript", "\ud800"),
            ("title", "x" * 65537),
            ("updated_at", True),
            ("updated_at", float("nan")),
            ("updated_at", float("inf")),
            ("updated_at", -1),
            ("updated_at", remote.MAX_SESSION_TIMESTAMP + 1),
            ("status", "unknown"),
            ("extra", []),
            ("parent_id", 1),
            ("parent_id", "x" * 4097),
        )
        for key, value in invalid_values:
            row = session_row()
            row[key] = value
            cases.append(row)

        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    remote._validate_protocol_rows([row], "edge")

    def test_session_row_validation_accepts_datetime_safe_int_before_annotation(self):
        row = session_row()
        row["updated_at"] = remote.MAX_SESSION_TIMESTAMP

        rows = remote._validate_protocol_rows([row], "edge")

        self.assertEqual(remote.MAX_SESSION_TIMESTAMP, rows[0]["updated_at"])
        self.assertEqual("edge", rows[0]["host"])
        self.assertNotIn("host", row)

    def test_bootstrap_cleanup_and_child_contract_are_structural(self):
        bootstrap = remote.REMOTE_BOOTSTRAP
        self.assertIn("tempfile.mkstemp", bootstrap)
        self.assertIn("os.fchmod(fd, 0o600)", bootstrap)
        self.assertIn("[sys.executable, \"-I\", path] + child_args", bootstrap)
        self.assertIn("selectors.DefaultSelector", bootstrap)
        self.assertIn("os.set_blocking", bootstrap)
        self.assertIn("MAX_OUTPUT = 8388608", bootstrap)
        self.assertIn("MAX_ERROR = 65536", bootstrap)
        self.assertIn("start_new_session=True", bootstrap)
        self.assertIn("os.killpg(process.pid, signal.SIGKILL)", bootstrap)
        self.assertIn("process.wait(timeout=1)", bootstrap)
        self.assertNotIn("subprocess.run(", bootstrap)
        self.assertNotIn(".communicate(", bootstrap)
        self.assertIn("stdout=subprocess.PIPE", bootstrap)
        self.assertIn("finally:", bootstrap)
        self.assertIn("os.unlink(path)", bootstrap)
        self.assertNotIn("daemon", bootstrap)
        self.assertNotIn("watch", bootstrap)
        self.assertNotIn("send", bootstrap)


class EmbeddedBootstrapTests(unittest.TestCase):
    def _zipapp(self, source):
        output = io.BytesIO()
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_STORED,
        ) as archive:
            archive.writestr("__main__.py", source)
        return output.getvalue()

    def _run_bootstrap(self, root, source, *, child_timeout=None):
        bootstrap = remote.REMOTE_BOOTSTRAP
        if child_timeout is not None:
            bootstrap = bootstrap.replace(
                "CHILD_TIMEOUT = 8",
                "CHILD_TIMEOUT = {}".format(child_timeout),
            )
        environment = os.environ.copy()
        environment["HOME"] = str(root)
        environment["TMPDIR"] = str(root)
        environment["CHILD_PID"] = str(root / "child.pid")
        environment["GRANDCHILD_PID"] = str(root / "grandchild.pid")
        return subprocess.run(
            [sys.executable, "-I", "-c", bootstrap, "status", "--json"],
            input=self._zipapp(source),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            check=False,
            timeout=4,
        )

    def test_bootstrap_caps_stdout_and_stderr_in_flight_without_hanging(self):
        for descriptor in (1, 2):
            with self.subTest(descriptor=descriptor):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    source = (
                        "import os\n"
                        "with open(os.environ['CHILD_PID'],'w') as stream:\n"
                        "    stream.write(str(os.getpid()))\n"
                        "chunk = b'x' * 65536\n"
                        "while True:\n"
                        "    os.write({}, chunk)\n".format(descriptor)
                    )
                    started = time.monotonic()

                    result = self._run_bootstrap(root, source)

                    self.assertLess(time.monotonic() - started, 3.0)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertEqual(
                        {"ok": False, "code": "protocol"},
                        json.loads(result.stdout),
                    )
                    pid = int((root / "child.pid").read_text(encoding="ascii"))
                    self.assertFalse(process_exists(pid))
                    self.assertEqual([], list(root.glob("*.pyz")))

    def test_bootstrap_timeout_reaps_group_and_unlinks_zipapp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = (
                "import os,subprocess,sys,time\n"
                "subprocess.Popen([sys.executable, '-c', "
                "\"import os,time; "
                "open(os.environ['GRANDCHILD_PID'],'w').write(str(os.getpid())); "
                "time.sleep(60)\"])\n"
                "with open(os.environ['CHILD_PID'],'w') as stream:\n"
                "    stream.write(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            started = time.monotonic()

            result = self._run_bootstrap(root, source, child_timeout=0.5)

            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                {"ok": False, "code": "timeout"},
                json.loads(result.stdout),
            )
            pid = int((root / "child.pid").read_text(encoding="ascii"))
            grandchild_pid = int(
                (root / "grandchild.pid").read_text(encoding="ascii")
            )
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and (
                process_exists(pid) or process_exists(grandchild_pid)
            ):
                time.sleep(0.01)
            self.assertFalse(process_exists(pid))
            self.assertFalse(process_exists(grandchild_pid))
            self.assertEqual([], list(root.glob("*.pyz")))


class BoundedTransportTests(unittest.TestCase):
    def _pid_path(self, root, name):
        return Path(root) / "{}.pid".format(name)

    def _read_pid(self, path):
        return int(path.read_text(encoding="ascii"))

    @mock.patch.object(remote, "_run_bounded")
    def test_remote_wrapper_keeps_thirty_second_timeout_ceiling(self, run_bounded):
        with self.assertRaises(ValueError):
            remote._bounded_popen(
                [sys.executable, "-c", "pass"],
                stdout_limit=1,
                timeout=remote.FLEET_TIMEOUT_SECONDS + 0.1,
            )

        run_bounded.assert_not_called()

    def test_real_child_stdout_and_stderr_floods_are_capped_and_reaped(self):
        with tempfile.TemporaryDirectory() as temporary:
            for descriptor, name, limit in (
                (1, "stdout", 1024),
                (2, "stderr", remote.MAX_STDERR_BYTES),
            ):
                with self.subTest(stream=name):
                    pid_path = self._pid_path(temporary, name)
                    code = (
                        "import os,sys;"
                        "p=open(sys.argv[1],'w');p.write(str(os.getpid()));p.close();"
                        "fd=int(sys.argv[2]);chunk=b'x'*65536;"
                        "\nwhile True: os.write(fd,chunk)"
                    )
                    result = remote._bounded_popen(
                        [sys.executable, "-c", code, str(pid_path), str(descriptor)],
                        stdout_limit=limit,
                        stderr_limit=remote.MAX_STDERR_BYTES,
                        timeout=2.0,
                    )
                    pid = self._read_pid(pid_path)
                    self.assertEqual(name, result.overflow)
                    self.assertLessEqual(len(result.stdout), limit)
                    self.assertLessEqual(
                        len(result.stderr),
                        remote.MAX_STDERR_BYTES,
                    )
                    self.assertFalse(process_exists(pid))

    def test_real_child_hard_timeout_reaps_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = self._pid_path(temporary, "hang")
            code = (
                "import os,sys,time;"
                "p=open(sys.argv[1],'w');p.write(str(os.getpid()));p.close();"
                "time.sleep(60)"
            )
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                remote._bounded_popen(
                    [sys.executable, "-c", code, str(pid_path)],
                    stdout_limit=1024,
                    timeout=0.2,
                )
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertFalse(process_exists(self._read_pid(pid_path)))

    def test_real_child_that_never_reads_stdin_cannot_deadlock(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = self._pid_path(temporary, "stdin")
            code = (
                "import os,sys,time;"
                "p=open(sys.argv[1],'w');p.write(str(os.getpid()));p.close();"
                "time.sleep(60)"
            )
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                remote._bounded_popen(
                    [sys.executable, "-c", code, str(pid_path)],
                    input_data=b"x" * remote.MAX_ARTIFACT_BYTES,
                    stdout_limit=1024,
                    timeout=0.2,
                )
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertFalse(process_exists(self._read_pid(pid_path)))


class AggregationTests(unittest.TestCase):
    def _assert_worker_signal_cleanup(self, exit_signal):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child_pid_path = root / "ssh.pid"
            descendant_pid_path = root / "ssh-descendant.pid"
            executable = root / "ssh"
            executable.write_text(
                "#!{}\n"
                "import os,subprocess,sys,time\n"
                "code=\"import os,sys,time;"
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                "time.sleep(60)\"\n"
                "subprocess.Popen([sys.executable,'-c',code,"
                "os.environ['SSH_DESCENDANT_PID']])\n"
                "open(os.environ['SSH_CHILD_PID'],'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n".format(sys.executable),
                encoding="utf-8",
            )
            executable.chmod(0o755)
            parent_code = (
                "import signal,sys,threading\n"
                "from sidecar.remote import RemoteHost,aggregate_remote\n"
                "try:\n"
                "    aggregate_remote(\n"
                "        'status',hosts=(RemoteHost('edge','ready'),),\n"
                "        artifact=b'zipapp',fleet_timeout=30,\n"
                "    )\n"
                "except KeyboardInterrupt:\n"
                "    values=(signal.SIGTERM,signal.SIGHUP)\n"
                "    if any(signal.getsignal(value) != signal.SIG_DFL "
                "for value in values):\n"
                "        raise SystemExit(7)\n"
                "    if any(thread.name.startswith('sidecar-remote') "
                "for thread in threading.enumerate()):\n"
                "        raise SystemExit(8)\n"
                "    raise SystemExit(130)\n"
            )
            environment = os.environ.copy()
            environment["PATH"] = str(root) + os.pathsep + environment["PATH"]
            environment["SSH_CHILD_PID"] = str(child_pid_path)
            environment["SSH_DESCENDANT_PID"] = str(descendant_pid_path)
            parent = subprocess.Popen(
                [sys.executable, "-c", parent_code],
                cwd=str(REPO_ROOT),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid = None
            descendant_pid = None
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and (
                    not child_pid_path.exists()
                    or not descendant_pid_path.exists()
                ):
                    if parent.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertTrue(child_pid_path.exists())
                self.assertTrue(descendant_pid_path.exists())
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                descendant_pid = int(
                    descendant_pid_path.read_text(encoding="ascii")
                )

                started = time.monotonic()
                os.kill(parent.pid, exit_signal)
                returncode = parent.wait(timeout=4)

                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(130, returncode)
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
                for pid in (child_pid, descendant_pid):
                    if pid is not None and process_exists(pid):
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    @unittest.skipUnless(os.name == "posix", "exit signals require POSIX")
    def test_exit_signals_cleanup_worker_process_groups_and_threads(self):
        for exit_signal in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(exit_signal=exit_signal):
                self._assert_worker_signal_cleanup(exit_signal)

    def test_partial_fleet_preserves_success_and_sorts_deterministically(self):
        hosts = (
            remote.RemoteHost("z-good", "ready"),
            remote.RemoteHost("a-bad", "no_dsh"),
        )

        def runner(argv, **kwargs):
            alias = argv[-2]
            is_probe = "MAX_ARTIFACT" not in argv[-1]
            if is_probe:
                return probe_completed(argv)
            if alias == "a-bad":
                return completed(
                    argv,
                    returncode=255,
                    stderr=b"Permission denied (publickey). secret key path",
                )
            return execution_completed(
                argv,
                [session_row("two"), session_row("one")],
            )

        result = remote.aggregate_remote(
            "status",
            hosts=hosts,
            max_workers=99,
            runner=runner,
            artifact=b"zipapp",
        )

        self.assertTrue(result.partial)
        self.assertEqual(remote.EXIT_OK, result.exit_code)
        self.assertEqual(("z-good",), result.succeeded)
        self.assertEqual(
            [("z-good", "one"), ("z-good", "two")],
            [(row["host"], row["session_id"]) for row in result.rows],
        )
        self.assertEqual(
            [("a-bad", "auth")],
            [(failure.host, failure.code) for failure in result.failures],
        )
        self.assertNotIn("secret", repr(result))

    def test_no_successful_host_has_distinct_exit(self):
        def runner(argv, **kwargs):
            del kwargs
            return completed(argv, returncode=255, stderr=b"Connection refused")

        all_failed = remote.aggregate_remote(
            "status",
            hosts=(remote.RemoteHost("edge", "ready"),),
            runner=runner,
            artifact=b"zipapp",
        )
        self.assertEqual(remote.EXIT_NO_SUCCESS, all_failed.exit_code)
        self.assertEqual("unreachable", all_failed.failures[0].code)

    def test_selected_aliases_are_case_insensitive_and_typos_fail(self):
        hosts = (
            remote.RemoteHost("Canonical.Edge", "ready"),
            remote.RemoteHost("other", "ready"),
        )

        def runner(argv, **kwargs):
            del kwargs
            if "MAX_ARTIFACT" not in argv[-1]:
                return probe_completed(argv)
            return execution_completed(argv, [session_row("selected")])

        result = remote.aggregate_remote(
            "status",
            hosts=hosts,
            selected=("canonical.edge",),
            runner=runner,
            artifact=b"zipapp",
        )
        self.assertEqual(("Canonical.Edge",), result.hosts)
        self.assertEqual("Canonical.Edge", result.rows[0]["host"])

        with self.assertRaises(ValueError):
            remote.aggregate_remote(
                "status",
                hosts=hosts,
                selected=("typo",),
                artifact=b"zipapp",
            )

    def test_scheduler_caps_concurrency_and_submits_incrementally(self):
        hosts = tuple(
            remote.RemoteHost("edge-{:02d}".format(index), "ready")
            for index in range(20)
        )
        lock = threading.Lock()
        active = 0
        peak = 0

        def runner(argv, **kwargs):
            nonlocal active, peak
            del kwargs
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.005)
                if "MAX_ARTIFACT" not in argv[-1]:
                    return probe_completed(argv)
                return execution_completed(argv, [session_row(argv[-2])])
            finally:
                with lock:
                    active -= 1

        result = remote.aggregate_remote(
            "status",
            hosts=hosts,
            max_workers=99,
            runner=runner,
            artifact=b"zipapp",
        )

        self.assertEqual(20, len(result.succeeded))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, remote.MAX_WORKERS)

    def test_fleet_deadline_marks_running_and_queued_hosts_timeout(self):
        hosts = tuple(
            remote.RemoteHost("slow-{:02d}".format(index), "ready")
            for index in range(20)
        )
        called = set()
        lock = threading.Lock()

        def runner(argv, **kwargs):
            with lock:
                called.add(argv[-2])
            delay = min(0.04, kwargs["timeout"] + 0.001)
            time.sleep(delay)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        started = time.monotonic()
        result = remote.aggregate_remote(
            "status",
            hosts=hosts,
            max_workers=2,
            runner=runner,
            artifact=b"zipapp",
            fleet_timeout=0.05,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2)
        self.assertEqual(20, len(result.failures))
        self.assertTrue(all(item.code == "timeout" for item in result.failures))
        self.assertLess(len(called), len(hosts))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            leaked = [
                thread
                for thread in threading.enumerate()
                if thread.name.startswith("sidecar-remote")
            ]
            if not leaked:
                break
            time.sleep(0.01)
        self.assertFalse(leaked)

    def test_aggregate_row_cap_rejects_only_host_that_exceeds_remainder(self):
        hosts = (
            remote.RemoteHost("a-small", "ready"),
            remote.RemoteHost("b-large", "ready"),
        )

        def runner(argv, **kwargs):
            del kwargs
            if "MAX_ARTIFACT" not in argv[-1]:
                return probe_completed(argv)
            count = 1 if argv[-2] == "a-small" else 2
            return execution_completed(
                argv,
                [session_row("{}-{}".format(argv[-2], index)) for index in range(count)],
            )

        with mock.patch.object(remote, "MAX_ROWS", 2):
            result = remote.aggregate_remote(
                "status",
                hosts=hosts,
                max_workers=1,
                runner=runner,
                artifact=b"zipapp",
            )

        self.assertEqual(("a-small",), result.succeeded)
        self.assertEqual(1, len(result.rows))
        self.assertEqual([("b-large", "protocol")], [
            (failure.host, failure.code) for failure in result.failures
        ])

    def test_aggregate_byte_cap_rejects_only_over_budget_host(self):
        hosts = (
            remote.RemoteHost("a-small", "ready"),
            remote.RemoteHost("b-over", "ready"),
        )
        first = session_row("a-small")
        first_annotated = dict(first)
        first_annotated["host"] = "a-small"
        exact_first_budget = len(remote._encoded_row(first_annotated)) + 3

        def runner(argv, **kwargs):
            del kwargs
            if "MAX_ARTIFACT" not in argv[-1]:
                return probe_completed(argv)
            return execution_completed(argv, [session_row(argv[-2])])

        with mock.patch.object(
            remote,
            "MAX_AGGREGATE_BYTES",
            exact_first_budget,
        ):
            result = remote.aggregate_remote(
                "status",
                hosts=hosts,
                max_workers=1,
                runner=runner,
                artifact=b"zipapp",
            )

        self.assertEqual(("a-small",), result.succeeded)
        self.assertEqual([("b-over", "protocol")], [
            (failure.host, failure.code) for failure in result.failures
        ])


if __name__ == "__main__":
    unittest.main()
