import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sidecar.adapters.base import Adapter
from sidecar.bus import EventBus
from sidecar.model import Event, Session, Status
from sidecar.tail import SessionTailer
from sidecar.tailer_pool import TailerPool


class FakeAdapter(Adapter):
    name = "fake"
    agent_names = ("fake",)

    def discover(self, home):
        del home
        return []

    def normalize(self, record, session):
        text = record.get("text")
        if not isinstance(text, str):
            return []
        return [
            Event(
                "2026-08-23T04:00:00+08:00",
                session.agent,
                session.session_id,
                str(record.get("type") or "message"),
                text,
            )
        ]


class PagedDSHAdapter(FakeAdapter):
    name = "dsh"
    agent_names = ("dsh",)

    def __init__(self, records=()):
        self.records = list(records)
        self.after_seqs = []

    def replay(self, session, after_seq=None, max_records=256):
        del session
        self.after_seqs.append(after_seq)
        return [
            record
            for record in self.records
            if after_seq is None or record["seq"] > after_seq
        ][:max_records]


def make_session(
    transcript,
    *,
    session_id="one",
    updated_at=None,
    status=Status.WORKING,
    agent="fake",
    extra=None,
):
    return Session(
        agent=agent,
        session_id=session_id,
        project=str(transcript.parent),
        transcript=str(transcript),
        updated_at=time.time() if updated_at is None else updated_at,
        title="fake session",
        status=status,
        extra={} if extra is None else dict(extra),
    )


class RecordingTailer:
    has_pending_records = False

    def __init__(self, session):
        self.session = session

    def poll(self):
        return []


class SinglePollTailer:
    single_poll_per_refresh = True

    def __init__(self, session):
        self.session = session
        self.poll_calls = 0
        self.has_pending_records = True

    def poll(self):
        self.poll_calls += 1
        if self.poll_calls == 1:
            return []
        self.has_pending_records = False
        return [
            Event(
                "2026-08-23T04:00:00+08:00",
                self.session.agent,
                self.session.session_id,
                "assistant",
                "second page",
            )
        ]


class StatefulCursorFollower:
    records = []
    instances = []

    def __init__(self, path, from_start=False, max_records=256):
        self.path = Path(path).resolve()
        self.from_start = from_start
        self.max_records = max_records
        self.position = 0
        self.initialized = False
        self.last_error = None
        self.poll_calls = 0
        self.__class__.instances.append(self)

    @property
    def has_pending_records(self):
        return self.initialized and self.position < len(self.records)

    def poll(self):
        self.poll_calls += 1
        if not self.initialized:
            self.initialized = True
            if not self.from_start:
                self.position = len(self.records)
                return []
        end = min(len(self.records), self.position + self.max_records)
        page = [dict(record) for record in self.records[self.position:end]]
        self.position = end
        return page

    def export_checkpoint(self):
        return {
            "version": 1,
            "kind": "cursor_chat",
            "path": str(self.path),
            "position": self.position,
            "initialized": self.initialized,
        }

    def restore_checkpoint(self, checkpoint):
        if (
            checkpoint.get("version") != 1
            or checkpoint.get("kind") != "cursor_chat"
            or checkpoint.get("path") != str(self.path)
            or not isinstance(checkpoint.get("position"), int)
            or not isinstance(checkpoint.get("initialized"), bool)
        ):
            return False
        self.position = checkpoint["position"]
        self.initialized = checkpoint["initialized"]
        return True


