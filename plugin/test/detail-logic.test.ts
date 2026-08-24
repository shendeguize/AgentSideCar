/**
 * Tests for src/client/detail/logic.ts (T5.3) — the pure view-model layer
 * of the session-detail timeline view. Node environment, no React, no DOM:
 * entry normalization, seq-gap detection (the design §4.b.3 honesty
 * marker), history-page merge dedup, listen-mode incremental merge,
 * source badges, relative-time boundaries, and the loading/empty/error
 * body-state model.
 */

import { describe, expect, it } from 'vitest'

import {
  BODY_MAX_CHARS,
  DEFAULT_MAX_RENDER_ROWS,
  SUMMARY_MAX_CHARS,
  aggregateChunkRows,
  applyListenPage,
  applyTimelinePage,
  buildTimelineRows,
  classifyKind,
  createTimelineVM,
  deriveDetailBodyState,
  deriveDetailStatus,
  deriveSourceBadges,
  detailErrorText,
  entryKey,
  filterTimelineRows,
  formatEventTime,
  formatRelativeTime,
  formatTemplate,
  gapLabel,
  isStreamChunkEntry,
  limitTimelineRows,
  normalizeTimelineEntry,
  shouldStickToLatest,
  sortTimelineEntries,
  type TimelineEntryWire,
  type TimelinePageWire,
  type TimelineRowVM,
  type TimelineSourcesWire,
  type TimelineVM,
} from '../src/client/detail/logic.ts'
import { DETAIL_STRINGS } from '../src/client/detail/strings.ts'

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

const NO_SOURCES: TimelineSourcesWire = {
  dshLive: false,
  dshCold: false,
  sidecarReplay: false,
  sidecarBuffer: false,
}

function wire(over: Partial<TimelineEntryWire> = {}): TimelineEntryWire {
  return {
    origin: 'sidecar',
    seq: 1,
    ts: 1_000_000,
    kind: 'assistant',
    text: 'hello',
    extra: null,
    ...over,
  }
}

function page(
  entries: TimelineEntryWire[],
  over: Partial<TimelinePageWire> = {},
): TimelinePageWire {
  return {
    sessionId: 's1',
    entries,
    cursor: null,
    nextCursor: null,
    sources: { ...NO_SOURCES, sidecarBuffer: true },
    ...over,
  }
}

/** Shorthand: a seq-carrying entry with ts following the seq. */
function seqEntry(seq: number, over: Partial<TimelineEntryWire> = {}): TimelineEntryWire {
  return wire({ seq, ts: 1_000_000 + seq * 1000, text: `event ${seq}`, ...over })
}

function vmWith(entries: TimelineEntryWire[]): TimelineVM {
  return applyTimelinePage(createTimelineVM('s1'), page(entries))
}

function eventRows(rows: TimelineRowVM[]): Array<Extract<TimelineRowVM, { type: 'event' }>> {
  return rows.filter((r): r is Extract<TimelineRowVM, { type: 'event' }> => r.type === 'event')
}

/** Expected dedup key of a wire entry (delegates to entryKey). */
function keyOf(entry: TimelineEntryWire): string {
  return entryKey(entry)
}

function gapRows(rows: TimelineRowVM[]): Array<Extract<TimelineRowVM, { type: 'gap' }>> {
  return rows.filter((r): r is Extract<TimelineRowVM, { type: 'gap' }> => r.type === 'gap')
}

// ---------------------------------------------------------------------------
// Kind classification.
// ---------------------------------------------------------------------------

describe('classifyKind', () => {
  it('maps the sidecar normalized vocabulary', () => {
    expect(classifyKind('user')).toBe('user')
    expect(classifyKind('assistant')).toBe('assistant')
    expect(classifyKind('thinking')).toBe('thinking')
    expect(classifyKind('tool_call')).toBe('toolCall')
    expect(classifyKind('tool_result')).toBe('toolResult')
    expect(classifyKind('turn_start')).toBe('turn')
    expect(classifyKind('turn_end')).toBe('turn')
    expect(classifyKind('step_start')).toBe('step')
    expect(classifyKind('error')).toBe('error')
  })

  it('maps dsh native slash-path types by their last segment', () => {
    expect(classifyKind('message/user')).toBe('user')
    expect(classifyKind('message/assistant')).toBe('assistant')
    expect(classifyKind('agent/tool_call')).toBe('toolCall')
  })

  it('falls back to the first segment for family-first dsh kinds (live data)', () => {
    expect(classifyKind('user/message')).toBe('user')
    expect(classifyKind('assistant/message')).toBe('assistant')
    expect(classifyKind('assistant/chunk')).toBe('assistant')
    expect(classifyKind('turn/start')).toBe('turn')
    expect(classifyKind('step/end')).toBe('step')
    // …but the last segment stays authoritative when both match.
    expect(classifyKind('message/user')).toBe('user')
  })

  it('is honest about unknown kinds', () => {
    expect(classifyKind('session/title')).toBe('other')
    expect(classifyKind('agent/inbox/spliced')).toBe('other')
    expect(classifyKind('request/header')).toBe('other')
    expect(classifyKind('')).toBe('other')
    expect(classifyKind('whatever_new_kind')).toBe('other')
  })
})

