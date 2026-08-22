import tempfile
import unittest
from pathlib import Path

from sidecar.index import IncrementalIndex, source_signatures
from sidecar.model import Session


def make_session(root, title="session"):
    return Session(
        agent="fake",
        session_id="one",
        project=str(root),
        transcript=str(root / "events.jsonl"),
        updated_at=100.0,
        title=title,
        extra={"state": str(root / "state.json")},
    )


class IncrementalIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "events.jsonl").write_text('{"type":"old"}\n', encoding="utf-8")
        (self.root / "state.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_reports_changed_unchanged_and_removed_keys(self):
        index = IncrementalIndex()
        session = make_session(self.root)
        key = ("fake", "one")

        first = index.update([session])
        second = index.update([make_session(self.root)])
        with (self.root / "events.jsonl").open("a", encoding="utf-8") as output:
            output.write('{"type":"new"}\n')
        third = index.update([make_session(self.root)])
        fourth = index.update([])

        self.assertEqual({key}, first.changed)
        self.assertEqual(set(), first.removed)
        self.assertEqual({key}, second.unchanged)
        self.assertEqual({key}, third.changed)
        self.assertEqual({key}, fourth.removed)
        self.assertEqual(0, len(index))

    def test_metadata_change_invalidates_without_source_change(self):
        index = IncrementalIndex()
        index.update([make_session(self.root, title="before")])

        delta = index.update([make_session(self.root, title="after")])

        self.assertEqual({("fake", "one")}, delta.changed)
        self.assertEqual("after", index.get(("fake", "one")).title)

    def test_signatures_cover_transcript_and_state_existence(self):
        session = make_session(self.root)
        signatures = source_signatures(session)

        self.assertEqual(
            {
                str(self.root / "events.jsonl"),
                str(self.root / "state.json"),
            },
            {signature.path for signature in signatures},
        )
        self.assertTrue(all(signature.exists for signature in signatures))

        (self.root / "state.json").unlink()
        missing = {
            signature.path: signature
            for signature in source_signatures(session)
        }
        self.assertFalse(missing[str(self.root / "state.json")].exists)


if __name__ == "__main__":
    unittest.main()