class TailerPoolPolicyTests(unittest.TestCase):
    def test_policy_selects_supported_recent_or_active_sessions(self):
        now = 10_000.0
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = [
                make_session(
                    root / "old-idle.jsonl",
                    session_id="old-idle",
                    updated_at=now - 1_000,
                    status=Status.IDLE,
                ),
                make_session(
                    root / "recent-dead.jsonl",
                    session_id="recent-dead",
                    updated_at=now - 10,
                    status=Status.DEAD,
                ),
                make_session(
                    root / "old-working.jsonl",
                    session_id="old-working",
                    updated_at=now - 10_000,
                ),
                make_session(
                    root / "old-waiting.jsonl",
                    session_id="old-waiting",
                    updated_at=now - 10_000,
                    status=Status.WAITING,
                ),
                make_session(
                    root / "unsupported.log",
                    session_id="unsupported",
                ),
            ]
            pool = TailerPool(
                lambda event: None,
                tail_recent_seconds=60,
                tailer_factory=RecordingTailer,
            )

            pool.refresh(
                sessions,
                changed_keys={
                    (session.agent, session.session_id) for session in sessions
                },
                initial=True,
                now=now,
            )

            self.assertEqual(
                {
                    ("fake", "recent-dead"),
                    ("fake", "old-working"),
                    ("fake", "old-waiting"),
                },
                pool.state.active,
            )

    def test_cursor_kind_requires_database_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cursor = make_session(
                root / "store.db",
                agent="cursor-cli",
                extra={"transcript_kind": "cursor-chat-sqlite"},
            )
            arbitrary_db = make_session(root / "other.db", agent="cursor-cli")
            wrong_shape = make_session(
                root / "events.jsonl",
                agent="cursor-cli",
                extra={"transcript_kind": "cursor-chat-sqlite"},
            )

            self.assertTrue(TailerPool.supports_tailing(cursor))
            self.assertFalse(TailerPool.supports_tailing(arbitrary_db))
            self.assertFalse(TailerPool.supports_tailing(wrong_shape))

    def test_pool_accepts_event_bus_and_callable_publishers(self):
        bus = EventBus()
        bus_pool = TailerPool(bus)
        callable_pool = TailerPool(lambda event: None)

        self.assertEqual(frozenset(), bus_pool.state.active)
        self.assertEqual(frozenset(), callable_pool.state.active)
        with self.assertRaises(TypeError):
            TailerPool(object())


