import concurrent.futures
import dataclasses
import hashlib
import io
import json
import os
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

import sidecar
from sidecar import remote, remote_inventory, remote_transport, remote_types
from sidecar.json_limits import (
    JSONLimitError,
    JSONLimits,
    JSONSyntaxError,
    parse_json,
    validate_json,
)
from sidecar.process_runner import BoundedProcessResult
from sidecar.release import build_release_zipapp


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


def probe_completed(argv, version=(3, 11, 0), executable="/usr/bin/python3"):
    return json_completed(
        argv,
        {"python": list(version), "executable": executable},
    )


def probe_exhausted(argv):
    return json_completed(argv, {"python": None})


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


def minimal_session_row(session_id="s"):
    return {
        "agent": "a",
        "session_id": session_id,
        "project": "",
        "transcript": "",
        "updated_at": 0,
        "title": "",
        "status": "idle",
        "extra": {},
        "parent_id": None,
    }


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


class TrackingFuture(concurrent.futures.Future):
    def __init__(self):
        super().__init__()
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1
        return super().cancel()


class Python38StyleExecutor:
    """Controlled executor whose shutdown accepts only Python 3.8 arguments."""

    def __init__(self, *futures):
        self._futures = iter(futures)
        self.cancel_events = []
        self.shutdown_calls = []

    def submit(self, function, *args, **kwargs):
        del function, args
        self.cancel_events.append(kwargs["cancel_event"])
        return next(self._futures)

    def shutdown(self, wait=True):
        self.shutdown_calls.append(wait)


