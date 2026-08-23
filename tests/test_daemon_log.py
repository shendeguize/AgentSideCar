import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from sidecar.daemon_log import (
    DaemonLog,
    DaemonLogError,
    LOCK_NAME,
    LOG_NAME,
    MAX_LINE_BYTES,
)


class DaemonLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name) / "runtime"
        self.runtime.mkdir(mode=0o700)
        os.chmod(str(self.runtime), 0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def _records(self):
        records = []
        for index in (2, 1, 0):
            suffix = "" if index == 0 else ".{}".format(index)
            path = self.runtime / "{}{}".format(LOG_NAME, suffix)
            if not path.exists():
                continue
            for line in path.read_bytes().splitlines():
                records.append(json.loads(line))
        return records

    def test_schema_is_allowlisted_bounded_and_privacy_safe(self):
        canary = "PRIVATE_path_token_message_cookie_HMAC"
        logger = DaemonLog(self.runtime, version="0.4.0").open()
        try:
            self.assertTrue(
                logger.append(
                    "tail_error",
                    component="tailer",
                    level="error",
                    agent="cursor-cli",
                    session_id="/private/session/" + canary,
                    code="ReadError",
                    stage="poll",
                    durable=True,
                    path="/private/" + canary,
                    title=canary,
                    text=canary,
                    transcript=canary,
                    project=canary,
                    exception=canary,
                    token=canary,
                    cookie=canary,
                    auth=canary,
                    env=canary,
                    ssh_alias=canary,
                    stderr=canary,
                    request=canary,
                    message=canary,
                    hmac=canary,
                )
            )
        finally:
            logger.close()

        raw = (self.runtime / LOG_NAME).read_bytes()
        self.assertNotIn(canary.encode("ascii"), raw)
        self.assertLessEqual(max(len(line) + 1 for line in raw.splitlines()), MAX_LINE_BYTES)
        record = json.loads(raw)
        self.assertEqual(
            {
                "agent",
                "code",
                "component",
                "event",
                "level",
                "pid",
                "schema_version",
                "session_id",
                "stage",
                "ts",
                "ts_epoch",
                "version",
            },
            set(record),
        )
        self.assertRegex(record["session_id"], r"^sha256:[0-9a-f]{16}$")
        self.assertTrue(record["ts"].endswith("Z"))
        self.assertIsInstance(record["ts_epoch"], float)

    def test_rotation_keeps_exact_current_and_total_bounds(self):
        maximum = 640
        logger = DaemonLog(
            self.runtime,
            version="0.4.0",
            max_bytes=maximum,
            backups=2,
        ).open()
        try:
            for index in range(80):
                self.assertTrue(
                    logger.append(
                        "scan_error",
                        component="scanner",
                        level="error",
                        adapter="cursor",
                        stage="discover",
                        code="ReadError",
                        count=index,
                    )
                )
        finally:
            logger.close()

        paths = [
            self.runtime / LOG_NAME,
            self.runtime / (LOG_NAME + ".1"),
            self.runtime / (LOG_NAME + ".2"),
        ]
        self.assertTrue(all(path.is_file() for path in paths))
        self.assertFalse((self.runtime / (LOG_NAME + ".3")).exists())
        self.assertTrue(all(path.stat().st_size <= maximum for path in paths))
        self.assertLessEqual(sum(path.stat().st_size for path in paths), maximum * 3)
        self.assertTrue(self._records())
        for path in paths:
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            for line in path.read_bytes().splitlines():
                json.loads(line)

    def test_concurrent_threads_append_complete_json_lines(self):
        logger = DaemonLog(self.runtime, version="0.4.0").open()
        workers = 8
        per_worker = 40

        def append(worker):
            for offset in range(per_worker):
                self.assertTrue(
                    logger.append(
                        "heartbeat",
                        component="daemon",
                        count=worker * per_worker + offset,
                    )
                )

        threads = [
            threading.Thread(target=append, args=(worker,))
            for worker in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
        logger.close()

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        records = self._records()
        self.assertEqual(workers * per_worker, len(records))
        self.assertEqual(
            set(range(workers * per_worker)),
            {record["count"] for record in records},
        )

    def test_symlink_wrong_mode_and_owner_fail_closed(self):
        target = self.runtime.parent / "target"
        target.write_text("", encoding="utf-8")
        os.chmod(str(target), 0o600)
        (self.runtime / LOG_NAME).symlink_to(target)
        with self.assertRaisesRegex(DaemonLogError, "unsafe_log"):
            DaemonLog(self.runtime).open()
        (self.runtime / LOG_NAME).unlink()

        current = self.runtime / LOG_NAME
        current.write_text("", encoding="utf-8")
        os.chmod(str(current), 0o644)
        with self.assertRaisesRegex(DaemonLogError, "unsafe_log"):
            DaemonLog(self.runtime).open()
        current.unlink()

        with mock.patch(
            "sidecar.daemon_log.os.geteuid",
            return_value=os.geteuid() + 1,
        ):
            with self.assertRaisesRegex(DaemonLogError, "unsafe_runtime"):
                DaemonLog(self.runtime).open()

    def test_lifetime_lock_prevents_rotation_races_and_is_released(self):
        first = DaemonLog(self.runtime).open()
        second = DaemonLog(self.runtime)
        with self.assertRaisesRegex(DaemonLogError, "log_locked"):
            second.open()
        first.close()

        replacement = DaemonLog(self.runtime).open()
        descriptors = (
            replacement._directory_fd,
            replacement._lock_fd,
            replacement._log_fd,
        )
        replacement.close()
        self.assertEqual(0o600, stat.S_IMODE((self.runtime / LOCK_NAME).stat().st_mode))
        for descriptor in descriptors:
            self.assertIsNotNone(descriptor)
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_disk_failure_disables_once_without_recursive_writes(self):
        logger = DaemonLog(self.runtime).open()
        self.assertTrue(logger.append("startup", durable=True))
        path = self.runtime / LOG_NAME
        before = path.read_bytes()
        real_write = os.write
        writes = 0

        def partial_then_full(fd, payload):
            nonlocal writes
            writes += 1
            if writes == 1:
                return real_write(fd, payload[:7])
            raise OSError(errno.ENOSPC, "private disk detail")

        with (
            mock.patch(
                "sidecar.daemon_log.os.write",
                side_effect=partial_then_full,
            ) as write,
            mock.patch(
                "sidecar.daemon_log.os.fsync",
                wraps=os.fsync,
            ) as fsync,
        ):
            self.assertFalse(logger.append("ready"))
            self.assertFalse(logger.append("shutdown", durable=True))
        self.assertTrue(logger.disabled)
        self.assertEqual("log_write_failed", logger.error_code)
        self.assertEqual(2, write.call_count)
        self.assertGreaterEqual(fsync.call_count, 1)
        self.assertEqual(before, path.read_bytes())
        for line in path.read_bytes().splitlines():
            json.loads(line)
        logger.close()

    def test_open_repairs_only_bounded_final_crash_suffix(self):
        path = self.runtime / LOG_NAME
        first = DaemonLog(self.runtime).open()
        self.assertTrue(first.append("startup", durable=True))
        first.close()
        valid = path.read_bytes()
        with path.open("ab") as output:
            output.write(b'{"schema_version":1,"partial"')

        repaired = DaemonLog(self.runtime).open()
        self.assertEqual(valid, path.read_bytes())
        self.assertTrue(repaired.append("ready", durable=True))
        repaired.close()

        lines = path.read_bytes().splitlines()
        self.assertEqual(["startup", "ready"], [json.loads(line)["event"] for line in lines])

    def test_invalid_complete_interior_line_fails_without_truncating(self):
        path = self.runtime / LOG_NAME
        first = DaemonLog(self.runtime).open()
        self.assertTrue(first.append("startup", durable=True))
        first.close()
        with path.open("ab") as output:
            output.write(b"not-json\n")
            output.write(b'{"partial"')
        corrupt = path.read_bytes()

        with self.assertRaisesRegex(DaemonLogError, "invalid_log"):
            DaemonLog(self.runtime).open()
        self.assertEqual(corrupt, path.read_bytes())

    def test_missing_fcntl_import_is_safe_and_disables_logger(self):
        code = r"""
import builtins
import os
import tempfile
from pathlib import Path

real_import = builtins.__import__
def without_fcntl(name, *args, **kwargs):
    if name == "fcntl":
        raise ImportError("not available")
    return real_import(name, *args, **kwargs)
builtins.__import__ = without_fcntl

from sidecar import cli
from sidecar.daemon import SidecarDaemon
from sidecar.daemon_log import DaemonLog, DaemonLogError

with tempfile.TemporaryDirectory() as temporary:
    runtime = Path(temporary) / "runtime"
    runtime.mkdir(mode=0o700)
    os.chmod(str(runtime), 0o700)
    try:
        DaemonLog(runtime).open()
    except DaemonLogError as error:
        print(error.code)
    else:
        raise SystemExit("logger unexpectedly opened")
print(SidecarDaemon.__name__)
print(callable(cli.main))
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            ["unsupported_platform", "SidecarDaemon", "True"],
            result.stdout.splitlines(),
        )


if __name__ == "__main__":
    unittest.main()