class TailerPoolCheckpointTests(unittest.TestCase):
    def test_startup_expired_jsonl_resumes_after_initial_eof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "events.jsonl"
            transcript.write_text(
                '{"type":"old","text":"history"}\n',
                encoding="utf-8",
            )
            adapter = FakeAdapter()
            events = []
            from_starts = []

            def factory(session, from_start=False):
                from_starts.append(from_start)
                return SessionTailer(
                    session,
                    adapter=adapter,
                    from_start=from_start,
                )

            pool = TailerPool(
                events.append,
                tail_recent_seconds=100,
                tailer_factory=factory,
            )
            key = ("fake", "one")
            expired = make_session(
                transcript,
                updated_at=800.0,
                status=Status.IDLE,
            )

            pool.refresh(
                [expired],
                changed_keys={key},
                initial=True,
                now=1_000.0,
            )
            self.assertEqual(frozenset(), pool.state.active)
            self.assertEqual({key}, pool.state.checkpoints)

            with transcript.open("a", encoding="utf-8") as output:
                output.write('{"type":"assistant","text":"startup gap"}\n')
            resumed = make_session(transcript, updated_at=1_001.0)
            pool.refresh(
                [resumed],
                changed_keys={key},
                now=1_001.0,
            )

            self.assertEqual(["startup gap"], [event.text for event in events])
            self.assertEqual([False, False], from_starts)
            self.assertEqual(frozenset(), pool.state.checkpoints)

    def test_expired_indexed_jsonl_retains_cursor_until_real_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "events.jsonl"
            transcript.write_text('{"text":"history"}\n', encoding="utf-8")
            adapter = FakeAdapter()
            events = []
            pool = TailerPool(
                events.append,
                tail_recent_seconds=100,
                tailer_factory=lambda session, from_start=False: SessionTailer(
                    session,
                    adapter=adapter,
                    from_start=from_start,
                ),
            )
            key = ("fake", "one")
            session = make_session(
                transcript,
                updated_at=950.0,
                status=Status.IDLE,
            )

            pool.refresh(
                [session],
                changed_keys={key},
                initial=True,
                now=1_000.0,
            )
            pool.refresh([session], changed_keys=set(), now=1_051.0)
            self.assertEqual(frozenset(), pool.state.active)
            self.assertEqual({key}, pool.state.checkpoints)

            with transcript.open("a", encoding="utf-8") as output:
                output.write('{"type":"assistant","text":"while inactive"}\n')
            resumed = make_session(transcript, updated_at=1_052.0)
            pool.refresh(
                [resumed],
                changed_keys={key},
                now=1_052.0,
            )
            self.assertEqual(
                ["while inactive"],
                [event.text for event in events],
            )

            pool.refresh([], changed_keys=set(), now=1_053.0)
            self.assertEqual(frozenset(), pool.state.known)
            self.assertEqual(frozenset(), pool.state.checkpoints)

    def test_startup_expired_dsh_resumes_from_sequence_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "session.jsonl.zstd"
            adapter = PagedDSHAdapter(
                [
                    {"seq": 1, "type": "ignored"},
                    {"seq": 2, "type": "ignored"},
                ]
            )
            events = []
            pool = TailerPool(
                events.append,
                tail_recent_seconds=100,
                tailer_factory=lambda session, from_start=False: SessionTailer(
                    session,
                    adapter=adapter,
                    from_start=from_start,
                ),
            )
            key = ("dsh", "one")
            expired = make_session(
                transcript,
                updated_at=800.0,
                status=Status.IDLE,
                agent="dsh",
            )
            expired.extra["seq"] = 2

            pool.refresh(
                [expired],
                changed_keys={key},
                initial=True,
                now=1_000.0,
            )
            self.assertEqual({key}, pool.state.checkpoints)
            self.assertEqual([], adapter.after_seqs)

            adapter.records.append(
                {"seq": 3, "type": "assistant", "text": "startup gap"}
            )
            resumed = make_session(
                transcript,
                updated_at=1_001.0,
                agent="dsh",
            )
            resumed.extra["seq"] = 3
            pool.refresh(
                [resumed],
                changed_keys={key},
                now=1_001.0,
            )

            self.assertEqual(["startup gap"], [event.text for event in events])
            self.assertEqual([2], adapter.after_seqs)

    def test_startup_expired_cursor_anchors_root_and_emits_only_append(self):
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "store.db"
            transcript.write_bytes(b"fake cursor store")
            key = ("cursor-cli", "one")
            expired = make_session(
                transcript,
                updated_at=800.0,
                status=Status.IDLE,
                agent="cursor-cli",
                extra={"transcript_kind": "cursor-chat-sqlite"},
            )
            events = []
            StatefulCursorFollower.records = [
                {"role": "assistant", "content": "history"}
            ]
            StatefulCursorFollower.instances = []

            with mock.patch(
                "sidecar.tail.CursorChatFollower",
                StatefulCursorFollower,
            ):
                pool = TailerPool(
                    events.append,
                    tail_recent_seconds=100,
                )
                pool.refresh(
                    [expired],
                    changed_keys={key},
                    initial=True,
                    now=1_000.0,
                )

                self.assertEqual({key}, pool.state.checkpoints)
                self.assertEqual(
                    1,
                    StatefulCursorFollower.instances[0].poll_calls,
                )

                StatefulCursorFollower.records.append(
                    {"role": "assistant", "content": "startup gap"}
                )
                resumed = make_session(
                    transcript,
                    updated_at=1_001.0,
                    agent="cursor-cli",
                    extra={"transcript_kind": "cursor-chat-sqlite"},
                )
                pool.refresh(
                    [resumed],
                    changed_keys={key},
                    now=1_001.0,
                )

            self.assertEqual(["startup gap"], [event.text for event in events])
            self.assertEqual(2, len(StatefulCursorFollower.instances))
            self.assertEqual(
                1,
                StatefulCursorFollower.instances[1].poll_calls,
            )
            self.assertEqual(frozenset(), pool.state.checkpoints)

    def test_expired_indexed_dsh_resumes_from_last_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "session.jsonl.zstd"
            adapter = PagedDSHAdapter(
                [
                    {"seq": 1, "type": "ignored"},
                    {"seq": 2, "type": "ignored"},
                ]
            )
            events = []
            pool = TailerPool(
                events.append,
                tail_recent_seconds=100,
                tailer_factory=lambda session, from_start=False: SessionTailer(
                    session,
                    adapter=adapter,
                    from_start=from_start,
                ),
            )
            key = ("dsh", "one")
            session = make_session(
                transcript,
                updated_at=950.0,
                status=Status.IDLE,
                agent="dsh",
            )
            session.extra["seq"] = 2
            pool.refresh(
                [session],
                changed_keys={key},
                initial=True,
                now=1_000.0,
            )
            pool.refresh([session], changed_keys=set(), now=1_051.0)
            self.assertEqual({key}, pool.state.checkpoints)

            adapter.records.append(
                {"seq": 3, "type": "assistant", "text": "while inactive"}
            )
            resumed = make_session(
                transcript,
                updated_at=1_052.0,
                agent="dsh",
            )
            resumed.extra["seq"] = 3
            pool.refresh(
                [resumed],
                changed_keys={key},
                now=1_052.0,
            )

            self.assertEqual(
                ["while inactive"],
                [event.text for event in events],
            )
            self.assertEqual([2], adapter.after_seqs)

    def test_transcript_path_change_discards_incompatible_checkpoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            first.write_text('{"text":"first history"}\n', encoding="utf-8")
            second.write_text('{"text":"second history"}\n', encoding="utf-8")
            adapter = FakeAdapter()
            events = []
            from_starts = []

            def factory(session, from_start=False):
                from_starts.append(from_start)
                return SessionTailer(
                    session,
                    adapter=adapter,
                    from_start=from_start,
                )

            pool = TailerPool(
                events.append,
                tail_recent_seconds=100,
                tailer_factory=factory,
            )
            key = ("fake", "one")
            old = make_session(
                first,
                updated_at=950.0,
                status=Status.IDLE,
            )
            pool.refresh(
                [old],
                changed_keys={key},
                initial=True,
                now=1_000.0,
            )
            pool.refresh([old], changed_keys=set(), now=1_051.0)
            self.assertEqual({key}, pool.state.checkpoints)

            moved = make_session(second, updated_at=1_052.0)
            pool.refresh(
                [moved],
                changed_keys={key},
                now=1_052.0,
            )
            self.assertEqual([], events)
            self.assertEqual([False, False], from_starts)

            with second.open("a", encoding="utf-8") as output:
                output.write('{"text":"after move"}\n')
            pool.refresh(
                [moved],
                changed_keys={key},
                now=1_053.0,
            )
            self.assertEqual(["after move"], [event.text for event in events])