// ---------------------------------------------------------------------------
// Entry normalization.
// ---------------------------------------------------------------------------

describe('normalizeTimelineEntry', () => {
  it('derives glyph, label, seq and key for a known kind', () => {
    const vm = normalizeTimelineEntry(wire({ kind: 'user', seq: 7, text: 'question' }))
    expect(vm.kind).toBe('user')
    expect(vm.label).toBe(DETAIL_STRINGS.kind.user)
    expect(vm.glyph).not.toBe('')
    expect(vm.seq).toBe(7)
    expect(vm.key).toBe('s:7\u0000user\u0000question')
    expect(vm.summary).toBe('question')
    expect(vm.expandable).toBe(false)
    expect(vm.body).toBeNull()
  })

  it('keeps the raw kind as the label for unknown kinds', () => {
    const vm = normalizeTimelineEntry(wire({ kind: 'session/title' }))
    expect(vm.kind).toBe('other')
    expect(vm.label).toBe('session/title')
  })

  it('summarizes to the first line, truncated with an ellipsis, keeping the full body', () => {
    const long = `${'x'.repeat(SUMMARY_MAX_CHARS + 40)}\nsecond line`
    const vm = normalizeTimelineEntry(wire({ text: long }))
    expect(vm.summary).toBe(`${'x'.repeat(SUMMARY_MAX_CHARS)}…`)
    expect(vm.summary.length).toBe(SUMMARY_MAX_CHARS + 1)
    expect(vm.expandable).toBe(true)
    expect(vm.body).toBe(long)
  })

  it('treats a multiline text as expandable even when the first line is short', () => {
    const vm = normalizeTimelineEntry(wire({ text: 'line one\nline two' }))
    expect(vm.summary).toBe('line one')
    expect(vm.expandable).toBe(true)
    expect(vm.body).toBe('line one\nline two')
  })

  it('bounds the expanded body at BODY_MAX_CHARS', () => {
    const vm = normalizeTimelineEntry(wire({ text: `short\n${'y'.repeat(BODY_MAX_CHARS + 100)}` }))
    expect(vm.body).not.toBeNull()
    expect(vm.body!.length).toBe(BODY_MAX_CHARS + 1) // truncated + ellipsis
  })

  it('falls back to dsh data.text when the normalized text is empty', () => {
    const vm = normalizeTimelineEntry(
      wire({ origin: 'dsh', text: '', data: { text: 'from dsh' }, kind: 'message/user' }),
    )
    expect(vm.summary).toBe('from dsh')
  })

  it('exposes pretty-printed dsh data as the body when there is no text', () => {
    const vm = normalizeTimelineEntry(
      wire({ origin: 'dsh', text: '', data: { tool: 'grep', args: [1] }, kind: 'tool_call' }),
    )
    expect(vm.summary).toBe('')
    expect(vm.expandable).toBe(true)
    expect(vm.body).toBe(JSON.stringify({ tool: 'grep', args: [1] }, null, 2))
  })

  it('renders nothing expandable for a bare sidecar entry without data', () => {
    const vm = normalizeTimelineEntry(wire({ text: 'plain' }))
    expect(vm.expandable).toBe(false)
    expect(vm.body).toBeNull()
  })
})

describe('entryKey', () => {
  it('collapses seq-carrying entries on seq+kind+text, ignoring ts', () => {
    const base = { seq: 3, ts: 1, kind: 'assistant', text: 'x' }
    // Same event seen twice (e.g. replay vs ring, differing clock reads):
    // one identity regardless of ts.
    expect(entryKey(base)).toBe(entryKey({ ...base, ts: 999 }))
    // Same-seq SIBLING events (multi-block record) stay distinct — a
    // seq-only key silently dropped them (F1).
    expect(entryKey(base)).not.toBe(entryKey({ ...base, kind: 'thinking' }))
    expect(entryKey(base)).not.toBe(entryKey({ ...base, text: 'y' }))
    expect(entryKey(base)).not.toBe(entryKey({ ...base, seq: 4 }))
  })

  it('distinguishes seq-less entries by ts, kind and text', () => {
    const base = { seq: null, ts: 5, kind: 'assistant', text: 'x' }
    expect(entryKey(base)).toBe(entryKey({ ...base }))
    expect(entryKey(base)).not.toBe(entryKey({ ...base, ts: 6 }))
    expect(entryKey(base)).not.toBe(entryKey({ ...base, kind: 'user' }))
    expect(entryKey(base)).not.toBe(entryKey({ ...base, text: 'y' }))
  })
})

// ---------------------------------------------------------------------------
// Ordering.
// ---------------------------------------------------------------------------

