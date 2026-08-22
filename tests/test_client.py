import unittest

from sidecar.client import SidecarClient


class StubClient(SidecarClient):
    def __init__(self, response):
        super().__init__(socket_path="/tmp/unused-agent-sidecar.sock")
        self.response = response

    def _request(self, operation):
        self.asserted_operation = operation
        return self.response


class SidecarClientTests(unittest.TestCase):
    def test_status_preserves_list_result_and_exposes_scan_errors(self):
        sessions = [{"agent": "claude", "session_id": "one"}]
        scan_errors = [
            {
                "adapter": "broken",
                "stage": "discover",
                "message": "unreadable",
                "exception_type": "OSError",
                "session_id": None,
            }
        ]
        client = StubClient(
            {
                "ok": True,
                "op": "status",
                "sessions": sessions,
                "scan_errors": scan_errors,
            }
        )

        self.assertEqual(sessions, client.status())
        self.assertEqual("status", client.asserted_operation)
        self.assertEqual(scan_errors, client.scan_errors)

        exposed = client.scan_errors
        exposed[0]["message"] = "changed"
        self.assertEqual("unreadable", client.scan_errors[0]["message"])
        with self.assertRaises(AttributeError):
            client.scan_errors = []

    def test_status_without_scan_errors_supports_older_daemon_and_clears_latest(self):
        client = StubClient(
            {
                "ok": True,
                "op": "status",
                "sessions": [],
                "scan_errors": [{"message": "old"}],
            }
        )
        client.status()
        client.response = {"ok": True, "op": "status", "sessions": []}

        self.assertEqual([], client.status())
        self.assertEqual([], client.scan_errors)


if __name__ == "__main__":
    unittest.main()