class RemoteDataModelTests(unittest.TestCase):
    def test_models_are_frozen_and_failure_contains_only_code(self):
        host = remote.RemoteHost("edge-1", "ready")
        failure = remote.RemoteFailure("edge-1", "auth")
        probe = remote_types.ProbeResult((3, 11, 4), "/opt/python3.11")
        aggregate = remote.RemoteAggregate(
            "status",
            failures=(failure,),
            hosts=("edge-1",),
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            host.alias = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            failure.code = "remote"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            probe.executable = "/opt/other"
        self.assertEqual((3, 11, 4), probe.version)
        self.assertEqual("/opt/python3.11", probe.executable)
        self.assertEqual(("version", "executable"), tuple(probe.__dataclass_fields__))
        self.assertFalse(hasattr(host, "host"))
        self.assertFalse(hasattr(host, "label"))
        self.assertEqual({"host": "edge-1", "code": "auth"}, failure.to_dict())
        self.assertNotIn("stderr", aggregate.to_dict()["failures"][0])
        self.assertEqual(remote.EXIT_NO_SUCCESS, aggregate.exit_code)
        self.assertNotIn("build_zipapp", remote.__all__)
        self.assertNotIn("build_zipapp_to_path", remote.__all__)
        self.assertFalse(hasattr(remote, "build_zipapp_to_path"))
        self.assertFalse(hasattr(remote, "MAX_JSON_EXTRA_ITEMS"))
        self.assertFalse(hasattr(remote, "MAX_ROW_BYTES"))

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
        empty = remote.RemoteAggregate("status")

        self.assertTrue(partial.partial)
        self.assertEqual(remote.EXIT_OK, partial.exit_code)
        self.assertFalse(complete.partial)
        self.assertEqual(remote.EXIT_OK, complete.exit_code)
        self.assertFalse(empty.partial)
        self.assertEqual(remote.EXIT_OK, empty.exit_code)
        self.assertEqual(remote.EXIT_OK, empty.to_dict()["exit_code"])
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
        for phase in ("running", "degraded"):
            with self.subTest(phase=phase):
                self.assertEqual(phase, remote.RemoteHost("edge", phase).phase)
        for alias in ("local", "LOCAL", "Local"):
            with self.subTest(alias=alias):
                with self.assertRaises(ValueError):
                    remote.RemoteHost(alias, "ready")


class BoundedJSONTests(unittest.TestCase):
    def test_canonical_limits_are_frozen_and_errors_are_neutral(self):
        limits = JSONLimits(
            max_bytes=32,
            max_depth=2,
            max_items=2,
            max_nodes=4,
            max_string_bytes=3,
            max_integer_bits=8,
        )

        with self.assertRaises(dataclasses.FrozenInstanceError):
            limits.max_bytes = 64
        with self.assertRaises(JSONSyntaxError):
            parse_json(b'{"key":1,"key":2}', limits)
        with self.assertRaises(JSONLimitError):
            parse_json(b'"four"', limits)
        cyclic = []
        cyclic.append(cyclic)
        with self.assertRaises(JSONSyntaxError):
            validate_json(cyclic, JSONLimits())

    def test_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        for payload in (
            b'{"host":"a","host":"b"}',
            b'{"value":NaN}',
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError) as raised:
                    remote.parse_bounded_json(payload, max_bytes=100)
                self.assertNotIsInstance(
                    raised.exception,
                    remote.ProtocolResourceLimitError,
                )

    def test_payload_size_depth_and_string_limits_are_bounded(self):
        with self.assertRaises(remote.ProtocolResourceLimitError):
            remote.parse_bounded_json(b"[]" * 10, max_bytes=5)
        self.assertEqual(
            [],
            remote.parse_bounded_json(b"[]", max_bytes=2),
        )
        with self.assertRaises(remote.ProtocolResourceLimitError):
            remote.parse_bounded_json(b"[]", max_bytes=1)
        nested = (
            "[" * (remote.MAX_JSON_DEPTH + 2) + "0" + "]" * (remote.MAX_JSON_DEPTH + 2)
        )
        with self.assertRaises(remote.ProtocolResourceLimitError):
            remote.parse_bounded_json(nested, max_bytes=len(nested.encode("utf-8")))
        boundary_string = json.dumps("x" * remote.MAX_JSON_STRING_BYTES)
        self.assertEqual(
            "x" * remote.MAX_JSON_STRING_BYTES,
            remote.parse_bounded_json(
                boundary_string,
                max_bytes=len(boundary_string.encode("utf-8")),
            ),
        )
        oversized_string = json.dumps("x" * (remote.MAX_JSON_STRING_BYTES + 1))
        with self.assertRaises(remote.ProtocolResourceLimitError):
            remote.parse_bounded_json(
                oversized_string,
                max_bytes=len(oversized_string.encode("utf-8")),
            )

        depth_boundary = None
        for _index in range(remote.MAX_JSON_DEPTH):
            depth_boundary = [depth_boundary]
        limits = JSONLimits(max_depth=remote.MAX_JSON_DEPTH)
        validate_json(depth_boundary, limits)
        with self.assertRaises(JSONLimitError):
            validate_json([depth_boundary], limits)

    def test_global_node_budget_accepts_more_than_2600_minimal_rows(self):
        row_count = 3000
        payload = json.dumps(
            {"ok": True, "rows": [minimal_session_row()] * row_count},
            separators=(",", ":"),
        ).encode("utf-8")

        rows = remote_types._parse_execution_response(payload, "edge")

        self.assertLess(len(payload), remote.MAX_PROTOCOL_BYTES)
        self.assertEqual(row_count, len(rows))

    def test_near_max_rows_fit_protocol_bytes_and_global_node_budget(self):
        payload = json.dumps(
            {"ok": True, "rows": [minimal_session_row()] * remote.MAX_ROWS},
            separators=(",", ":"),
        ).encode("utf-8")

        rows = remote_types._parse_execution_response(payload, "edge")

        self.assertLess(len(payload), remote.MAX_PROTOCOL_BYTES)
        self.assertEqual(remote.MAX_ROWS, len(rows))

    def test_deep_and_high_node_extras_have_resource_limit_diagnostic(self):
        deep = {}
        cursor = deep
        for _index in range(remote.MAX_JSON_DEPTH + 1):
            child = {}
            cursor["child"] = child
            cursor = child
        high_node = {"items": [None] * (remote_types.MAX_JSON_EXTRA_ITEMS + 1)}

        for extra in (deep, high_node):
            row = minimal_session_row()
            row["extra"] = extra
            with self.subTest(kind="deep" if extra is deep else "high-node"):
                with self.assertRaises(remote.ProtocolResourceLimitError) as raised:
                    remote_types._validate_protocol_rows([row], "edge")
                self.assertEqual("resource_limit", raised.exception.code)

    def test_parser_maps_recursion_exhaustion_to_resource_limit(self):
        deeply_nested = "[" * 5000 + "0" + "]" * 5000

        with self.assertRaises(remote.ProtocolResourceLimitError):
            remote.parse_bounded_json(
                deeply_nested,
                max_bytes=len(deeply_nested),
            )

    def test_invalid_unicode_remains_malformed_protocol_data(self):
        invalid_values = (
            '"\ud800"',
            b'"\\ud800"',
            b'"\xff"',
        )

        for payload in invalid_values:
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(ValueError) as raised:
                    remote.parse_bounded_json(
                        payload,
                        max_bytes=max(100, len(payload)),
                    )
                self.assertNotIsInstance(
                    raised.exception,
                    remote.ProtocolResourceLimitError,
                )

    def test_node_limit_accepts_boundary_and_rejects_boundary_plus_one(self):
        validate_json(
            [None, None, None],
            JSONLimits(max_nodes=4),
        )

        with self.assertRaises(JSONLimitError):
            validate_json(
                [None, None, None, None],
                JSONLimits(max_nodes=4),
            )


class InventoryTests(unittest.TestCase):
    @mock.patch.object(remote_inventory, "_run_bounded")
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
            state = {"hosts": {"fallback": {"phase": "ready", "orphaned": False}}}
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

            with mock.patch.object(
                remote_inventory,
                "MAX_INVENTORY_BYTES",
                65536,
            ):
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
            payload = remote_inventory._read_bounded_inventory_file(path)

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
            inventory_row("running", phase="running"),
            inventory_row("degraded", phase="degraded"),
        ]
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return json_completed(argv, rows)

        hosts = remote.load_remote_hosts(runner=runner, env={"HOME": "/unused"})

        self.assertEqual(
            [
                ("degraded", "degraded"),
                ("No_DSH", "no_dsh"),
                ("Ready.Remote", "ready"),
                ("running", "running"),
            ],
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
                else {"hosts": {"fallback": {"phase": "ready", "orphaned": False}}}
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

    def test_center_fixture_filters_by_contract_eligibility(self):
        fixture = REPO_ROOT / "tests" / "fixtures" / "dshc_ls_v1.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))

        hosts = remote_inventory._hosts_from_canonical(payload)

        self.assertEqual(
            [
                ("remote-degraded", "degraded"),
                ("remote-no-dsh", "no_dsh"),
                ("remote-ready", "ready"),
                ("remote-running", "running"),
            ],
            [(host.alias, host.phase) for host in hosts],
        )

    def test_center_bare_array_and_hosts_container_have_same_result(self):
        fixture = REPO_ROOT / "tests" / "fixtures" / "dshc_ls_v1.json"
        payload = json.loads(fixture.read_text(encoding="utf-8"))

        self.assertEqual(
            remote_inventory._hosts_from_canonical(payload),
            remote_inventory._hosts_from_canonical(payload["hosts"]),
        )

    def test_inventory_reads_enabled_and_local_from_config(self):
        payload = [
            {
                "name": "config-only-flags",
                "orphaned": False,
                "phase": "ready",
                "config": {"enabled": True, "local": False},
            }
        ]

        hosts = remote_inventory._hosts_from_canonical(payload)

        self.assertEqual(
            [("config-only-flags", "ready")],
            [(host.alias, host.phase) for host in hosts],
        )

    def test_inventory_rejects_map_alias_name_mismatch(self):
        payload = {
            "hosts": {
                "edge": {
                    "name": "other-edge",
                    "orphaned": False,
                    "phase": "ready",
                    "config": {"enabled": True, "local": False},
                }
            }
        }

        with self.assertRaises(remote.RemoteInventoryError):
            remote_inventory._hosts_from_canonical(payload)


class ZipappTests(unittest.TestCase):
    def _write_outer_archive(self, path, *, omit=(), extras=()):
        with zipfile.ZipFile(io.BytesIO(remote.build_zipapp_bytes())) as source:
            with zipfile.ZipFile(
                path,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=False,
            ) as outer:
                for info in source.infolist():
                    if info.filename not in omit:
                        outer.writestr(info, source.read(info))
                for name, data in extras:
                    outer.writestr(name, data)

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
            self.assertIn("sidecar/remote_inventory.py", names)
            self.assertIn("sidecar/remote_transport.py", names)
            self.assertIn("sidecar/remote_types.py", names)
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

    def test_zipapp_bytes_run_version_and_list_with_isolated_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact_path = root / "dist" / "agent-sidecar.pyz"
            artifact_path.parent.mkdir()
            artifact = remote.build_zipapp_bytes()
            artifact_path.write_bytes(artifact)
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
                [sys.executable, "-I", str(artifact_path), "--version"],
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
                    str(artifact_path),
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

            self.assertEqual(0, version.returncode, version.stderr)
            self.assertEqual(
                "agent-sidecar {}\n".format(sidecar.__version__),
                version.stdout,
            )
            self.assertEqual(0, listing.returncode, listing.stderr)
            self.assertIsInstance(json.loads(listing.stdout), list)
            self.assertEqual(
                artifact,
                artifact_path.read_bytes(),
            )

    def test_zipimport_reader_is_bounded_canonical_and_never_extracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            safe = root / "safe.pyz"
            self._write_outer_archive(
                safe,
                extras=(
                    ("docs/ignored.txt", b"not source"),
                    ("assets/payload.bin", b"\x00\x01"),
                ),
            )

            members = remote_transport._archive_source_members(safe)

            self.assertIn("__main__.py", members)
            self.assertIn("sidecar/remote_transport.py", members)
            self.assertNotIn("docs/ignored.txt", members)
            self.assertNotIn("assets/payload.bin", members)
            self.assertEqual([safe], list(root.iterdir()))

            portable = root / "portable.pyz"
            with zipfile.ZipFile(io.BytesIO(remote.build_zipapp_bytes())) as source:
                with zipfile.ZipFile(portable, mode="w") as outer:
                    for info in source.infolist():
                        outer.writestr(info.filename, source.read(info))
            portable_members = remote_transport._archive_source_members(portable)
            self.assertEqual(members, portable_members)

            traversal = root / "traversal.pyz"
            self._write_outer_archive(
                traversal,
                extras=(("sidecar/../escape.py", b"raise RuntimeError"),),
            )
            with self.assertRaises(ValueError):
                remote_transport._archive_source_members(traversal)
            self.assertFalse((root / "escape.py").exists())

            incomplete = root / "incomplete.pyz"
            self._write_outer_archive(
                incomplete,
                omit=("sidecar/cli.py",),
            )
            with self.assertRaises(ValueError):
                remote_transport._archive_source_members(incomplete)

            with zipfile.ZipFile(safe) as archive:
                member_count = len(archive.infolist()) - 1
            with mock.patch.object(
                remote_transport,
                "_MAX_ARCHIVE_MEMBERS",
                member_count,
            ):
                with self.assertRaises(ValueError):
                    remote_transport._archive_source_members(safe)

    def test_standalone_release_builds_remote_list_and_watch_artifacts(self):
        script = (
            "import hashlib,sys\n"
            "sys.path.insert(0,sys.argv[1])\n"
            "from sidecar import remote\n"
            "host=remote.RemoteHost('edge','ready')\n"
            "artifacts=[]\n"
            "def execute(host,command,artifact,**kwargs):\n"
            "    artifacts.append(artifact)\n"
            "    return (),None\n"
            "remote.execute_remote_host=execute\n"
            "result=remote.aggregate_remote('list',hosts=(host,),max_workers=1)\n"
            "assert result.succeeded == ('edge',)\n"
            "def opener(host,artifact,**kwargs):\n"
            "    artifacts.append(artifact)\n"
            "    return None,remote.RemoteFailure(host.alias,'remote')\n"
            "with remote.watch_remote(hosts=(host,),host_opener=opener) as session:\n"
            "    list(session)\n"
            "rebuilt=remote.build_zipapp_bytes()\n"
            "assert len(artifacts) == 2\n"
            "assert artifacts[0] == artifacts[1] == rebuilt\n"
            "print(hashlib.sha256(rebuilt).hexdigest())\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outer = root / "agent-sidecar.pyz"
            build_release_zipapp(outer)
            expected = hashlib.sha256(remote.build_zipapp_bytes()).hexdigest()

            results = [
                subprocess.run(
                    [sys.executable, "-I", "-c", script, str(outer)],
                    cwd=str(root),
                    env={"PATH": os.environ.get("PATH", "")},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=15,
                )
                for _index in range(2)
            ]

        for result in results:
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(expected + "\n", result.stdout)


class SSHExecutionTests(unittest.TestCase):
    def test_ssh_argv_has_fixed_options_no_pty_and_alias_as_data(self):
        argv = remote.ssh_argv("edge.safe", command="list")
        recent_argv = remote.ssh_argv(
            "edge.safe",
            command="list",
            recent_seconds=172800,
        )

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
        self.assertTrue(recent_argv[-1].endswith("list --json --recent-seconds 172800"))
        with self.assertRaises(ValueError):
            remote.ssh_argv("edge; touch /tmp/pwned", command="status")
        with self.assertRaises(ValueError):
            remote.remote_shell_command("watch")

    def test_snapshot_builders_default_to_python3_and_shell_quote_custom_executable(self):
        default_command = remote.remote_shell_command("status")
        default_argv = remote.ssh_argv("edge", command="status")
        custom = "/opt/Python Builds/python3"
        custom_command = remote.remote_shell_command(
            "status",
            python_executable=custom,
        )
        custom_argv = remote.ssh_argv(
            "edge",
            command="status",
            python_executable=custom,
        )

        self.assertEqual("python3", shlex.split(default_command)[0])
        self.assertEqual("python3", shlex.split(default_argv[-1])[0])
        self.assertEqual(custom, shlex.split(custom_command)[0])
        self.assertEqual(custom, shlex.split(custom_argv[-1])[0])
        self.assertIn(shlex.quote(custom), custom_command)

    def test_recency_api_rejects_nonfinite_nonpositive_and_overbound_values(self):
        invalid = (
            0,
            -1,
            float("nan"),
            float("inf"),
            float("-inf"),
            remote.MAX_RECENT_SECONDS + 1,
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    remote.remote_shell_command("list", recent_seconds=value)
        with self.assertRaises(ValueError):
            remote.remote_shell_command("status", recent_seconds=1)

    def test_probe_candidates_and_shell_contract_are_frozen(self):
        expected_candidates = (
            "python3",
            "python3.14",
            "python3.13",
            "python3.12",
            "python3.11",
            "python3.10",
            "python3.9",
            "python3.8",
        )
        self.assertEqual((3, 8, 0), remote_transport.REMOTE_MIN_PYTHON)
        self.assertEqual(expected_candidates, remote_transport.REMOTE_PYTHON_CANDIDATES)
        self.assertNotIn("\n", remote_transport._PROBE_SCRIPT)
        self.assertIn("command -v", remote_transport._PROBE_SCRIPT)
        self.assertIn('"$@"', remote_transport._PROBE_SCRIPT)
        self.assertNotIn("output=$(", remote_transport._PROBE_SCRIPT)
        self.assertNotIn("$(", remote_transport._PROBE_SCRIPT)
        self.assertIn('"$candidate" -c', remote_transport._PROBE_SCRIPT)
        self.assertIn(
            "3>&1 1>/dev/null 2>/dev/null",
            remote_transport._PROBE_SCRIPT,
        )
        self.assertTrue(
            all(
                candidate not in remote_transport._PROBE_SCRIPT
                for candidate in expected_candidates
            )
        )
        self.assertNotIn("glob", remote_transport._PROBE_SCRIPT)
        self.assertNotIn("*", remote_transport._PROBE_SCRIPT)
        self.assertIn(
            repr(remote_transport.REMOTE_MIN_PYTHON),
            remote_transport._PROBE_CODE,
        )
        self.assertEqual(
            ("sh", "-c", remote_transport._PROBE_SCRIPT, "sh")
            + expected_candidates,
            tuple(shlex.split(remote_transport._PROBE_REMOTE_COMMAND)),
        )

    def test_explicit_probe_has_exact_one_element_argv_and_identical_script(self):
        explicit = "/opt/custom+python/python3.8"
        default_tokens = tuple(shlex.split(remote.ssh_argv("edge")[-1]))
        explicit_tokens = tuple(
            shlex.split(remote.ssh_argv("edge", candidates=(explicit,))[-1])
        )

        self.assertEqual(
            ("sh", "-c", remote_transport._PROBE_SCRIPT, "sh")
            + remote_transport.REMOTE_PYTHON_CANDIDATES,
            default_tokens,
        )
        self.assertEqual(
            ("sh", "-c", remote_transport._PROBE_SCRIPT, "sh", explicit),
            explicit_tokens,
        )
        self.assertEqual(
            default_tokens[2].encode("utf-8"),
            explicit_tokens[2].encode("utf-8"),
        )
        self.assertNotIn(explicit, remote_transport._PROBE_SCRIPT)
        self.assertTrue(
            all(
                candidate not in explicit_tokens[4:]
                for candidate in remote_transport.REMOTE_PYTHON_CANDIDATES
            )
        )

    @unittest.skipUnless(os.name == "posix", "probe shell requires POSIX")
    def test_probe_shell_skips_missing_broken_and_old_then_stops_at_first_hit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "calls"

            def write_candidate(name, body):
                path = root / name
                path.write_text(
                    "#!/bin/sh\n"
                    'printf "%s\\n" "$0" >> "$PROBE_CALLS"\n'
                    + body,
                    encoding="utf-8",
                )
                path.chmod(0o755)

            write_candidate(
                "python3",
                "printf 'noisy stdout\\n'\n"
                "printf 'noisy stderr\\n' >&2\n"
                "exit 1\n",
            )
            write_candidate("python3.14", "exit 127\n")
            write_candidate("python3.13", "exit 1\n")
            write_candidate(
                "python3.12",
                "exec {} \"$@\"\n".format(shlex.quote(sys.executable)),
            )
            write_candidate(
                "python3.11",
                "exec {} \"$@\"\n".format(shlex.quote(sys.executable)),
            )
            environment = {"PATH": str(root), "PROBE_CALLS": str(log)}

            result = subprocess.run(
                [
                    "/bin/sh",
                    "-c",
                    remote_transport._PROBE_SCRIPT,
                    "sh",
                    *remote_transport.REMOTE_PYTHON_CANDIDATES,
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=10,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                {
                    "python": list(sys.version_info[:3]),
                    "executable": sys.executable,
                },
                json.loads(result.stdout),
            )
            self.assertEqual(b"", result.stderr)
            self.assertEqual(
                ["python3", "python3.14", "python3.13", "python3.12"],
                [
                    Path(line).name
                    for line in log.read_text(encoding="utf-8").splitlines()
                ],
            )

            exhausted = subprocess.run(
                ["/bin/sh", "-c", remote_transport._PROBE_SCRIPT, "sh", "missing"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=3,
            )
            self.assertEqual(b'{"python":null}\n', exhausted.stdout)

    @unittest.skipUnless(os.name == "posix", "probe shell requires POSIX")
    def test_explicit_missing_nonexecutable_and_old_fail_closed_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls_path = root / "calls"
            fallback = root / "python3"
            fallback.write_text(
                "#!/bin/sh\n"
                'printf "fallback\\n" >> "$PROBE_CALLS"\n'
                "printf '%s\\n' "
                '\'{"python":[3,12,0],"executable":"/fallback/python3"}\' >&3\n',
                encoding="utf-8",
            )
            fallback.chmod(0o755)

            nonexecutable = root / "nonexecutable-python"
            nonexecutable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            nonexecutable.chmod(0o644)
            old = root / "old-python"
            old.write_text(
                "#!/bin/sh\n"
                'printf "old\\n" >> "$PROBE_CALLS"\n'
                "exit 1\n",
                encoding="utf-8",
            )
            old.chmod(0o755)
            missing = root / "missing-python"
            environment = {
                "PATH": "{}:/usr/bin:/bin".format(root),
                "PROBE_CALLS": str(calls_path),
            }

            for label, candidate, expected_calls in (
                ("missing", missing, []),
                ("nonexecutable", nonexecutable, []),
                ("old", old, ["old"]),
            ):
                with self.subTest(label=label):
                    calls_path.unlink(missing_ok=True)
                    runner_calls = []

                    def runner(argv, **kwargs):
                        del kwargs
                        runner_calls.append(tuple(argv))
                        return subprocess.run(
                            shlex.split(argv[-1]),
                            env=environment,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            check=False,
                            timeout=3,
                        )

                    hit, failure = remote.probe_remote_python(
                        remote.RemoteHost("edge", "ready"),
                        candidates=(str(candidate),),
                        runner=runner,
                    )

                    self.assertIsNone(hit)
                    self.assertEqual("python_too_old", failure.code)
                    self.assertEqual(1, len(runner_calls))
                    self.assertEqual(
                        (
                            "sh",
                            "-c",
                            remote_transport._PROBE_SCRIPT,
                            "sh",
                            str(candidate),
                        ),
                        tuple(shlex.split(runner_calls[0][-1])),
                    )
                    self.assertEqual(
                        expected_calls,
                        (
                            calls_path.read_text(encoding="utf-8").splitlines()
                            if calls_path.exists()
                            else []
                        ),
                    )

    def test_probe_success_and_exhaustion_have_named_results_and_one_ssh_call(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return probe_completed(argv, (3, 8, 0), "/opt/python3.8")

        hit, failure = remote.probe_remote_python(
            remote.RemoteHost("edge", "ready"),
            runner=runner,
        )

        self.assertEqual(
            remote_types.ProbeResult(
                version=(3, 8, 0),
                executable="/opt/python3.8",
            ),
            hit,
        )
        self.assertIsNone(failure)
        self.assertEqual(1, len(calls))
        self.assertEqual("ssh", calls[0][0][0])

        calls.clear()
        hit, failure = remote.probe_remote_python(
            remote.RemoteHost("edge", "ready"),
            runner=lambda argv, **kwargs: (
                calls.append((argv, kwargs)) or probe_exhausted(argv)
            ),
        )
        self.assertIsNone(hit)
        self.assertEqual("python_too_old", failure.code)
        self.assertEqual(1, len(calls))

    def test_snapshot_uses_explicit_candidate_verbatim_for_probe_and_bootstrap(self):
        pinned = "/opt/pinned/python3.8"
        resolved = "/resolved/python3.8"
        calls = []

        def runner(argv, **kwargs):
            calls.append((tuple(argv), kwargs))
            if len(calls) == 1:
                return probe_completed(argv, executable=resolved)
            return execution_completed(argv, [])

        rows, failure = remote.execute_remote_host(
            remote.RemoteHost("edge", "ready"),
            "status",
            b"zipapp",
            python_candidates=(pinned,),
            runner=runner,
        )

        self.assertEqual((), rows)
        self.assertIsNone(failure)
        self.assertEqual(2, len(calls))
        probe_tokens = tuple(shlex.split(calls[0][0][-1]))
        bootstrap_tokens = tuple(shlex.split(calls[1][0][-1]))
        self.assertEqual(
            ("sh", "-c", remote_transport._PROBE_SCRIPT, "sh", pinned),
            probe_tokens,
        )
        self.assertEqual(probe_tokens[-1], bootstrap_tokens[0])
        self.assertEqual(pinned, bootstrap_tokens[0])
        self.assertNotEqual(resolved, bootstrap_tokens[0])
        self.assertNotIn("python3", probe_tokens[4:])

    def test_explicit_snapshot_probe_response_is_validated_without_fallback(self):
        pinned = "/opt/pinned/python3.8"
        malformed = (
            {"python": [3, 8, 0], "executable": "relative/python3"},
            {"python": [3, 7, 999], "executable": "/resolved/python3"},
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                calls = []

                def runner(argv, **kwargs):
                    calls.append((tuple(argv), kwargs))
                    return json_completed(argv, payload)

                rows, failure = remote.execute_remote_host(
                    remote.RemoteHost("edge", "ready"),
                    "status",
                    b"zipapp",
                    python_candidates=(pinned,),
                    runner=runner,
                )

                self.assertIsNone(rows)
                self.assertEqual("protocol", failure.code)
                self.assertEqual(1, len(calls))
                self.assertEqual(
                    (pinned,),
                    tuple(shlex.split(calls[0][0][-1]))[4:],
                )

    def test_probe_response_validation_fails_closed(self):
        malformed = [
            {},
            {"python": None, "executable": "/usr/bin/python3"},
            {"python": [3, 8, 0]},
            {"python": [3, 8, 0], "executable": "/usr/bin/python3", "extra": 1},
            {"python": [3, 8], "executable": "/usr/bin/python3"},
            {"python": [3, 8, 0, 0], "executable": "/usr/bin/python3"},
            {"python": [True, 8, 0], "executable": "/usr/bin/python3"},
            {"python": [-1, 8, 0], "executable": "/usr/bin/python3"},
            {"python": [1000, 8, 0], "executable": "/usr/bin/python3"},
            {"python": [3, 7, 999], "executable": "/usr/bin/python3"},
            {"python": [3, 8, 0], "executable": ""},
            {"python": [3, 8, 0], "executable": "python3"},
            {"python": [3, 8, 0], "executable": "/" + "p" * 1024},
            {"python": [3, 8, 0], "executable": "/usr/bin/python 3"},
            {"python": [3, 8, 0], "executable": "/usr/../bin/python3"},
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                hit, failure = remote.probe_remote_python(
                    remote.RemoteHost("edge", "ready"),
                    runner=lambda argv, payload=payload, **kwargs: json_completed(
                        argv,
                        payload,
                    ),
                )
                self.assertIsNone(hit)
                self.assertEqual("protocol", failure.code)

        boundary = {
            "python": [999, 999, 999],
            "executable": "/" + "p" * 1023,
        }
        hit, failure = remote.probe_remote_python(
            remote.RemoteHost("edge", "ready"),
            runner=lambda argv, **kwargs: json_completed(argv, boundary),
        )
        self.assertIsNone(failure)
        self.assertEqual(tuple(boundary["python"]), hit.version)
        self.assertEqual(boundary["executable"], hit.executable)

    def test_probe_response_size_and_overflow_are_resource_limits(self):
        cases = (
            completed(("ssh",), stdout=b"x" * 4097),
            BoundedProcessResult(
                args=("ssh",),
                returncode=0,
                stdout=b"",
                stderr=b"",
                overflow="stdout",
            ),
        )
        for response in cases:
            with self.subTest(overflow=getattr(response, "overflow", None)):
                hit, failure = remote.probe_remote_python(
                    remote.RemoteHost("edge", "ready"),
                    runner=lambda argv, response=response, **kwargs: response,
                )
                self.assertIsNone(hit)
                self.assertEqual("resource_limit", failure.code)

    def test_python_38_snapshot_transfers_artifact(self):
        calls = []
        artifact = b"zipapp"

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if len(calls) == 1:
                return probe_completed(argv, (3, 8, 19))
            return execution_completed(argv, [])

        rows, failure = remote.execute_remote_host(
            remote.RemoteHost("edge", "ready"),
            "status",
            artifact,
            runner=runner,
        )

        self.assertEqual((), rows)
        self.assertIsNone(failure)
        self.assertEqual(2, len(calls))
        self.assertEqual(artifact, calls[1][1]["input"])

    def test_python_37_is_gated_before_artifact_transfer(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return probe_exhausted(argv)

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

    def test_snapshot_uses_discovered_executable_and_probes_each_call(self):
        calls = []
        executables = {
            "first": "/opt/first+python",
            "second": "/srv/second-python",
        }

        def runner(argv, **kwargs):
            alias = argv[-2]
            calls.append((alias, argv[-1], kwargs))
            host_calls = [call for call in calls if call[0] == alias]
            if len(host_calls) % 2 == 1:
                return probe_completed(argv, executable=executables[alias])
            return execution_completed(argv, [])

        for alias in ("first", "second", "first"):
            rows, failure = remote.execute_remote_host(
                remote.RemoteHost(alias, "ready"),
                "status",
                b"zipapp",
                runner=runner,
            )
            self.assertEqual((), rows)
            self.assertIsNone(failure)

        self.assertEqual(6, len(calls))
        for index, alias in enumerate(("first", "second", "first")):
            probe_call, execution_call = calls[index * 2 : index * 2 + 2]
            self.assertEqual(alias, probe_call[0])
            self.assertEqual(alias, execution_call[0])
            self.assertEqual(
                executables[alias],
                shlex.split(execution_call[1])[0],
            )

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

    def test_transport_overflow_distinguishes_resource_streams(self):
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
        self.assertEqual("resource_limit", failure.code)

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

    def test_probe_malformed_and_resource_failures_are_distinguished(self):
        payloads = (
            (b'Welcome\n{"python":[3,11,0]}\n', "protocol"),
            (b'{"python":[3,11,0]}\n\n', "protocol"),
            (b"x" * 4097, "resource_limit"),
            (b'{"python":["\\ud800",11,0]}', "protocol"),
            (
                ("[" * 500 + "0" + "]" * 500).encode("ascii"),
                "resource_limit",
            ),
        )
        for payload, expected_code in payloads:
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
                self.assertEqual(expected_code, failure.code)

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

    def test_list_recency_is_forwarded_as_fixed_child_arguments(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            if len(calls) == 1:
                return probe_completed(argv)
            return execution_completed(argv, [session_row()])

        rows, failure = remote.execute_remote_host(
            remote.RemoteHost("edge", "ready"),
            "list",
            b"zipapp",
            recent_seconds=172800,
            runner=runner,
        )

        self.assertIsNone(failure)
        self.assertEqual(1, len(rows))
        self.assertIn(
            "list --json --recent-seconds 172800",
            calls[1][0][-1],
        )

    def test_oversize_and_malformed_execution_responses_are_distinguished(self):
        payloads = (
            (b"x" * (remote.MAX_PROTOCOL_BYTES + 1), "resource_limit"),
            (
                json.dumps(
                    {
                        "ok": True,
                        "rows": [{"agent": "claude", "status": "waiting"}],
                    }
                ).encode("utf-8"),
                "protocol",
            ),
            (
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
                "protocol",
            ),
        )
        for payload, expected_code in payloads:
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
            self.assertEqual(expected_code, failure.code)

    def test_protocol_resource_and_malformed_failures_remain_distinct(self):
        over = minimal_session_row()
        over["title"] = "x" * (remote_types._SESSION_STRING_LIMITS["title"] + 1)
        payloads = (
            (
                json.dumps(
                    {"ok": True, "rows": [over]},
                    separators=(",", ":"),
                ).encode("utf-8"),
                "resource_limit",
            ),
            (
                b'{"ok":true,"ok":true,"rows":[]}',
                "protocol",
            ),
            (
                ("[" * 5000 + "0" + "]" * 5000).encode("ascii"),
                "resource_limit",
            ),
        )

        for payload, expected_code in payloads:
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
            self.assertEqual(expected_code, failure.code)

    def test_probe_recursion_exhaustion_is_resource_limit(self):
        payload = ("[" * 5000 + "0" + "]" * 5000).encode("ascii")

        rows, failure = remote.execute_remote_host(
            remote.RemoteHost("edge", "ready"),
            "status",
            b"zipapp",
            runner=lambda argv, **kwargs: completed(argv, stdout=payload),
        )

        self.assertIsNone(rows)
        self.assertEqual("resource_limit", failure.code)

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
            ("session_id", ""),
            ("project", 7),
            ("transcript", "\ud800"),
            ("updated_at", True),
            ("updated_at", float("nan")),
            ("updated_at", float("inf")),
            ("status", "unknown"),
            ("extra", []),
            ("parent_id", 1),
        )
        for key, value in invalid_values:
            row = session_row()
            row[key] = value
            cases.append(row)

        for row in cases:
            with self.subTest(row=row):
                with self.assertRaises(ValueError) as raised:
                    remote_types._validate_protocol_rows([row], "edge")
                self.assertNotIsInstance(
                    raised.exception,
                    remote.ProtocolResourceLimitError,
                )

    def test_bounded_session_fields_accept_limit_and_reject_limit_plus_one(self):
        for key, limit in remote_types._SESSION_STRING_LIMITS.items():
            with self.subTest(key=key):
                boundary = minimal_session_row()
                boundary[key] = "x" * limit
                remote_types._validate_protocol_rows([boundary], "edge")

                over = minimal_session_row()
                over[key] = "x" * (limit + 1)
                with self.assertRaises(remote.ProtocolResourceLimitError):
                    remote_types._validate_protocol_rows([over], "edge")

    def test_timestamp_extra_row_and_row_count_boundaries_are_typed(self):
        for value in (
            remote.MIN_SESSION_TIMESTAMP,
            remote.MAX_SESSION_TIMESTAMP,
        ):
            row = minimal_session_row()
            row["updated_at"] = value
            remote_types._validate_protocol_rows([row], "edge")
        for value in (
            remote.MIN_SESSION_TIMESTAMP - 1,
            remote.MAX_SESSION_TIMESTAMP + 1,
        ):
            row = minimal_session_row()
            row["updated_at"] = value
            with self.assertRaises(remote.ProtocolResourceLimitError):
                remote_types._validate_protocol_rows([row], "edge")

        boundary_items = remote_types.MAX_JSON_EXTRA_ITEMS - 3
        extra_boundary = minimal_session_row()
        extra_boundary["extra"] = {"items": [None] * boundary_items}
        remote_types._validate_protocol_rows([extra_boundary], "edge")
        extra_over = minimal_session_row()
        extra_over["extra"] = {"items": [None] * (boundary_items + 1)}
        with self.assertRaises(remote.ProtocolResourceLimitError):
            remote_types._validate_protocol_rows([extra_over], "edge")

        row = minimal_session_row()
        encoded_size = len(remote_types._encoded_row(row))
        with mock.patch.object(remote_types, "MAX_ROW_BYTES", encoded_size):
            remote_types._validate_protocol_rows([row], "edge")
        with mock.patch.object(remote_types, "MAX_ROW_BYTES", encoded_size - 1):
            with self.assertRaises(remote.ProtocolResourceLimitError):
                remote_types._validate_protocol_rows([row], "edge")

        with self.assertRaises(remote.ProtocolResourceLimitError):
            remote_types._validate_protocol_rows(
                [minimal_session_row()] * (remote.MAX_ROWS + 1),
                "edge",
            )

    def test_session_row_validation_accepts_datetime_safe_int_before_annotation(self):
        row = session_row()
        row["updated_at"] = remote.MAX_SESSION_TIMESTAMP

        rows = remote_types._validate_protocol_rows([row], "edge")

        self.assertEqual(remote.MAX_SESSION_TIMESTAMP, rows[0]["updated_at"])
        self.assertEqual("edge", rows[0]["host"])
        self.assertNotIn("host", row)

    def test_bootstrap_cleanup_and_child_contract_are_structural(self):
        bootstrap = remote.REMOTE_BOOTSTRAP
        self.assertIn("tempfile.mkstemp", bootstrap)
        self.assertIn("os.fchmod(fd, 0o600)", bootstrap)
        self.assertIn('[sys.executable, "-I", path] + child_args', bootstrap)
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

    def _run_bootstrap(
        self,
        root,
        source,
        *,
        child_timeout=None,
        child_args=None,
    ):
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
        args = ["status", "--json"] if child_args is None else list(child_args)
        return subprocess.run(
            [sys.executable, "-I", "-c", bootstrap] + args,
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
                        {
                            "ok": False,
                            "code": (
                                "resource_limit" if descriptor == 1 else "protocol"
                            ),
                        },
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
                '"import os,time; '
                "open(os.environ['GRANDCHILD_PID'],'w').write(str(os.getpid())); "
                'time.sleep(60)"])\n'
                "with open(os.environ['CHILD_PID'],'w') as stream:\n"
                "    stream.write(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            started = time.monotonic()

            # The child starts an interpreter, spawns a second one, and only
            # then records the pids this test kills by. A budget under an
            # interpreter start cuts it off before that and leaves nothing to
            # assert; the timeout path is still what ends the run.
            result = self._run_bootstrap(root, source, child_timeout=1.5)

            self.assertLess(time.monotonic() - started, 5.0)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                {"ok": False, "code": "timeout"},
                json.loads(result.stdout),
            )
            deadline = time.monotonic() + 5
            pid = read_pid_when_ready(root / "child.pid", deadline)
            grandchild_pid = read_pid_when_ready(root / "grandchild.pid", deadline)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline and (
                process_exists(pid) or process_exists(grandchild_pid)
            ):
                time.sleep(0.01)
            self.assertFalse(process_exists(pid))
            self.assertFalse(process_exists(grandchild_pid))
            self.assertEqual([], list(root.glob("*.pyz")))

    def test_bootstrap_maps_json_recursion_exhaustion_to_resource_limit(self):
        source = "print('[' * 5000 + '0' + ']' * 5000)"
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run_bootstrap(Path(temporary), source)

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            {"ok": False, "code": "resource_limit"},
            json.loads(result.stdout),
        )

    def test_bootstrap_accepts_only_bounded_list_recency_shape(self):
        source = "import json,sys;print(json.dumps(sys.argv[1:]))"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            accepted = self._run_bootstrap(
                root,
                source,
                child_args=("list", "--json", "--recent-seconds", "172800"),
            )
            rejected_values = ("0", "-1", "nan", "inf", "31536001")
            rejected = [
                self._run_bootstrap(
                    root,
                    source,
                    child_args=(
                        "list",
                        "--json",
                        "--recent-seconds",
                        value,
                    ),
                )
                for value in rejected_values
            ]

        self.assertEqual(
            {
                "ok": True,
                "rows": ["list", "--json", "--recent-seconds", "172800"],
            },
            json.loads(accepted.stdout),
        )
        self.assertTrue(
            all(
                json.loads(result.stdout) == {"ok": False, "code": "protocol"}
                for result in rejected
            )
        )


class BoundedTransportTests(unittest.TestCase):
    def _pid_path(self, root, name):
        return Path(root) / "{}.pid".format(name)

    def _read_pid(self, path):
        # The child may still be starting when the bounded call returns, so
        # wait for the pid it promised rather than reading an absent file and
        # failing on a precondition instead of on the reaping under test.
        return read_pid_when_ready(path, time.monotonic() + 5)

    @mock.patch.object(remote_transport, "_run_bounded")
    def test_remote_wrapper_keeps_thirty_second_timeout_ceiling(self, run_bounded):
        with self.assertRaises(ValueError):
            remote_transport._bounded_popen(
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
                    result = remote_transport._bounded_popen(
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
                remote_transport._bounded_popen(
                    [sys.executable, "-c", code, str(pid_path)],
                    stdout_limit=1024,
                    # Above an interpreter start, so the child reaches its
                    # sleep and the timeout is what ends the call.
                    timeout=1.5,
                )
            self.assertLess(time.monotonic() - started, 3.0)
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
                remote_transport._bounded_popen(
                    [sys.executable, "-c", code, str(pid_path)],
                    input_data=b"x" * remote.MAX_ARTIFACT_BYTES,
                    stdout_limit=1024,
                    timeout=1.5,
                )
            self.assertLess(time.monotonic() - started, 3.0)
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
                'code="import os,sys,time;'
                "open(sys.argv[1],'w').write(str(os.getpid()));"
                'time.sleep(60)"\n'
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
                child_pid = read_pid_when_ready(child_pid_path, deadline)
                descendant_pid = read_pid_when_ready(descendant_pid_path, deadline)

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

    def test_python38_shutdown_signature_preserves_normal_completion(self):
        future = TrackingFuture()
        future.set_result(((), None))
        executor = Python38StyleExecutor(future)

        with mock.patch.object(
            remote.concurrent.futures,
            "ThreadPoolExecutor",
            return_value=executor,
        ):
            result = remote.aggregate_remote(
                "status",
                hosts=(remote.RemoteHost("edge", "ready"),),
                artifact=b"zipapp",
            )

        self.assertEqual(("edge",), result.succeeded)
        self.assertEqual(0, future.cancel_calls)
        self.assertEqual([True], executor.shutdown_calls)
        self.assertFalse(executor.cancel_events[0].is_set())

    def test_aggregate_threads_one_explicit_candidate_to_every_snapshot_host(self):
        pinned = ("/opt/pinned/python3.8",)
        calls = []

        def execute(host, command, artifact, **kwargs):
            del command, artifact
            calls.append((host.alias, kwargs["python_candidates"]))
            return (), None

        with mock.patch.object(remote, "execute_remote_host", new=execute):
            result = remote.aggregate_remote(
                "status",
                hosts=(
                    remote.RemoteHost("first", "ready"),
                    remote.RemoteHost("second", "ready"),
                ),
                max_workers=1,
                python_candidates=pinned,
                artifact=b"zipapp",
            )

        self.assertEqual(("first", "second"), result.succeeded)
        self.assertEqual(
            [("first", pinned), ("second", pinned)],
            calls,
        )

    def test_python38_shutdown_signature_cancels_pending_work_on_deadline(self):
        running = TrackingFuture()
        self.assertTrue(running.set_running_or_notify_cancel())
        pending = TrackingFuture()
        executor = Python38StyleExecutor(running, pending)
        monotonic = iter((0.0, 0.0, 0.0, 1.0, 1.0)).__next__
        hosts = (
            remote.RemoteHost("running", "ready"),
            remote.RemoteHost("pending", "ready"),
        )

        with mock.patch.object(
            remote.concurrent.futures,
            "ThreadPoolExecutor",
            return_value=executor,
        ):
            result = remote.aggregate_remote(
                "status",
                hosts=hosts,
                max_workers=2,
                artifact=b"zipapp",
                fleet_timeout=0.1,
                monotonic=monotonic,
            )

        self.assertFalse(running.cancelled())
        self.assertTrue(pending.cancelled())
        self.assertEqual(1, running.cancel_calls)
        self.assertEqual(1, pending.cancel_calls)
        self.assertEqual([False], executor.shutdown_calls)
        self.assertTrue(all(event.is_set() for event in executor.cancel_events))
        self.assertEqual(
            [("pending", "timeout"), ("running", "timeout")],
            [(failure.host, failure.code) for failure in result.failures],
        )

    def test_python38_shutdown_signature_cancels_pending_work_on_exception(self):
        pending = TrackingFuture()
        executor = Python38StyleExecutor(pending)

        with mock.patch.object(
            remote.concurrent.futures,
            "ThreadPoolExecutor",
            return_value=executor,
        ):
            with mock.patch.object(
                remote.concurrent.futures,
                "wait",
                side_effect=RuntimeError("wait failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "wait failed"):
                    remote.aggregate_remote(
                        "status",
                        hosts=(remote.RemoteHost("edge", "ready"),),
                        artifact=b"zipapp",
                        monotonic=lambda: 0.0,
                    )

        self.assertTrue(pending.cancelled())
        self.assertEqual(1, pending.cancel_calls)
        self.assertEqual([True], executor.shutdown_calls)
        self.assertTrue(executor.cancel_events[0].is_set())

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

    def test_list_aggregation_uses_all_or_bounded_recency_protocol(self):
        execution_commands = []

        def runner(argv, **kwargs):
            del kwargs
            if "MAX_ARTIFACT" not in argv[-1]:
                return probe_completed(argv)
            execution_commands.append(argv[-1])
            return execution_completed(argv, [])

        for recent_seconds in (None, 172800):
            result = remote.aggregate_remote(
                "list",
                recent_seconds=recent_seconds,
                hosts=(remote.RemoteHost("edge", "ready"),),
                runner=runner,
                artifact=b"zipapp",
            )
            self.assertEqual(("edge",), result.succeeded)

        self.assertIn("list --json --all", execution_commands[0])
        self.assertIn(
            "list --json --recent-seconds 172800",
            execution_commands[1],
        )

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

    def test_empty_eligible_fleet_is_success_without_starting_transport(self):
        calls = []

        result = remote.aggregate_remote(
            "status",
            hosts=(),
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )

        self.assertEqual((), result.hosts)
        self.assertEqual((), result.succeeded)
        self.assertEqual((), result.failures)
        self.assertEqual((), result.rows)
        self.assertEqual(remote.EXIT_OK, result.exit_code)
        self.assertEqual([], calls)

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

    def test_sliding_window_does_not_let_one_slow_host_starve_later_hosts(self):
        hosts = (
            remote.RemoteHost("a-slow", "ready"),
            remote.RemoteHost("b-fast", "ready"),
            remote.RemoteHost("c-fast", "ready"),
            remote.RemoteHost("d-fast", "ready"),
        )
        attempted = set()
        lock = threading.Lock()
        later_hosts_finished = threading.Event()

        def runner(argv, **kwargs):
            alias = argv[-2]
            with lock:
                attempted.add(alias)
            if alias == "a-slow":
                later_hosts_finished.wait(kwargs["timeout"] + 0.005)
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            if "MAX_ARTIFACT" not in argv[-1]:
                return probe_completed(argv)
            if alias == "d-fast":
                later_hosts_finished.set()
            return execution_completed(argv, [session_row(alias)])

        result = remote.aggregate_remote(
            "status",
            hosts=hosts,
            max_workers=2,
            runner=runner,
            artifact=b"zipapp",
            fleet_timeout=0.2,
        )

        self.assertEqual(("b-fast", "c-fast", "d-fast"), result.succeeded)
        self.assertEqual(
            [("a-slow", "timeout")],
            [(failure.host, failure.code) for failure in result.failures],
        )
        self.assertEqual({host.alias for host in hosts}, attempted)
        self.assertEqual(
            ["b-fast", "c-fast", "d-fast"],
            [row["host"] for row in result.rows],
        )

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

        self.assertLess(elapsed, 0.5)
        self.assertEqual(20, len(result.failures))
        self.assertTrue(all(item.code == "timeout" for item in result.failures))
        self.assertEqual(
            sorted(host.alias for host in hosts),
            sorted(item.host for item in result.failures),
        )
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

    def test_aggregate_row_cap_uses_fleet_global_overflow_policy(self):
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
                [
                    session_row("{}-{}".format(argv[-2], index))
                    for index in range(count)
                ],
            )

        with mock.patch.object(remote, "MAX_ROWS", 2):
            result = remote.aggregate_remote(
                "status",
                hosts=hosts,
                max_workers=1,
                runner=runner,
                artifact=b"zipapp",
            )

        self.assertEqual((), result.succeeded)
        self.assertEqual((), result.rows)
        self.assertEqual(
            [
                ("a-small", "resource_limit"),
                ("b-large", "resource_limit"),
            ],
            [(failure.host, failure.code) for failure in result.failures],
        )

    def test_aggregate_byte_cap_uses_fleet_global_overflow_policy(self):
        hosts = (
            remote.RemoteHost("a-small", "ready"),
            remote.RemoteHost("b-over", "ready"),
        )
        first = session_row("a-small")
        first_annotated = dict(first)
        first_annotated["host"] = "a-small"
        exact_first_budget = len(remote_types._encoded_row(first_annotated)) + 3

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

        self.assertEqual((), result.succeeded)
        self.assertEqual((), result.rows)
        self.assertEqual(
            [
                ("a-small", "resource_limit"),
                ("b-over", "resource_limit"),
            ],
            [(failure.host, failure.code) for failure in result.failures],
        )

    def test_aggregate_caps_are_independent_of_completion_order(self):
        hosts = (
            remote.RemoteHost("a-edge", "ready"),
            remote.RemoteHost("b-edge", "ready"),
        )

        def run(delays, *, rows_by_host, row_cap, byte_cap):
            def runner(argv, **kwargs):
                del kwargs
                if "MAX_ARTIFACT" not in argv[-1]:
                    return probe_completed(argv)
                alias = argv[-2]
                time.sleep(delays[alias])
                return execution_completed(argv, rows_by_host[alias])

            with mock.patch.object(remote, "MAX_ROWS", row_cap):
                with mock.patch.object(
                    remote,
                    "MAX_AGGREGATE_BYTES",
                    byte_cap,
                ):
                    result = remote.aggregate_remote(
                        "status",
                        hosts=hosts,
                        max_workers=2,
                        runner=runner,
                        artifact=b"zipapp",
                    )
            return (
                result.rows,
                result.succeeded,
                tuple((failure.host, failure.code) for failure in result.failures),
            )

        timing_orders = (
            {"a-edge": 0.02, "b-edge": 0.001},
            {"a-edge": 0.001, "b-edge": 0.02},
        )
        row_sets = {
            "a-edge": [session_row("a-1"), session_row("a-2")],
            "b-edge": [session_row("b-1")],
        }
        row_results = [
            run(
                delays,
                rows_by_host=row_sets,
                row_cap=2,
                byte_cap=remote.MAX_AGGREGATE_BYTES,
            )
            for delays in timing_orders
        ]

        byte_sets = {
            "a-edge": [session_row("a")],
            "b-edge": [session_row("b")],
        }
        annotated_sizes = []
        for alias, values in byte_sets.items():
            annotated = dict(values[0], host=alias)
            annotated_sizes.append(len(remote_types._encoded_row(annotated)) + 1)
        byte_results = [
            run(
                delays,
                rows_by_host=byte_sets,
                row_cap=remote.MAX_ROWS,
                byte_cap=2 + max(annotated_sizes),
            )
            for delays in timing_orders
        ]

        self.assertEqual(row_results[0], row_results[1])
        self.assertEqual(byte_results[0], byte_results[1])
        for result in (row_results[0], byte_results[0]):
            self.assertEqual((), result[0])
            self.assertEqual((), result[1])
            self.assertEqual(
                (
                    ("a-edge", "resource_limit"),
                    ("b-edge", "resource_limit"),
                ),
                result[2],
            )


if __name__ == "__main__":
    unittest.main()