describe('sortTimelineEntries', () => {
  it('keeps exact seq order even when timestamps wobble within the seq domain', () => {
    const entries = [
      normalizeTimelineEntry(wire({ seq: 2, ts: 90 })),
      normalizeTimelineEntry(wire({ seq: 1, ts: 100 })),
    ]
    expect(sortTimelineEntries(entries).map((e) => e.seq)).toEqual([1, 2])
  })

  it('interleaves seq-less entries by timestamp', () => {
    const entries = [
      normalizeTimelineEntry(wire({ seq: 1, ts: 100 })),
      normalizeTimelineEntry(wire({ seq: null, ts: 95, text: 'early' })),
      normalizeTimelineEntry(wire({ seq: 2, ts: 110 })),
      normalizeTimelineEntry(wire({ seq: null, ts: 105, text: 'middle' })),
    ]
    expect(sortTimelineEntries(entries).map((e) => e.seq ?? e.summary)).toEqual([
      'early',
      1,
      'middle',
      2,
    ])
  })
})

// ---------------------------------------------------------------------------
// History-page merge (initial + 加载更多).
// ---------------------------------------------------------------------------

describe('applyTimelinePage', () => {
  it('accumulates pages backward: dedup on overlap, ascending order, cursor advances', () => {
    const initial = applyTimelinePage(
      createTimelineVM('s1'),
      page([seqEntry(4), seqEntry(5)], { nextCursor: 'tok-older-1' }),
    )
    expect(initial.entries.map((e) => e.seq)).toEqual([4, 5])
    expect(initial.nextCursor).toBe('tok-older-1')
    expect(initial.reachedStart).toBe(false)

    // The older page overlaps on seq 4 — the duplicate must fold away.
    const older = applyTimelinePage(
      initial,
      page([seqEntry(2), seqEntry(3), seqEntry(4)], { nextCursor: 'tok-older-2' }),
    )
    expect(older.entries.map((e) => e.seq)).toEqual([2, 3, 4, 5])
    expect(older.nextCursor).toBe('tok-older-2')
    expect(older.reachedStart).toBe(false)

    const last = applyTimelinePage(older, page([seqEntry(1)], { nextCursor: null }))
    expect(last.entries.map((e) => e.seq)).toEqual([1, 2, 3, 4, 5])
    expect(last.nextCursor).toBeNull()
    expect(last.reachedStart).toBe(true)
  })

  it('unions page sources instead of overwriting them', () => {
    const first = applyTimelinePage(
      createTimelineVM('s1'),
      page([seqEntry(1)], { sources: { ...NO_SOURCES, dshLive: true } }),
    )
    const second = applyTimelinePage(
      first,
      page([seqEntry(2)], { sources: { ...NO_SOURCES, sidecarReplay: true } }),
    )
    expect(second.sources).toEqual({
      dshLive: true,
      dshCold: false,
      sidecarReplay: true,
      sidecarBuffer: false,
    })
  })

  it('never marks history entries as new and leaves prior highlights intact', () => {
    const listened = applyListenPage(vmWith([seqEntry(1)]), page([seqEntry(2)]))
    expect(listened.newKeys).toEqual([keyOf(seqEntry(2))])
    const paged = applyTimelinePage(listened, page([seqEntry(0)], { nextCursor: null }))
    expect(paged.newKeys).toEqual([keyOf(seqEntry(2))])
    expect(paged.entries.map((e) => e.seq)).toEqual([0, 1, 2])
  })

  it('does not mutate the previous state (fresh objects per apply)', () => {
    const before = vmWith([seqEntry(1)])
    const snapshot = before.entries.map((e) => e.key)
    applyTimelinePage(before, page([seqEntry(2)]))
    expect(before.entries.map((e) => e.key)).toEqual(snapshot)
  })
})

// ---------------------------------------------------------------------------
// Listen-mode incremental merge.
// ---------------------------------------------------------------------------

