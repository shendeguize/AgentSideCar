import datetime as dt
import unittest
from pathlib import Path

from sidecar.model import Session, Status


class _StringValue:
    def __str__(self):
        return "fallback"


class ModelSerializationTests(unittest.TestCase):
    def test_session_extra_serializes_supported_stdlib_values(self):
        session = Session(
            agent="test",
            session_id="session",
            project="project",
            transcript="transcript",
            updated_at=1.0,
            status="idle",
            extra={
                "status": Status.WORKING,
                "date": dt.date(2026, 8, 24),
                "path": Path("relative/path"),
                "fallback": _StringValue(),
            },
        )

        self.assertEqual(
            {
                "status": "working",
                "date": "2026-08-24",
                "path": "relative/path",
                "fallback": "fallback",
            },
            session.to_dict()["extra"],
        )


if __name__ == "__main__":
    unittest.main()
