import tempfile
import unittest
from pathlib import Path

from sidecar.adapters.base import Adapter
from sidecar.model import Session, Status
from sidecar.scan import Scanner


class FakeAdapter(Adapter):
    name = "fake"
    agent_names = ("fake",)

    def __init__(self, sessions=(), error=None, status=Status.WAITING):
        self.sessions = list(sessions)
        self.error = error
        self.status = status

    def discover(self, home):
        if self.error is not None:
            raise self.error
        return list(self.sessions)

    def normalize(self, record, session):
        return []

    def infer_status(self, session, now=None):
        return self.status


class InferErrorAdapter(FakeAdapter):
    def infer_status(self, session, now=None):
        raise RuntimeError("bad status hint")


def make_session(agent, session_id, updated_at, title=""):
    return Session(
        agent=agent,
        session_id=session_id,
        project="/tmp/project",
        transcript="",
        updated_at=updated_at,
        title=title,
    )


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_adapter_discovery_exception_does_not_break_other_adapters(self):
        broken = FakeAdapter(error=RuntimeError("broken store"))
        broken.name = "broken"
        healthy = FakeAdapter([make_session("healthy", "one", 90)])
        healthy.name = "healthy"
        scanner = Scanner([broken, healthy], home=self.home)

        sessions = scanner.scan(now=100)

        self.assertEqual(["one"], [session.session_id for session in sessions])
        self.assertEqual(Status.WAITING, sessions[0].status)
        self.assertEqual(1, len(scanner.errors))
        self.assertEqual("broken", scanner.errors[0].adapter)
        self.assertEqual("discover", scanner.errors[0].stage)

    def test_sorts_descending_and_deduplicates_exact_identity(self):
        older = make_session("claude", "same", 10, title="older")
        newer = make_session("claude", "same", 30, title="newer")
        other_agent = make_session("cursor-ide", "same", 20, title="other")
        newest = make_session("claude", "newest", 40, title="newest")
        adapter = FakeAdapter([older, other_agent, newest, newer])

        sessions = Scanner([adapter], home=self.home).scan(now=50)

        self.assertEqual(
            [
                ("claude", "newest", "newest"),
                ("claude", "same", "newer"),
                ("cursor-ide", "same", "other"),
            ],
            [(item.agent, item.session_id, item.title) for item in sessions],
        )

    def test_status_hint_exception_is_collected_and_falls_back(self):
        broken_hint = InferErrorAdapter([make_session("broken-hint", "one", 90)])
        broken_hint.name = "broken-hint"
        healthy = FakeAdapter([make_session("healthy", "two", 95)])
        scanner = Scanner([broken_hint, healthy], home=self.home)

        sessions = scanner.scan(now=100)

        self.assertEqual(["two", "one"], [session.session_id for session in sessions])
        self.assertEqual(Status.DEAD, sessions[1].status)
        self.assertEqual(1, len(scanner.errors))
        self.assertEqual("infer_status", scanner.errors[0].stage)
        self.assertEqual("one", scanner.errors[0].session_id)

    def test_recent_filter_uses_call_time(self):
        adapter = FakeAdapter(
            [
                make_session("fake", "recent", 95),
                make_session("fake", "old", 80),
            ]
        )
        scanner = Scanner([adapter], home=self.home)

        first = scanner.scan(recent_seconds=10, now=100)
        second = scanner.scan(recent_seconds=10, now=110)

        self.assertEqual(["recent"], [session.session_id for session in first])
        self.assertEqual([], second)

if __name__ == "__main__":
    unittest.main()