describe('applyListenPage', () => {
  it('appends only unseen entries and reports exactly those as new', () => {
    const base = vmWith([seqEntry(4), seqEntry(5)])
    const next = applyListenPage(base, page([seqEntry(5), seqEntry(6), seqEntry(7)]))
    expect(next.entries.map((e) => e.seq)).toEqual([4, 5, 6, 7])
    expect(next.newKeys).toEqual([keyOf(seqEntry(6)), keyOf(seqEntry(7))])
  })

  it('re-applying the same newest window highlights nothing (dedup, no re-highlight)', () => {
    const base = applyListenPage(vmWith([seqEntry(4)]), page([seqEntry(5)]))
    expect(base.newKeys).toEqual([keyOf(seqEntry(5))])
    const replay = applyListenPage(base, page([seqEntry(4), seqEntry(5)]))
    expect(replay.entries.map((e) => e.seq)).toEqual([4, 5])
    expect(replay.newKeys).toEqual([])
  })

  it('dedups seq-less events on ts+kind+text', () => {
    const seqless = wire({ seq: null, ts: 42, kind: 'assistant', text: 'same' })
    const base = vmWith([seqless])
    const next = applyListenPage(
      base,
      page([
        wire({ seq: null, ts: 42, kind: 'assistant', text: 'same' }),
        wire({ seq: null, ts: 43, kind: 'assistant', text: 'other' }),
      ]),
    )
    expect(next.entries).toHaveLength(2)
    expect(next.newKeys).toHaveLength(1)
  })

  it('never touches the history pagination cursor', () => {
    const base = applyTimelinePage(
      createTimelineVM('s1'),
      page([seqEntry(4)], { nextCursor: 'tok-old' }),
    )
    const next = applyListenPage(base, page([seqEntry(5)], { nextCursor: 'tok-newest' }))
    expect(next.nextCursor).toBe('tok-old')
    expect(next.reachedStart).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Same-seq sibling events (F1: one dsh record → several normalized events
// sharing one seq; a seq-only key silently folded the siblings away).
// ---------------------------------------------------------------------------

describe('same-seq sibling events (F1)', () => {
  const thinking = wire({ seq: 2, ts: 1_002_000, kind: 'thinking', text: 'let me think' })
  const answer = wire({ seq: 2, ts: 1_002_000, kind: 'assistant', text: 'the answer' })

  it('keeps every sibling (reasoning+text of one record) in order and flags no gap', () => {
    const vm = vmWith([seqEntry(1), thinking, answer])
    expect(vm.entries.map((e) => [e.seq, e.kindRaw])).toEqual([
      [1, 'assistant'],
      [2, 'thinking'],
      [2, 'assistant'],
    ])
    const rows = buildTimelineRows(vm, 2_000_000)
    // Equal seqs are not a discontinuity: no gap marker anywhere.
    expect(gapRows(rows)).toEqual([])
    expect(eventRows(rows)).toHaveLength(3)
  })

  it('keeps multi-block siblings of the SAME kind apart by text', () => {
    const partOne = wire({ seq: 6, ts: 1_006_000, kind: 'user', text: 'part one' })
    const partTwo = wire({ seq: 6, ts: 1_006_000, kind: 'user', text: 'part two' })
    const vm = vmWith([partOne, partTwo])
    expect(vm.entries.map((e) => e.summary)).toEqual(['part one', 'part two'])
  })

  it('detects a real seq break across a sibling group with the honest count', () => {
    const vm = vmWith([thinking, answer, seqEntry(5)])
    const gaps = gapRows(buildTimelineRows(vm, 2_000_000))
    expect(gaps).toHaveLength(1)
    expect(gaps[0]!.missingCount).toBe(2) // seq 3 and 4
  })

  it('dedups a listen replay of the same siblings without re-highlighting', () => {
    const base = vmWith([thinking, answer])
    const replay = applyListenPage(base, page([thinking, answer]))
    expect(replay.entries).toHaveLength(2)
    expect(replay.newKeys).toEqual([])
  })

  it('orders the dsh entry before its sidecar siblings regardless of arrival order', () => {
    const sibling = wire({ seq: 3, ts: 1_003_000, kind: 'assistant', text: 'text block' })
    const dshEntry = wire({
      origin: 'dsh',
      seq: 3,
      ts: 1_003_000,
      kind: 'assistant/message',
      text: 'reasoning block',
      data: { blocks: 2 },
    })
    // The sidecar sibling lands first (older fetch), the dsh entry later:
    // the merged order still mirrors the host (dsh primary first).
    const vm = applyListenPage(vmWith([sibling]), page([dshEntry]))
    expect(vm.entries.map((e) => [e.origin, e.kindRaw])).toEqual([
      ['dsh', 'assistant/message'],
      ['sidecar', 'assistant'],
    ])
  })

  it('upgrades the un-supplemented dsh entry in place when its folded text arrives', () => {
    const bare = wire({
      origin: 'dsh',
      seq: 5,
      ts: 1_005_000,
      kind: 'assistant/message',
      text: '',
    })
    const folded = wire({
      origin: 'dsh',
      seq: 5,
      ts: 1_005_000,
      kind: 'assistant/message',
      text: 'now supplemented',
    })
    const base = vmWith([bare])
    expect(base.entries[0]!.summary).toBe('')

    // The twin text arrived on the host between fetches: same event, new
    // key — it must replace the bare entry, not duplicate it.
    const upgraded = applyListenPage(base, page([folded]))
    expect(upgraded.entries).toHaveLength(1)
    expect(upgraded.entries[0]!.summary).toBe('now supplemented')
    expect(upgraded.newKeys).toEqual([keyOf(folded)])

    // …and a stale un-supplemented replay of the same event is dropped.
    const stale = applyListenPage(upgraded, page([bare]))
    expect(stale.entries).toHaveLength(1)
    expect(stale.entries[0]!.summary).toBe('now supplemented')
    expect(stale.newKeys).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Gap detection (design §4.b.3 honest presentation).
// ---------------------------------------------------------------------------

describe('buildTimelineRows gap detection', () => {
  const NOW = 2_000_000

  it('inserts no marker for a continuous seq run', () => {
    const rows = buildTimelineRows(vmWith([seqEntry(1), seqEntry(2), seqEntry(3)]), NOW)
    expect(gapRows(rows)).toHaveLength(0)
    expect(rows).toHaveLength(3)
  })

  it('inserts a gap marker with the missing count on a seq break', () => {
    const rows = buildTimelineRows(vmWith([seqEntry(1), seqEntry(4)]), NOW)
    const gaps = gapRows(rows)
    expect(gaps).toHaveLength(1)
    expect(gaps[0]!.missingCount).toBe(2)
    expect(gaps[0]!.label).toBe(
      formatTemplate(DETAIL_STRINGS.gap.label, { n: 2 }),
    )
    // Marker sits between the two events.
    expect(rows.map((r) => r.type)).toEqual(['event', 'gap', 'event'])
  })

  it('skips seq-less entries: they neither trigger nor mask a gap', () => {
    const noGap = buildTimelineRows(
      vmWith([seqEntry(1), wire({ seq: null, ts: 1_001_500, text: 'aside' }), seqEntry(2)]),
      NOW,
    )
    expect(gapRows(noGap)).toHaveLength(0)

    const withGap = buildTimelineRows(
      vmWith([seqEntry(1), wire({ seq: null, ts: 1_002_500, text: 'aside' }), seqEntry(5)]),
      NOW,
    )
    const gaps = gapRows(withGap)
    expect(gaps).toHaveLength(1)
    expect(gaps[0]!.missingCount).toBe(3)
    // The seq-less row stays where its ts puts it; the marker precedes seq 5.
    expect(withGap.map((r) => (r.type === 'gap' ? 'gap' : (r.entry.seq ?? 'x')))).toEqual([
      1,
      'x',
      'gap',
      5,
    ])
  })

  it('raises no false positive on an all-seq-less timeline', () => {
    const rows = buildTimelineRows(
      vmWith([
        wire({ seq: null, ts: 10, text: 'a' }),
        wire({ seq: null, ts: 20, text: 'b' }),
      ]),
      NOW,
    )
    expect(gapRows(rows)).toHaveLength(0)
  })

  it('does not flag unloaded older history as a gap (no marker before the first entry)', () => {
    const rows = buildTimelineRows(vmWith([seqEntry(50), seqEntry(51)]), NOW)
    expect(gapRows(rows)).toHaveLength(0)
  })

  it('marks every break in a multi-gap timeline with stable keys', () => {
    const rows = buildTimelineRows(vmWith([seqEntry(1), seqEntry(3), seqEntry(10)]), NOW)
    const gaps = gapRows(rows)
    expect(gaps.map((g) => g.missingCount)).toEqual([1, 6])
    expect(gaps.map((g) => g.key)).toEqual(['gap:1-3', 'gap:3-10'])
  })

  it('flags listen-merged new entries via isNew and no others', () => {
    const vm = applyListenPage(vmWith([seqEntry(1)]), page([seqEntry(2)]))
    const rows = eventRows(buildTimelineRows(vm, NOW))
    expect(rows.map((r) => [r.entry.seq, r.isNew])).toEqual([
      [1, false],
      [2, true],
    ])
  })

  it('carries hover metadata (ISO time, raw kind, origin, seq)', () => {
    const vm = vmWith([seqEntry(3, { kind: 'message/user', origin: 'dsh' })])
    const row = eventRows(buildTimelineRows(vm, NOW))[0]!
    expect(row.hoverTitle).toContain('message/user')
    expect(row.hoverTitle).toContain('dsh')
    expect(row.hoverTitle).toContain(new Date(1_003_000).toISOString())
    expect(row.hoverTitle).toContain(formatTemplate(DETAIL_STRINGS.timeline.seq, { n: 3 }))
  })
})

describe('gapLabel', () => {
  it('interpolates the honest wording with the count', () => {
    expect(gapLabel(7)).toBe('缺口:可能有 7 条事件未捕获(256 队列上限或未持久化)')
  })
})

// ---------------------------------------------------------------------------
// Segmented rendering bound.
// ---------------------------------------------------------------------------

describe('limitTimelineRows', () => {
  const NOW = 2_000_000

  it('passes small lists through untouched', () => {
    const rows = buildTimelineRows(vmWith([seqEntry(1), seqEntry(2)]), NOW)
    const limited = limitTimelineRows(rows, 10)
    expect(limited.rows).toHaveLength(2)
    expect(limited.hiddenCount).toBe(0)
    expect(limited.notice).toBeNull()
  })

  it('keeps the newest rows and reports the hidden count', () => {
    const entries = Array.from({ length: 10 }, (_, i) => seqEntry(i + 1))
    const rows = buildTimelineRows(vmWith(entries), NOW)
    const limited = limitTimelineRows(rows, 4)
    expect(limited.hiddenCount).toBe(6)
    expect(eventRows(limited.rows).map((r) => r.entry.seq)).toEqual([7, 8, 9, 10])
    expect(limited.notice).toBe(
      formatTemplate(DETAIL_STRINGS.timeline.hiddenNotice, { n: 6 }),
    )
  })

  it('has a sane default cap', () => {
    expect(DEFAULT_MAX_RENDER_ROWS).toBeGreaterThan(0)
  })
})

// ---------------------------------------------------------------------------
// Source badges.
// ---------------------------------------------------------------------------

describe('deriveSourceBadges', () => {
  it('yields nothing when no source contributed', () => {
    expect(deriveSourceBadges(NO_SOURCES)).toEqual([])
  })

  it('lists contributing sources in stable order with honest labels', () => {
    const badges = deriveSourceBadges({
      dshLive: false,
      dshCold: true,
      sidecarReplay: true,
      sidecarBuffer: true,
    })
    expect(badges.map((b) => b.id)).toEqual(['dshCold', 'sidecarReplay', 'sidecarBuffer'])
    expect(badges.map((b) => b.label)).toEqual([
      DETAIL_STRINGS.sources.dshCold,
      DETAIL_STRINGS.sources.sidecarReplay,
      DETAIL_STRINGS.sources.sidecarBuffer,
    ])
  })

  it('marks the live dsh feed with the success tone', () => {
    const badges = deriveSourceBadges({ ...NO_SOURCES, dshLive: true })
    expect(badges).toEqual([
      { id: 'dshLive', label: DETAIL_STRINGS.sources.dshLive, tone: 'success' },
    ])
  })
})

// ---------------------------------------------------------------------------
// Relative time boundaries.
// ---------------------------------------------------------------------------

describe('formatRelativeTime', () => {
  const NOW = 10 * 86_400_000

  it('renders 刚刚 below one minute, including future clock skew', () => {
    expect(formatRelativeTime(NOW, NOW)).toBe(DETAIL_STRINGS.time.justNow)
    expect(formatRelativeTime(NOW - 59_999, NOW)).toBe(DETAIL_STRINGS.time.justNow)
    expect(formatRelativeTime(NOW + 3_600_000, NOW)).toBe(DETAIL_STRINGS.time.justNow)
  })

  it('switches buckets exactly on the minute/hour/day boundaries', () => {
    expect(formatRelativeTime(NOW - 60_000, NOW)).toBe('1 分钟前')
    expect(formatRelativeTime(NOW - 3_599_999, NOW)).toBe('59 分钟前')
    expect(formatRelativeTime(NOW - 3_600_000, NOW)).toBe('1 小时前')
    expect(formatRelativeTime(NOW - 86_399_999, NOW)).toBe('23 小时前')
    expect(formatRelativeTime(NOW - 86_400_000, NOW)).toBe('1 天前')
  })

  it('renders empty for non-finite input', () => {
    expect(formatRelativeTime(Number.NaN, NOW)).toBe('')
    expect(formatRelativeTime(Number.POSITIVE_INFINITY, NOW)).toBe('')
  })
})

// ---------------------------------------------------------------------------
// Absolute short time (UX-13) — local-calendar rule, so fixtures build
// their instants from local Date components, never epoch literals.
// ---------------------------------------------------------------------------

describe('formatEventTime', () => {
  const now = new Date(2026, 7, 25, 14, 30).getTime()

  it('renders HH:mm within the same local day', () => {
    expect(formatEventTime(new Date(2026, 7, 25, 9, 5).getTime(), now)).toBe('09:05')
    expect(formatEventTime(new Date(2026, 7, 25, 0, 0).getTime(), now)).toBe('00:00')
  })

  it('prefixes MM-DD on any other day, even less than 24h away', () => {
    expect(formatEventTime(new Date(2026, 7, 24, 23, 59).getTime(), now)).toBe('08-24 23:59')
    // Same month/day one year earlier is still "another day".
    expect(formatEventTime(new Date(2025, 7, 25, 14, 30).getTime(), now)).toBe('08-25 14:30')
  })

  it('renders empty for non-finite input', () => {
    expect(formatEventTime(Number.NaN, now)).toBe('')
    expect(formatEventTime(now, Number.NaN)).toBe('')
  })
})

// ---------------------------------------------------------------------------
// Kind filter (UX-03): conversation-first with an honest hidden count.
// ---------------------------------------------------------------------------

describe('filterTimelineRows', () => {
  const NOW = 2_000_000

  it("passes everything through untouched in 'all' mode", () => {
    const rows = buildTimelineRows(
      vmWith([seqEntry(1), seqEntry(2, { kind: 'request/header' })]),
      NOW,
    )
    const out = filterTimelineRows(rows, 'all')
    expect(out.rows).toEqual(rows)
    expect(out.hiddenCount).toBe(0)
  })

  it("keeps only user/assistant/error in 'conversation' mode and counts the rest", () => {
    const rows = buildTimelineRows(
      vmWith([
        seqEntry(1, { kind: 'user' }),
        seqEntry(2, { kind: 'assistant' }),
        seqEntry(3, { kind: 'thinking' }),
        seqEntry(4, { kind: 'tool_call' }),
        seqEntry(5, { kind: 'request/header' }),
        seqEntry(6, { kind: 'error' }),
      ]),
      NOW,
    )
    const out = filterTimelineRows(rows, 'conversation')
    expect(eventRows(out.rows).map((r) => r.entry.kind)).toEqual(['user', 'assistant', 'error'])
    expect(out.hiddenCount).toBe(3)
  })

  it('never drops a gap marker: honesty rows survive the filter', () => {
    const rows = buildTimelineRows(
      vmWith([seqEntry(1, { kind: 'thinking' }), seqEntry(4, { kind: 'thinking' })]),
      NOW,
    )
    const out = filterTimelineRows(rows, 'conversation')
    expect(out.rows.map((r) => r.type)).toEqual(['gap'])
    expect(out.hiddenCount).toBe(2)
  })

  it('keeps assistant streaming chunks, which then collapse via aggregation (live dsh shape)', () => {
    // Real dsh timeline shape: user/message + empty assistant/chunk noise +
    // protocol rows. Conversation mode keeps the chunks (assistant family)
    // and the pipeline's aggregation step folds them into one line.
    const rows = buildTimelineRows(
      vmWith([
        seqEntry(1, { kind: 'user/message' }),
        seqEntry(2, { kind: 'assistant/chunk', text: '' }),
        seqEntry(3, { kind: 'request/header', text: '' }),
        seqEntry(4, { kind: 'assistant/chunk', text: '' }),
        seqEntry(5, { kind: 'assistant/message' }),
      ]),
      NOW,
    )
    const filtered = filterTimelineRows(rows, 'conversation')
    expect(filtered.hiddenCount).toBe(1) // request/header only
    const out = aggregateChunkRows(filtered.rows)
    // The protocol row between the chunks was filtered out, so the two
    // chunks became adjacent and collapsed into one run.
    expect(out.map((r) => (r.type === 'chunks' ? `chunks×${r.count}` : r.type))).toEqual([
      'event',
      'chunks×2',
      'event',
    ])
  })
})

// ---------------------------------------------------------------------------
// Streaming-chunk aggregation (UX-03).
// ---------------------------------------------------------------------------

/** Shorthand: an empty-text streaming-chunk entry at the given seq. */
function chunkEntry(seq: number, over: Partial<TimelineEntryWire> = {}): TimelineEntryWire {
  return seqEntry(seq, { kind: 'assistant/chunk', text: '', ...over })
}

function chunkRuns(rows: TimelineRowVM[]): Array<Extract<TimelineRowVM, { type: 'chunks' }>> {
  return rows.filter((r): r is Extract<TimelineRowVM, { type: 'chunks' }> => r.type === 'chunks')
}

describe('isStreamChunkEntry', () => {
  it('matches empty-summary chunk-flavored kinds by the last slash segment', () => {
    const { entries } = vmWith([
      chunkEntry(1),
      chunkEntry(2, { kind: 'chunk' }),
      chunkEntry(3, { kind: 'stream_chunk' }),
      chunkEntry(4, { kind: 'stream-chunk' }),
    ])
    expect(entries.map((e) => isStreamChunkEntry(e))).toEqual([true, true, true, true])
  })

  it('refuses chunks that carry text and non-chunk kinds', () => {
    const { entries } = vmWith([
      chunkEntry(1, { text: 'partial words' }),
      seqEntry(2, { kind: 'assistant', text: '' }),
    ])
    expect(entries.map((e) => isStreamChunkEntry(e))).toEqual([false, false])
  })
})

describe('aggregateChunkRows', () => {
  const NOW = 2_000_000

  it('collapses a run of ≥2 adjacent same-kind chunks into one lossless row', () => {
    const rows = buildTimelineRows(
      vmWith([
        seqEntry(1, { kind: 'user' }),
        chunkEntry(2),
        chunkEntry(3),
        chunkEntry(4),
        seqEntry(5, { kind: 'assistant' }),
      ]),
      NOW,
    )
    const out = aggregateChunkRows(rows)
    expect(out.map((r) => r.type)).toEqual(['event', 'chunks', 'event'])
    const run = chunkRuns(out)[0]!
    expect(run.count).toBe(3)
    expect(run.label).toBe(formatTemplate(DETAIL_STRINGS.timeline.chunkRun, { n: 3 }))
    // Lossless: members are the original event rows, verbatim.
    expect(run.members).toEqual(eventRows(rows).slice(1, 4))
    // Stable key (first member) + newest member's time label.
    expect(run.key).toBe(`chunks:${run.members[0]!.key}`)
    expect(run.timeLabel).toBe(run.members[2]!.timeLabel)
  })

  it('keeps a single chunk as a plain row (a 1-run header would add noise)', () => {
    const rows = buildTimelineRows(vmWith([seqEntry(1, { kind: 'user' }), chunkEntry(2)]), NOW)
    expect(aggregateChunkRows(rows).map((r) => r.type)).toEqual(['event', 'event'])
  })

  it('breaks the run on a gap marker: aggregation cannot paper over a seq break', () => {
    const rows = buildTimelineRows(
      vmWith([chunkEntry(1), chunkEntry(2), chunkEntry(5), chunkEntry(6)]),
      NOW,
    )
    expect(aggregateChunkRows(rows).map((r) => r.type)).toEqual(['chunks', 'gap', 'chunks'])
  })

  it('splits adjacent runs of different chunk kinds', () => {
    const rows = buildTimelineRows(
      vmWith([
        chunkEntry(1),
        chunkEntry(2),
        chunkEntry(3, { kind: 'thinking/chunk' }),
        chunkEntry(4, { kind: 'thinking/chunk' }),
      ]),
      NOW,
    )
    expect(chunkRuns(aggregateChunkRows(rows)).map((r) => [r.kindRaw, r.count])).toEqual([
      ['assistant/chunk', 2],
      ['thinking/chunk', 2],
    ])
  })

  it('marks the run new when any member arrived in the latest listen merge', () => {
    const vm = applyListenPage(vmWith([chunkEntry(1), chunkEntry(2)]), page([chunkEntry(3)]))
    const runs = chunkRuns(aggregateChunkRows(buildTimelineRows(vm, NOW)))
    expect(runs).toHaveLength(1)
    expect(runs[0]!.count).toBe(3)
    expect(runs[0]!.isNew).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Viewport landing rule (UX-04).
// ---------------------------------------------------------------------------

describe('shouldStickToLatest', () => {
  it('never scrolls an empty timeline', () => {
    expect(shouldStickToLatest({ entryCount: 0, positioned: false, listening: false })).toBe(false)
    expect(shouldStickToLatest({ entryCount: 0, positioned: false, listening: true })).toBe(false)
  })

  it('lands on the newest events exactly once when entries first arrive', () => {
    expect(shouldStickToLatest({ entryCount: 5, positioned: false, listening: false })).toBe(true)
    expect(shouldStickToLatest({ entryCount: 5, positioned: true, listening: false })).toBe(false)
  })

  it('keeps pinning the tail while listen mode is on', () => {
    expect(shouldStickToLatest({ entryCount: 5, positioned: true, listening: true })).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Header status badge.
// ---------------------------------------------------------------------------

describe('deriveDetailStatus', () => {
  it('maps the known vocabulary with tones', () => {
    expect(deriveDetailStatus('working')).toEqual({
      status: 'working',
      tone: 'success',
      label: DETAIL_STRINGS.status.working,
    })
    expect(deriveDetailStatus(' DEAD ')).toEqual({
      status: 'dead',
      tone: 'muted',
      label: DETAIL_STRINGS.status.dead,
    })
  })

  it('keeps unknown raw statuses as their own label, empty reads 未知', () => {
    expect(deriveDetailStatus('paused')).toEqual({
      status: 'unknown',
      tone: 'neutral',
      label: 'paused',
    })
    expect(deriveDetailStatus('  ').label).toBe(DETAIL_STRINGS.status.unknown)
  })
})

// ---------------------------------------------------------------------------
// Loading / empty / error body states.
// ---------------------------------------------------------------------------

describe('deriveDetailBodyState', () => {
  it('shows the loading state only before any entry arrived', () => {
    expect(deriveDetailBodyState({ loading: true, error: null, entryCount: 0 })).toEqual({
      kind: 'loading',
      title: DETAIL_STRINGS.states.loadingTitle,
      hint: null,
      errorBanner: null,
    })
  })

  it('shows the empty state when a successful load found nothing', () => {
    const state = deriveDetailBodyState({ loading: false, error: null, entryCount: 0 })
    expect(state.kind).toBe('empty')
    expect(state.title).toBe(DETAIL_STRINGS.states.emptyTitle)
    expect(state.hint).toBe(DETAIL_STRINGS.states.emptyHint)
  })

  it('maps known error reasons to friendly text', () => {
    const state = deriveDetailBodyState({
      loading: false,
      error: 'session_not_found',
      entryCount: 0,
    })
    expect(state.kind).toBe('error')
    expect(state.title).toBe(DETAIL_STRINGS.states.errorTitle)
    expect(state.hint).toBe(DETAIL_STRINGS.states.errors.session_not_found)
  })

  it('error outranks loading when both apply and nothing is shown yet', () => {
    const state = deriveDetailBodyState({
      loading: true,
      error: 'network_error',
      entryCount: 0,
    })
    expect(state.kind).toBe('error')
  })

  it('never hides data already shown: entries + error → list with a banner', () => {
    const state = deriveDetailBodyState({
      loading: false,
      error: 'request_timeout',
      entryCount: 3,
    })
    expect(state.kind).toBe('list')
    expect(state.errorBanner).toBe(DETAIL_STRINGS.states.errors.request_timeout)
    expect(state.title).toBeNull()
  })
})

describe('detailErrorText', () => {
  it('falls back to the honest 错误码 template for unknown codes', () => {
    expect(detailErrorText('weird_code')).toBe('错误码:weird_code')
    expect(detailErrorText('invalid_cursor')).toBe(DETAIL_STRINGS.states.errors.invalid_cursor)
  })
})
