"""Bounded historical replay of plain JSONL transcripts.

The daemon's ``replay`` op is how the board reconstructs a session's history
before the sidecar was watching it. DSH sessions have always had one (a
compressed-stream decode in ``adapters/dsh.py``); every other agent writes an
append-only JSONL transcript that was, until now, only ever *tailed* — so
opening an older Claude or Codex session showed an empty timeline even though
the whole conversation sat on disk.

This module supplies the missing half: a bounded forward scan of a JSONL
transcript that yields the same record shape the adapter's ``normalize``
already consumes when tailing, plus a synthetic ``seq`` cursor so the caller
can page.

The cursor is the record's 1-based line ordinal. It is counted over EVERY
complete line — blank ones and unparseable ones included — because the
ordinal must mean the same thing on the next request; skipping unparseable
lines would renumber the whole file the moment a partially-written line was
later completed. Transcripts here are append-only, so an ordinal is stable
for the life of the file, and a rewritten file (an agent replacing rather
than appending) simply replays from a mismatched cursor — bounded and
recoverable, never corrupting.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# Budgets mirror the DSH replay's intent: bound the scan, the retained bytes,
# the record count, and the wall clock independently, and report which one
# ended the page.
JSONL_REPLAY_SCAN_BYTES = 64 * 1024 * 1024
JSONL_REPLAY_RETAINED_BYTES = 8 * 1024 * 1024
JSONL_REPLAY_RECORDS = 1024
JSONL_REPLAY_LINE_BYTES = 1024 * 1024
JSONL_REPLAY_TIMEOUT_S = 5.0

_CHUNK_BYTES = 256 * 1024


class ReplayUnsupported(Exception):
    """This session's transcript cannot be replayed by its adapter.

    Raised for a source the adapter reads but cannot page backwards — a
    Cursor SQLite chat store, a Copilot workspace file. Distinct from an
    empty page: "there is nothing to show" and "this shape has no replay"
    are different answers, and only the second one should stop the board
    from asking again.
    """


class ReplayPage(List[Dict[str, Any]]):
    """One bounded replay page plus an explicit end-of-transcript signal.

    ``exhausted`` is ``True`` only when the scan truly consumed the
    retrievable transcript: it reached the end of the decoded stream, or a
    degraded source (missing file, missing ``zstd``, failed decoder start)
    had nothing retrievable at all. It is ``False`` when the page stopped
    early on the record, retained-byte, scan-byte, or time budget, so
    callers know more retained records may remain past this page.
    """

    __slots__ = ("exhausted",)

    def __init__(
        self,
        records: Iterable[Dict[str, Any]] = (),
        exhausted: bool = True,
    ) -> None:
        super().__init__(records)
        self.exhausted = bool(exhausted)


def replay_jsonl_events(
    path: Path,
    after_seq: Optional[int] = None,
    max_records: int = JSONL_REPLAY_RECORDS,
    max_scan_bytes: int = JSONL_REPLAY_SCAN_BYTES,
    max_retained_bytes: int = JSONL_REPLAY_RETAINED_BYTES,
    max_line_bytes: int = JSONL_REPLAY_LINE_BYTES,
    timeout: float = JSONL_REPLAY_TIMEOUT_S,
) -> ReplayPage:
    """Return one bounded page of JSONL records after the ``after_seq`` line.

    Each returned record is the parsed object with its line ordinal attached
    as ``seq``. A trailing line without its newline is never returned: the
    writer may still be mid-record, and the live tailer holds the same line
    back, so returning it here would surface a half-written event and then
    contradict it.
    """

    if (
        max_records <= 0
        or max_scan_bytes <= 0
        or max_retained_bytes <= 0
        or max_line_bytes <= 0
        or timeout <= 0
    ):
        return ReplayPage()
    skip_through = 0 if after_seq is None or after_seq < 0 else after_seq

    records: List[Dict[str, Any]] = []
    ordinal = 0
    retained = 0
    scanned = 0
    pending = bytearray()
    dropping_line = False
    stop = False
    eof = False
    deadline = time.monotonic() + timeout

    def consume(raw: bytes) -> bool:
        """Take one complete line; return True when the page is full."""

        nonlocal retained
        if ordinal <= skip_through or not raw.strip():
            return False
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeError):
            return False
        if not isinstance(record, dict):
            return False
        if len(raw) > max_retained_bytes - retained:
            # An already useful page is worth returning; a single oversized
            # record is skipped so a later smaller one can still be found.
            return bool(records)
        record["seq"] = ordinal
        records.append(record)
        retained += len(raw)
        return len(records) >= max_records or retained >= max_retained_bytes

    try:
        with open(path, "rb") as stream:
            while not stop and scanned < max_scan_bytes:
                if time.monotonic() >= deadline:
                    break
                chunk = stream.read(min(_CHUNK_BYTES, max_scan_bytes - scanned))
                if not chunk:
                    eof = True
                    break
                scanned += len(chunk)
                start = 0
                while start < len(chunk):
                    newline = chunk.find(b"\n", start)
                    if newline < 0:
                        segment = chunk[start:]
                        if not dropping_line:
                            if len(pending) + len(segment) <= max_line_bytes:
                                pending.extend(segment)
                            else:
                                pending.clear()
                                dropping_line = True
                        break
                    segment = chunk[start:newline]
                    start = newline + 1
                    ordinal += 1
                    if dropping_line:
                        dropping_line = False
                        pending.clear()
                        continue
                    if len(pending) + len(segment) > max_line_bytes:
                        pending.clear()
                        continue
                    raw = bytes(pending) + segment
                    pending.clear()
                    if consume(raw):
                        stop = True
                        break
    except OSError:
        # A transcript that vanished or cannot be opened has nothing
        # retrievable; that is an exhausted empty page, not a failure the
        # caller should retry against.
        return ReplayPage()

    return ReplayPage(records, exhausted=eof and not stop)


def replay_jsonl_transcript(
    transcript: str,
    after_seq: Optional[int] = None,
    max_records: int = JSONL_REPLAY_RECORDS,
) -> ReplayPage:
    """Adapter-facing wrapper: replay a session's transcript path by ordinal."""

    if not transcript:
        return ReplayPage()
    path = Path(transcript).expanduser()
    try:
        if not path.is_file():
            return ReplayPage()
    except OSError:
        return ReplayPage()
    return replay_jsonl_events(
        path,
        after_seq=after_seq,
        max_records=max_records,
    )


def is_jsonl_transcript(transcript: str) -> bool:
    """Whether a path looks like the append-only JSONL these helpers read."""

    return bool(transcript) and os.path.splitext(transcript)[1].lower() == ".jsonl"


class JsonlReplayMixin:
    """Give an adapter the ordinal-cursor replay of its JSONL transcript.

    Mixed in ahead of :class:`~sidecar.adapters.base.Adapter` so the daemon's
    ``getattr(adapter, "replay")`` probe finds it. Adapters whose transcript
    is only sometimes JSONL (Cursor keeps chats in SQLite too) override this
    and raise :class:`ReplayUnsupported` for the other shapes.
    """

    def replay(
        self,
        session: Any,
        after_seq: Optional[int] = None,
        max_records: int = JSONL_REPLAY_RECORDS,
    ) -> ReplayPage:
        return replay_jsonl_transcript(
            getattr(session, "transcript", ""),
            after_seq=after_seq,
            max_records=max_records,
        )


__all__ = [
    "JSONL_REPLAY_RECORDS",
    "JsonlReplayMixin",
    "ReplayPage",
    "ReplayUnsupported",
    "is_jsonl_transcript",
    "replay_jsonl_events",
    "replay_jsonl_transcript",
]