class TailerPoolPollingTests(unittest.TestCase):
    def test_jsonl_poll_drains_pending_records_within_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "events.jsonl"
            transcript.write_text('{"text":"history"}\n', encoding="utf-8")
            bus = EventBus()
            subscription = bus.subscribe()
            adapter = FakeAdapter()
            pool = TailerPool(
                bus,
                max_event_polls=3,
                tailer_factory=lambda session, from_start=False: SessionTailer(
                    session,
                    adapter=adapter,
                    from_start=from_start,
                    max_records=256,
                ),
            )
            session = make_session(transcript)
            key = ("fake", "one")
            pool.refresh(
                [session],
                changed_keys={key},
                initial=True,
            )

            with transcript.open("a", encoding="utf-8") as output:
                for _ in range(256):
                    output.write('{"type":"ignored"}\n')
                output.write(
                    '{"type":"assistant","text":"after ignored rows"}\n'
                )
            pool.refresh([session], changed_keys={key})

            self.assertEqual(
                "after ignored rows",
                subscription.get(timeout=0.1)["text"],
            )
            self.assertEqual(frozenset(), pool.state.pending)
            subscription.close()

    def test_dsh_polls_at_most_once_per_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            transcript = root / "session.jsonl.zstd"
            transcript.write_bytes(b"compressed frames")
            adapter = PagedDSHAdapter(
                [
                    {"seq": 1, "type": "ignored"},
                    {"seq": 2, "type": "ignored"},
                    {
                        "seq": 3,
                        "type": "assistant",
                        "text": "after empty page",
                    },
                ]
            )
            events = []
            pool = TailerPool(
                events.append,
                max_event_polls=8,
                tailer_factory=lambda session, from_start=False: SessionTailer(
                    session,
                    adapter=adapter,
                    from_start=from_start,
                    max_records=2,
                ),
            )
            session = make_session(transcript, agent="dsh")
            session.extra["seq"] = 0
            key = ("dsh", "one")
            pool.refresh(
                [session],
                changed_keys={key},
                initial=True,
            )

            pool.refresh([session], changed_keys={key})
            self.assertEqual([0], adapter.after_seqs)
            self.assertEqual({key}, pool.state.pending)
            self.assertEqual([], events)

            pool.refresh([session], changed_keys=set())
            self.assertEqual([0, 2], adapter.after_seqs)
            self.assertEqual(["after empty page"], [event.text for event in events])
            self.assertEqual(frozenset(), pool.state.pending)

            pool.refresh([session], changed_keys=set())
            self.assertEqual([0, 2], adapter.after_seqs)

    def test_cursor_pending_page_uses_generic_one_poll_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            transcript = Path(temporary) / "store.db"
            transcript.write_bytes(b"fake cursor store")
            events = []
            tailers = []

            def factory(session):
                tailer = SinglePollTailer(session)
                tailers.append(tailer)
                return tailer

            pool = TailerPool(
                events.append,
                max_event_polls=8,
                tailer_factory=factory,
            )
            session = make_session(
                transcript,
                agent="cursor-cli",
                extra={"transcript_kind": "cursor-chat-sqlite"},
            )
            key = ("cursor-cli", "one")
            pool.refresh(
                [session],
                changed_keys={key},
                initial=True,
            )

            pool.refresh([session], changed_keys={key})
            self.assertEqual(1, tailers[0].poll_calls)
            self.assertEqual({key}, pool.state.pending)
            self.assertEqual([], events)

            pool.refresh([session], changed_keys=set())
            self.assertEqual(2, tailers[0].poll_calls)
            self.assertEqual(["second page"], [event.text for event in events])
            self.assertEqual(frozenset(), pool.state.pending)


if __name__ == "__main__":
    unittest.main()
