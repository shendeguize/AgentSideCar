/**
 * Unit tests for the FusionQuery data-fusion layer (design §4.e, T5.1).
 * Fake dsh event feed, fake sessionQuery, fake store, fake replay seam;
 * no cordis/dsh, no I/O.
 */

import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import {
  DEFAULT_MAX_BUFFERED_EVENTS_PER_SESSION,
  DEFAULT_MAX_BUFFERED_SESSIONS,
  FusionQuery,
  type DshLineageTraceFace,
  type DshSessionEventFace,
  type DshSessionFace,
  type DshEventFace,
  type FusionStoreFace,
  type SessionQueryFace,
  type SidecarEventFace,
  type SidecarReplayFace,
  type SidecarSessionRowFace,
} from '../src/fusion.ts'

/** Base instant: epoch ms (dsh clock) and epoch seconds (sidecar clock). */
const T0 = 1_700_000_000_000
const T0S = T0 / 1000

// ---------------------------------------------------------------------------
// Fakes.
// ---------------------------------------------------------------------------

type SessionHandler = (session: DshSessionFace) => void
type EventHandler = (session: DshSessionFace, ev: DshSessionEventFace) => void

class FakeDshEvents implements DshEventFace {
  readonly created = new Set<SessionHandler>()
  readonly events = new Set<EventHandler>()
  readonly disposed = new Set<SessionHandler>()
  readonly resident = new Map<string, DshSessionFace>()
  readonly getCalls: string[] = []
  disposeCalls = 0

  on(event: 'session/event', handler: EventHandler): () => void
  on(event: 'session/created', handler: SessionHandler): () => void
  on(event: 'session/disposed', handler: SessionHandler): () => void
  on(event: string, handler: EventHandler | SessionHandler): () => void {
    const bucket =
      event === 'session/event'
        ? this.events
        : event === 'session/created'
          ? this.created
          : this.disposed
    bucket.add(handler as EventHandler & SessionHandler)
    return () => {
      this.disposeCalls += 1
      bucket.delete(handler as EventHandler & SessionHandler)
    }
  }

  emitCreated(session: DshSessionFace): void {
    this.resident.set(session.id, session)
    for (const handler of this.created) handler(session)
  }

  emitEvent(session: DshSessionFace, ev: DshSessionEventFace): void {
    for (const handler of this.events) handler(session, ev)
  }

  emitDisposed(session: DshSessionFace): void {
    this.resident.delete(session.id)
    for (const handler of this.disposed) handler(session)
  }

  /** Make a session discoverable through ctx.sessions.get without an event. */
  seed(session: DshSessionFace): void {
    this.resident.set(session.id, session)
  }

  get(sessionId: string): DshSessionFace | undefined {
    this.getCalls.push(sessionId)
    return this.resident.get(sessionId)
  }

  get handlerCount(): number {
    return this.created.size + this.events.size + this.disposed.size
  }
}

interface FakeDshSession {
  session: DshSessionFace
  /** Append to the fake log and return the event (post-commit shape). */
  append(type: string, time: number, data?: unknown): DshSessionEventFace
}

function makeDshSession(
  id: string,
  opts: { cwd?: string; parentSession?: string; createdAt?: number } = {},
): FakeDshSession {
  const events: DshSessionEventFace[] = []
  const header: { createdAt: number; cwd?: string; parentSession?: string } = {
    createdAt: opts.createdAt ?? T0,
  }
  if (opts.cwd !== undefined) header.cwd = opts.cwd
  if (opts.parentSession !== undefined) header.parentSession = opts.parentSession
  const session: DshSessionFace = { id, header, events }
  return {
    session,
    append(type, time, data = {}) {
      const ev: DshSessionEventFace = { type, seq: events.length, time, data }
      events.push(ev)
      return ev
    },
  }
}

function fakeStore(initial: SidecarSessionRowFace[] = []): FusionStoreFace & {
  set(rows: SidecarSessionRowFace[]): void
} {
  let rows = initial
  return {
    getBoardState: () => ({ sessions: rows, streamHealth: 'ok' }),
    set(next) {
      rows = next
    },
  }
}

function sidecarRow(
  over: Partial<SidecarSessionRowFace> & { agent: string; session_id: string },
): SidecarSessionRowFace {
  return {
    status: 'waiting',
    title: '',
    project: '/proj/a',
    updated_at: T0S,
    last_event: null,
    gap: false,
    ...over,
  }
}

function sidecarEvent(
  sessionId: string,
  over: Partial<SidecarEventFace> = {},
): SidecarEventFace {
  return {
    ts: new Date(T0).toISOString(),
    agent: 'dsh',
    session_id: sessionId,
    kind: 'assistant',
    text: 'normalized text',
    extra: {},
    ...over,
  }
}

interface FakeEngine {
  engine: SessionQueryFace
  traceCalls: string[]
  readCalls: string[]
  searchCalls: Array<{ query: string; limit?: number }>
  setTrace(value: DshLineageTraceFace | Error): void
  setLog(events: DshSessionEventFace[] | Error): void
  setHits(hits: Array<{ id: string; snippet: string }> | Error): void
}

function makeTrace(targetId: string): DshLineageTraceFace {
  const record = (id: string) => ({
    header: { id, createdAt: T0 },
    live: false,
    persisted: true,
  })
  return {
    target: record(targetId),
    ancestors: [record('parent-1')],
    descendants: [{ session: record('child-1'), descendants: [] }],
    complete: true,
    root: record('parent-1'),
  }
}

function fakeEngine(): FakeEngine {
  let trace: DshLineageTraceFace | Error = makeTrace('unset')
  let log: DshSessionEventFace[] | Error = []
  let hits: Array<{ id: string; snippet: string }> | Error = []
  const traceCalls: string[] = []
  const readCalls: string[] = []
  const searchCalls: Array<{ query: string; limit?: number }> = []
  return {
    traceCalls,
    readCalls,
    searchCalls,
    setTrace(value) {
      trace = value
    },
    setLog(value) {
      log = value
    },
    setHits(value) {
      hits = value
    },
    engine: {
      async traceSession(sessionId) {
        traceCalls.push(sessionId)
        if (trace instanceof Error) throw trace
        return trace
      },
      async readSession(sessionId) {
        readCalls.push(sessionId)
        if (log instanceof Error) throw log
        return { events: log }
      },
      async searchSessions(request) {
        searchCalls.push(request)
        if (hits instanceof Error) throw hits
        return {
          items: hits.map((hit) => ({
            header: { id: hit.id },
            bestMatch: { seq: 0, snippet: hit.snippet },
          })),
        }
      },
    },
  }
}

interface Harness {
  fusion: FusionQuery
  dshEvents: FakeDshEvents
  store: ReturnType<typeof fakeStore>
}

function makeFusion(
  opts: {
    rows?: SidecarSessionRowFace[]
    dshEvents?: FakeDshEvents | null
    getSessionQuery?: (() => SessionQueryFace | null | undefined) | null
    replay?: SidecarReplayFace | null
    now?: () => number
    maxBufferedEventsPerSession?: number
    maxBufferedSessions?: number
  } = {},
): Harness {
  const store = fakeStore(opts.rows ?? [])
  const dshEvents = opts.dshEvents === undefined ? new FakeDshEvents() : opts.dshEvents
  const fusion = new FusionQuery({
    store,
    dshEvents,
    getSessionQuery: opts.getSessionQuery ?? null,
    replay: opts.replay ?? null,
    now: opts.now ?? (() => T0),
    ...(opts.maxBufferedEventsPerSession !== undefined
      ? { maxBufferedEventsPerSession: opts.maxBufferedEventsPerSession }
      : {}),
    ...(opts.maxBufferedSessions !== undefined
      ? { maxBufferedSessions: opts.maxBufferedSessions }
      : {}),
  })
  fusion.start()
  return { fusion, dshEvents: dshEvents ?? new FakeDshEvents(), store }
}

// ---------------------------------------------------------------------------
// Construction.
// ---------------------------------------------------------------------------

describe('construction', () => {
  it('rejects invalid buffer bounds', () => {
    const store = fakeStore()
    expect(
      () => new FusionQuery({ store, maxBufferedEventsPerSession: 0 }),
    ).toThrow(RangeError)
    expect(() => new FusionQuery({ store, maxBufferedSessions: -1 })).toThrow(RangeError)
  })

  it('exposes sane default bounds', () => {
    expect(DEFAULT_MAX_BUFFERED_EVENTS_PER_SESSION).toBeGreaterThan(0)
    expect(DEFAULT_MAX_BUFFERED_SESSIONS).toBeGreaterThan(0)
  })
})

// ---------------------------------------------------------------------------
// Dedup and primary-source rules.
// ---------------------------------------------------------------------------

describe('getUnifiedSessions dedup', () => {
  it('merges a live dsh session over its sidecar row (dsh primary, sidecar supplement)', () => {
    const row = sidecarRow({
      agent: 'dsh',
      session_id: 'sess-1',
      title: 'stale disk title',
      project: '/proj/from-disk',
      status: 'working',
      updated_at: T0S,
      extra: { stats: { tokens: 42 }, seq: 3 },
      parent_id: null,
      last_event: { ts: new Date(T0).toISOString(), kind: 'assistant', text: 'summary' },
      gap: true,
    })
    const { fusion, dshEvents } = makeFusion({ rows: [row] })
    const fake = makeDshSession('sess-1', { cwd: '/proj/live', createdAt: T0 })
    dshEvents.emitCreated(fake.session)
    dshEvents.emitEvent(fake.session, fake.append('turn/start', T0 + 5_000, { turn: 1 }))
    dshEvents.emitEvent(
      fake.session,
      fake.append('session/title', T0 + 6_000, { title: 'live title' }),
    )

    const sessions = fusion.getUnifiedSessions()
    expect(sessions).toHaveLength(1)
    const merged = sessions[0]!
    expect(merged.origin).toBe('merged')
    expect(merged.live).toBe(true)
    // dsh in-process facts win…
    expect(merged.title).toBe('live title')
    expect(merged.project).toBe('/proj/live')
    expect(merged.lastSeq).toBe(1)
    expect(merged.lastActivityAt).toBe(T0 + 6_000)
    // …while sidecar-only facts supplement.
    expect(merged.status).toBe('working')
    expect(merged.extra).toEqual({ stats: { tokens: 42 }, seq: 3 })
    expect(merged.lastEvent?.text).toBe('summary')
    expect(merged.gap).toBe(true)
  })

  it('falls back to the sidecar row when the dsh session is not live (cold read)', () => {
    const row = sidecarRow({
      agent: 'dsh',
      session_id: 'sess-cold',
      title: 'historic session',
      extra: { seq: 9 },
    })
    const { fusion } = makeFusion({ rows: [row] })
    const sessions = fusion.getUnifiedSessions()
    expect(sessions).toHaveLength(1)
    expect(sessions[0]!.origin).toBe('sidecar')
    expect(sessions[0]!.live).toBe(false)
    expect(sessions[0]!.title).toBe('historic session')
    expect(sessions[0]!.lastSeq).toBe(9)
  })

  it('reverts to the sidecar row after session/disposed', () => {
    const row = sidecarRow({ agent: 'dsh', session_id: 'sess-1', title: 'disk' })
    const { fusion, dshEvents } = makeFusion({ rows: [row] })
    const fake = makeDshSession('sess-1')
    dshEvents.emitCreated(fake.session)
    expect(fusion.getUnifiedSessions()[0]!.origin).toBe('merged')
    dshEvents.emitDisposed(fake.session)
    const after = fusion.getUnifiedSessions()
    expect(after).toHaveLength(1)
    expect(after[0]!.origin).toBe('sidecar')
    expect(after[0]!.title).toBe('disk')
  })

  it('keeps non-dsh agents on the sidecar source and never merges them by id', () => {
    const rows = [
      sidecarRow({ agent: 'claude', session_id: 'shared-id', title: 'claude one' }),
      sidecarRow({ agent: 'cursor-ide', session_id: 'sess-c', title: 'cursor one' }),
    ]
    const { fusion, dshEvents } = makeFusion({ rows })
    // A dsh live session that coincidentally reuses a non-dsh id must not merge.
    const fake = makeDshSession('shared-id', { cwd: '/proj/x' })
    dshEvents.emitCreated(fake.session)

    const sessions = fusion.getUnifiedSessions()
    expect(sessions).toHaveLength(3)
    const claude = sessions.find((s) => s.agent === 'claude')!
    expect(claude.origin).toBe('sidecar')
    expect(claude.live).toBe(false)
    const dsh = sessions.find((s) => s.agent === 'dsh')!
    expect(dsh.origin).toBe('dsh-live')
  })

  it('exposes a live dsh session unknown to the sidecar as dsh-live with honest status', () => {
    const { fusion, dshEvents } = makeFusion({ rows: [] })
    const fake = makeDshSession('sess-new', {
      cwd: '/proj/new',
      parentSession: 'sess-parent',
      createdAt: T0,
    })
    dshEvents.emitCreated(fake.session)
    dshEvents.emitEvent(fake.session, fake.append('turn/start', T0 + 1_000, { turn: 1 }))

    const sessions = fusion.getUnifiedSessions()
    expect(sessions).toHaveLength(1)
    const live = sessions[0]!
    expect(live.origin).toBe('dsh-live')
    expect(live.status).toBe('unknown')
    expect(live.project).toBe('/proj/new')
    expect(live.parentId).toBe('sess-parent')
    expect(live.lastActivityAt).toBe(T0 + 1_000)
    expect(live.lastSeq).toBe(0)
  })

  it('takes the freshest activity signal from either source', () => {
    const newerSidecar = sidecarRow({
      agent: 'dsh',
      session_id: 'sess-1',
      updated_at: T0S + 100, // sidecar 100s fresher than the dsh event
    })
    const { fusion, dshEvents } = makeFusion({ rows: [newerSidecar] })
    const fake = makeDshSession('sess-1')
    dshEvents.emitCreated(fake.session)
    dshEvents.emitEvent(fake.session, fake.append('turn/start', T0 + 5_000, {}))
    expect(fusion.getUnifiedSessions()[0]!.lastActivityAt).toBe((T0S + 100) * 1000)
  })

  it('auto-registers a session first seen through session/event (feed attached late)', () => {
    const { fusion, dshEvents } = makeFusion()
    const fake = makeDshSession('sess-late')
    fake.append('turn/start', T0 + 1_000, {})
    fake.append('session/title', T0 + 2_000, { title: 'from log fold' })
    const ev = fake.append('turn/end', T0 + 3_000, { turn: 1, reason: 'completed' })
    dshEvents.emitEvent(fake.session, ev) // no session/created ever emitted

    const sessions = fusion.getUnifiedSessions()
    expect(sessions).toHaveLength(1)
    expect(sessions[0]!.title).toBe('from log fold')
    expect(sessions[0]!.lastSeq).toBe(2)
  })

  it('handles the empty case', () => {
    const { fusion } = makeFusion()
    expect(fusion.getUnifiedSessions()).toEqual([])
    expect(fusion.getProjectGroups()).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Timeline merge.
// ---------------------------------------------------------------------------

describe('getSessionTimeline', () => {
  it('merges dsh and sidecar events, deduplicating by seq with sidecar supplement', async () => {
    const { fusion, dshEvents } = makeFusion()
    const fake = makeDshSession('sess-1')
    dshEvents.emitCreated(fake.session)
    for (let i = 0; i < 4; i += 1) {
      dshEvents.emitEvent(fake.session, fake.append(`native/${i}`, T0 + i * 1_000, { i }))
    }
    // Sidecar twins for seq 1 and 2 (must merge into the dsh entries)…
    fusion.ingestSidecarEvent(
      sidecarEvent('sess-1', {
        ts: new Date(T0 + 1_000).toISOString(),
        kind: 'assistant',
        text: 'norm-1',
        extra: { seq: 1, native_type: 'native/1' },
      }),
    )
    fusion.ingestSidecarEvent(
      sidecarEvent('sess-1', {
        ts: new Date(T0 + 2_000).toISOString(),
        kind: 'tool_call',
        text: 'norm-2',
        extra: { seq: 2 },
      }),
    )
    // …and one seq-less sidecar event interleaved by timestamp.
    fusion.ingestSidecarEvent(
      sidecarEvent('sess-1', {
        ts: new Date(T0 + 2_500).toISOString(),
        kind: 'status',
        text: 'status changed',
        extra: {},
      }),
    )

    const page = await fusion.getSessionTimeline('sess-1')
    expect(page.sources).toEqual({
      dshLive: true,
      dshCold: false,
      sidecarReplay: false,
      sidecarBuffer: true,
    })
    expect(page.entries.map((e) => [e.origin, e.seq, e.kind])).toEqual([
      ['dsh', 0, 'native/0'],
      ['dsh', 1, 'native/1'],
      ['dsh', 2, 'native/2'],
      ['sidecar', null, 'status'],
      ['dsh', 3, 'native/3'],
    ])
    // Deduplicated twins supplement the dsh entries with normalized text/extra.
    const merged1 = page.entries[1]!
    expect(merged1.text).toBe('norm-1')
    expect(merged1.extra).toEqual({ seq: 1, native_type: 'native/1' })
    expect(merged1.data).toEqual({ i: 1 })
    // Entries without a twin keep an empty text.
    expect(page.entries[0]!.text).toBe('')
    expect(page.cursor).toBeNull()
  })

  it('paginates backwards with a cursor', async () => {
    const { fusion, dshEvents } = makeFusion()
    const fake = makeDshSession('sess-1')
    dshEvents.emitCreated(fake.session)
    for (let i = 0; i < 5; i += 1) {
      dshEvents.emitEvent(fake.session, fake.append(`e/${i}`, T0 + i * 1_000, {}))
    }

    const page1 = await fusion.getSessionTimeline('sess-1', { limit: 2 })
    expect(page1.entries.map((e) => e.seq)).toEqual([3, 4])
    expect(page1.cursor).toEqual({ seq: 3, ts: T0 + 3_000 })

    const page2 = await fusion.getSessionTimeline('sess-1', {
      limit: 2,
      before: page1.cursor,
    })
    expect(page2.entries.map((e) => e.seq)).toEqual([1, 2])

    const page3 = await fusion.getSessionTimeline('sess-1', {
      limit: 2,
      before: page2.cursor,
    })
    expect(page3.entries.map((e) => e.seq)).toEqual([0])
    expect(page3.cursor).toBeNull()
  })

  it('late-binds one resident live session on demand without a global scan', async () => {
    const dshEvents = new FakeDshEvents()
    const fake = makeDshSession('sess-preexisting')
    fake.append('user/message', T0 + 1_000, { content: 'already live' })
    dshEvents.seed(fake.session)
    const { fusion } = makeFusion({ dshEvents })

    expect(fusion.getUnifiedSessions()).toEqual([])
    const page = await fusion.getSessionTimeline('sess-preexisting')

    expect(dshEvents.getCalls).toEqual(['sess-preexisting'])
    expect(page.entries.map((entry) => entry.kind)).toEqual(['user/message'])
    expect(page.sourceOutcomes.liveSession).toBe('succeeded')
    expect(fusion.getUnifiedSessions()[0]?.sessionId).toBe('sess-preexisting')
  })

  it('cold-reads a non-live dsh session through sessionQuery.readSession', async () => {
    const engine = fakeEngine()
    engine.setLog([
      { type: 'turn/start', seq: 0, time: T0, data: {} },
      { type: 'turn/end', seq: 1, time: T0 + 1_000, data: {} },
    ])
    const { fusion } = makeFusion({ getSessionQuery: () => engine.engine })
    const page = await fusion.getSessionTimeline('sess-cold')
    expect(engine.readCalls).toEqual(['sess-cold'])
    expect(page.sources.dshCold).toBe(true)
    expect(page.sources.dshLive).toBe(false)
    expect(page.entries.map((e) => e.seq)).toEqual([0, 1])
  })

  it('degrades to sidecar-only when sessionQuery is absent', async () => {
    const { fusion } = makeFusion()
    fusion.ingestSidecarEvent(
      sidecarEvent('sess-cold', { text: 'only source', extra: { seq: 0 } }),
    )
    const page = await fusion.getSessionTimeline('sess-cold')
    expect(page.sources.dshCold).toBe(false)
    expect(page.entries).toHaveLength(1)
    expect(page.entries[0]!.origin).toBe('sidecar')
    expect(page.entries[0]!.seq).toBe(0)
  })

  it('degrades to sidecar-only when readSession fails', async () => {
    const engine = fakeEngine()
    engine.setLog(new Error('unknown session'))
    const { fusion } = makeFusion({ getSessionQuery: () => engine.engine })
    fusion.ingestSidecarEvent(sidecarEvent('sess-x', { text: 'survivor' }))
    const page = await fusion.getSessionTimeline('sess-x')
    expect(page.sources.dshCold).toBe(false)
    expect(page.entries.map((e) => e.text)).toEqual(['survivor'])
  })

  it('reports a redacted partial failure while preserving successful entries', async () => {
    const secret = 'PROMPT-SECRET /Users/private/session-id-77'
    const engine = fakeEngine()
    engine.setLog(new Error(`permission denied: ${secret}`))
    const { fusion } = makeFusion({ getSessionQuery: () => engine.engine })
    fusion.ingestSidecarEvent(sidecarEvent('sess-x', { text: 'safe survivor' }))

    const page = await fusion.getSessionTimeline('sess-x')

    expect(page.entries.map((entry) => entry.text)).toEqual(['safe survivor'])
    expect(page.sourceOutcomes.sessionQuery).toBe('source_failed')
    expect(page.sourceOutcomes.buffer).toBe('succeeded')
    expect(page.degraded).toBe(true)
    expect(page.reason).toBe('partial_source_failure')
    expect(JSON.stringify(page)).not.toContain(secret)
  })

  it('distinguishes all-source failure from a healthy empty timeline', async () => {
    const engine = fakeEngine()
    engine.setLog(new Error('storage failed: /private/log/session-42'))
    const failingReplay: SidecarReplayFace = {
      replay: async () => {
        throw new Error('socket failed for id=session-42 prompt=private')
      },
    }
    const failed = await makeFusion({
      getSessionQuery: () => engine.engine,
      replay: failingReplay,
    }).fusion.getSessionTimeline('sess-empty')
    expect(failed.entries).toEqual([])
    expect(failed.sourceOutcomes).toEqual({
      liveSession: 'not_found',
      sessionQuery: 'source_failed',
      sidecarReplay: 'source_failed',
      buffer: 'not_found',
    })
    expect(failed.degraded).toBe(true)
    expect(failed.reason).toBe('all_sources_failed')
    expect(JSON.stringify(failed)).not.toMatch(/private|session-42|prompt/i)

    engine.setLog([])
    const healthyReplay: SidecarReplayFace = { replay: async () => [] }
    const healthy = await makeFusion({
      getSessionQuery: () => engine.engine,
      replay: healthyReplay,
    }).fusion.getSessionTimeline('sess-empty')
    expect(healthy.entries).toEqual([])
    expect(healthy.sourceOutcomes.sessionQuery).toBe('succeeded')
    expect(healthy.sourceOutcomes.sidecarReplay).toBe('succeeded')
    expect(healthy.degraded).toBe(false)
    expect(healthy.reason).toBeNull()
  })

  it('prefers the replay seam and deduplicates the ring against it', async () => {
    const replayCalls: string[] = []
    const replay: SidecarReplayFace = {
      async replay({ sessionId }) {
        replayCalls.push(sessionId)
        return [
          sidecarEvent('sess-1', { text: 'replayed-0', extra: { seq: 0 } }),
          sidecarEvent('sess-1', {
            ts: new Date(T0 + 1_000).toISOString(),
            text: 'replayed-1',
            extra: { seq: 1 },
          }),
        ]
      },
    }
    const { fusion } = makeFusion({ replay })
    // The ring holds the SAME normalized event as replay page seq 1 (both
    // channels normalize one record identically: same kind, same text) —
    // it must not appear twice.
    fusion.ingestSidecarEvent(
      sidecarEvent('sess-1', {
        ts: new Date(T0 + 1_000).toISOString(),
        text: 'replayed-1',
        extra: { seq: 1 },
      }),
    )
    const page = await fusion.getSessionTimeline('sess-1')
    expect(replayCalls).toEqual(['sess-1'])
    expect(page.sources.sidecarReplay).toBe(true)
    expect(page.entries.map((e) => [e.seq, e.text])).toEqual([
      [0, 'replayed-0'],
      [1, 'replayed-1'],
    ])
  })

  it('keeps synthetic queue and steer digests on the newest page', async () => {
    const queueMarker = 'synthetic queued marker'
    const steerMarker = 'synthetic steered marker'
    const digest = (text: string): string =>
      createHash('sha256').update(text, 'utf8').digest('hex')
    const events: SidecarEventFace[] = []
    for (let seq = 0; seq < 106; seq += 1) {
      events.push(sidecarEvent('sess-inject', { text: `old-${seq}`, extra: { seq } }))
    }
    events.push(
      sidecarEvent('sess-inject', {
        kind: 'user',
        text: queueMarker,
        extra: { seq: 106, source: { kind: 'plugin', plugin: 'agent-sidecar' } },
      }),
    )
    for (let seq = 107; seq < 138; seq += 1) {
      events.push(sidecarEvent('sess-inject', { text: `between-${seq}`, extra: { seq } }))
    }
    events.push(
      sidecarEvent('sess-inject', {
        kind: 'user',
        text: steerMarker,
        extra: { seq: 138, source: { kind: 'plugin', plugin: 'agent-sidecar' } },
      }),
    )
    for (let seq = 139; seq < 144; seq += 1) {
      events.push(sidecarEvent('sess-inject', { text: `tail-${seq}`, extra: { seq } }))
    }
    const replay: SidecarReplayFace = {
      replay: async () => events,
    }
    const { fusion } = makeFusion({ replay })

    const page = await fusion.getSessionTimeline('sess-inject')
    const newestDigests = new Set(page.entries.map((entry) => digest(entry.text)))

    expect(page.entries).toHaveLength(100)
    expect(newestDigests).toContain(digest(queueMarker))
    expect(newestDigests).toContain(digest(steerMarker))
    expect(page.cursor?.seq).toBe(44)
  })

  // ------------------------------------------------------- F1 regressions
  // One dsh record legally normalizes into SEVERAL sidecar events sharing
  // one extra.seq (assistant reasoning+text blocks, multi-block messages,
  // spliced inserts). A seq-only dedup key silently dropped the siblings.

  it('keeps all same-seq sibling events (reasoning+text blocks) in block order', async () => {
    // Sidecar is the only source (dsh cold, sessionQuery absent): the
    // path where a dropped sibling would be unrecoverable.
    const replay: SidecarReplayFace = {
      async replay() {
        return [
          sidecarEvent('sess-1', { kind: 'user', text: 'question', extra: { seq: 4 } }),
          sidecarEvent('sess-1', {
            ts: new Date(T0 + 1_000).toISOString(),
            kind: 'thinking',
            text: 'let me think',
            extra: { seq: 5 },
          }),
          sidecarEvent('sess-1', {
            ts: new Date(T0 + 1_000).toISOString(),
            kind: 'assistant',
            text: 'the answer',
            extra: { seq: 5 },
          }),
        ]
      },
    }
    const { fusion } = makeFusion({ replay })
    const page = await fusion.getSessionTimeline('sess-1')
    // Both seq-5 siblings survive, in block order, between seq 4 and the end.
    expect(page.entries.map((e) => [e.seq, e.kind, e.text])).toEqual([
      [4, 'user', 'question'],
      [5, 'thinking', 'let me think'],
      [5, 'assistant', 'the answer'],
    ])
  })

  it('still deduplicates the identical event across replay and ring, keeping distinct siblings', async () => {
    const twin = (kind: string, text: string): SidecarEventFace =>
      sidecarEvent('sess-1', {
        ts: new Date(T0 + 1_000).toISOString(),
        kind,
        text,
        extra: { seq: 7 },
      })
    const replay: SidecarReplayFace = {
      async replay() {
        return [twin('thinking', 'pondering'), twin('assistant', 'done')]
      },
    }
    const { fusion } = makeFusion({ replay })
    // The ring re-delivers both events (same seq, kind, text): true dups.
    fusion.ingestSidecarEvent(twin('thinking', 'pondering'))
    fusion.ingestSidecarEvent(twin('assistant', 'done'))
    const page = await fusion.getSessionTimeline('sess-1')
    expect(page.entries.map((e) => [e.seq, e.kind, e.text])).toEqual([
      [7, 'thinking', 'pondering'],
      [7, 'assistant', 'done'],
    ])
  })

  it('folds the first same-seq twin into the dsh entry and keeps the siblings as entries', async () => {
    const { fusion, dshEvents } = makeFusion()
    const fake = makeDshSession('sess-1')
    dshEvents.emitCreated(fake.session)
    dshEvents.emitEvent(fake.session, fake.append('user/message', T0, { blocks: 1 }))
    dshEvents.emitEvent(fake.session, fake.append('assistant/message', T0 + 1_000, { blocks: 2 }))
    fusion.ingestSidecarEvent(
      sidecarEvent('sess-1', {
        ts: new Date(T0 + 1_000).toISOString(),
        kind: 'thinking',
        text: 'reasoning block',
        extra: { seq: 1 },
      }),
    )
    fusion.ingestSidecarEvent(
      sidecarEvent('sess-1', {
        ts: new Date(T0 + 1_000).toISOString(),
        kind: 'assistant',
        text: 'text block',
        extra: { seq: 1 },
      }),
    )
    const page = await fusion.getSessionTimeline('sess-1')
    expect(page.entries.map((e) => [e.origin, e.seq, e.kind, e.text])).toEqual([
      ['dsh', 0, 'user/message', ''],
      // dsh entry stays primary, supplemented by the FIRST twin…
      ['dsh', 1, 'assistant/message', 'reasoning block'],
      // …and the second block is NOT silently dropped.
      ['sidecar', 1, 'assistant', 'text block'],
    ])
    expect(page.entries[1]!.data).toEqual({ blocks: 2 })
  })

  it('never splits a same-seq sibling group across page boundaries', async () => {
    const sib = (seq: number, kind: string, text: string): SidecarEventFace =>
      sidecarEvent('sess-1', {
        ts: new Date(T0 + seq * 1_000).toISOString(),
        kind,
        text,
        extra: { seq },
      })
    const replay: SidecarReplayFace = {
      async replay() {
        return [
          sib(1, 'user', 'q'),
          sib(2, 'thinking', 'a'),
          sib(2, 'assistant', 'b'),
          sib(2, 'assistant', 'c'),
          sib(3, 'tool_call', 'x'),
        ]
      },
    }
    const { fusion } = makeFusion({ replay })

    // limit 2 from the newest end would cut inside the seq-2 group: the
    // window widens to keep the group whole (bounded overshoot).
    const page1 = await fusion.getSessionTimeline('sess-1', { limit: 2 })
    expect(page1.entries.map((e) => [e.seq, e.text])).toEqual([
      [2, 'a'],
      [2, 'b'],
      [2, 'c'],
      [3, 'x'],
    ])
    expect(page1.cursor).toEqual({ seq: 2, ts: T0 + 2_000 })

    // The older page picks up exactly the rest: nothing lost, nothing twice.
    const page2 = await fusion.getSessionTimeline('sess-1', {
      limit: 2,
      before: page1.cursor,
    })
    expect(page2.entries.map((e) => [e.seq, e.text])).toEqual([[1, 'q']])
    expect(page2.cursor).toBeNull()
  })

  it('keeps the per-session ring bounded (oldest dropped)', async () => {
    const { fusion } = makeFusion({ maxBufferedEventsPerSession: 3 })
    for (let i = 0; i < 5; i += 1) {
      fusion.ingestSidecarEvent(
        sidecarEvent('sess-1', {
          ts: new Date(T0 + i * 1_000).toISOString(),
          text: `ev-${i}`,
          extra: { seq: i },
        }),
      )
    }
    const page = await fusion.getSessionTimeline('sess-1')
    expect(page.entries.map((e) => e.text)).toEqual(['ev-2', 'ev-3', 'ev-4'])
  })

  it('evicts the least-recently-fed session ring beyond the session bound', async () => {
    const { fusion } = makeFusion({ maxBufferedSessions: 2 })
    fusion.ingestSidecarEvent(sidecarEvent('sess-a', { text: 'a' }))
    fusion.ingestSidecarEvent(sidecarEvent('sess-b', { text: 'b' }))
    fusion.ingestSidecarEvent(sidecarEvent('sess-a', { text: 'a2' })) // refresh a
    fusion.ingestSidecarEvent(sidecarEvent('sess-c', { text: 'c' })) // evicts b
    expect((await fusion.getSessionTimeline('sess-b')).entries).toEqual([])
    expect((await fusion.getSessionTimeline('sess-a')).entries).toHaveLength(2)
    expect((await fusion.getSessionTimeline('sess-c')).entries).toHaveLength(1)
  })

  it('returns an empty page for an unknown session', async () => {
    const { fusion } = makeFusion()
    const page = await fusion.getSessionTimeline('missing')
    expect(page.entries).toEqual([])
    expect(page.cursor).toBeNull()
    expect(page.sources).toEqual({
      dshLive: false,
      dshCold: false,
      sidecarReplay: false,
      sidecarBuffer: false,
    })
    expect(page.sourceOutcomes).toEqual({
      liveSession: 'not_found',
      sessionQuery: 'unavailable',
      sidecarReplay: 'unavailable',
      buffer: 'not_found',
    })
    expect(page.degraded).toBe(false)
    expect(page.reason).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// Project correlation groups.
// ---------------------------------------------------------------------------

describe('getProjectGroups', () => {
  it('groups sessions of different agents under one normalized project', () => {
    const rows = [
      sidecarRow({ agent: 'claude', session_id: 's-c', project: '/proj/a', updated_at: T0S }),
      // Trailing slash must normalize into the same group.
      sidecarRow({
        agent: 'cursor-ide',
        session_id: 's-i',
        project: '/proj/a/',
        updated_at: T0S - 60,
      }),
    ]
    const { fusion, dshEvents } = makeFusion({ rows })
    const fake = makeDshSession('s-d', { cwd: '/proj/a' })
    dshEvents.emitCreated(fake.session)
    dshEvents.emitEvent(fake.session, fake.append('turn/start', T0 + 1_000, {}))

    const groups = fusion.getProjectGroups({ now: T0 + 1_000 })
    expect(groups).toHaveLength(1)
    const group = groups[0]!
    expect(group.project).toBe('/proj/a')
    expect(group.agents).toEqual(['claude', 'cursor-ide', 'dsh'])
    expect(group.sessions.map((s) => s.agent)).toEqual(['dsh', 'claude', 'cursor-ide'])
    expect(group.lastActivityAt).toBe(T0 + 1_000)
  })

  it('excludes sessions outside the time window', () => {
    const rows = [
      sidecarRow({ agent: 'claude', session_id: 's-new', updated_at: T0S }),
      sidecarRow({
        agent: 'codex',
        session_id: 's-old',
        project: '/proj/old',
        updated_at: T0S - 60 * 60 * 48, // 48h stale
      }),
    ]
    const { fusion } = makeFusion({ rows })
    const groups = fusion.getProjectGroups({ now: T0 })
    expect(groups).toHaveLength(1)
    expect(groups[0]!.sessions.map((s) => s.sessionId)).toEqual(['s-new'])
  })

  it('sorts groups by recency', () => {
    const rows = [
      sidecarRow({ agent: 'claude', session_id: 's-1', project: '/proj/a', updated_at: T0S - 100 }),
      sidecarRow({ agent: 'codex', session_id: 's-2', project: '/proj/b', updated_at: T0S }),
    ]
    const { fusion } = makeFusion({ rows })
    const groups = fusion.getProjectGroups({ now: T0 })
    expect(groups.map((g) => g.project)).toEqual(['/proj/b', '/proj/a'])
  })
})

// ---------------------------------------------------------------------------
// sessionQuery lazy degradation.
// ---------------------------------------------------------------------------

describe('sessionQuery degradation', () => {
  it('degrades getLineage and capabilities when the service is absent', async () => {
    const { fusion } = makeFusion()
    const lineage = await fusion.getLineage('sess-1')
    expect(lineage).toEqual({
      available: false,
      trace: null,
      reason: 'session_query_unavailable',
    })
    const caps = fusion.getCapabilities()
    expect(caps.sessionQuery.available).toBe(false)
    expect(caps.sessionQuery.reason).toBe('session_query_unavailable')
    expect(caps.search.mode).toBe('filter-only')
  })

  it('returns the lineage trace when the service is mounted', async () => {
    const engine = fakeEngine()
    engine.setTrace(makeTrace('sess-1'))
    const { fusion } = makeFusion({ getSessionQuery: () => engine.engine })
    const lineage = await fusion.getLineage('sess-1')
    expect(engine.traceCalls).toEqual(['sess-1'])
    expect(lineage.available).toBe(true)
    expect(lineage.reason).toBeNull()
    expect(lineage.trace?.target.header.id).toBe('sess-1')
    expect(lineage.trace?.ancestors).toHaveLength(1)
    const caps = fusion.getCapabilities()
    expect(caps.sessionQuery).toEqual({ available: true, reason: null })
    expect(caps.search.mode).toBe('full-text')
  })

  it('degrades getLineage when traceSession throws', async () => {
    const engine = fakeEngine()
    engine.setTrace(new Error('unknown ancestry'))
    const { fusion } = makeFusion({ getSessionQuery: () => engine.engine })
    const lineage = await fusion.getLineage('sess-1')
    expect(lineage.available).toBe(false)
    expect(lineage.trace).toBeNull()
    expect(lineage.reason).toBe('trace_failed')
    expect(lineage.detail).toBe('unknown ancestry')
  })

  it('re-resolves the service lazily on every use (late mount, no restart)', async () => {
    const engine = fakeEngine()
    engine.setTrace(makeTrace('sess-1'))
    let mounted: SessionQueryFace | undefined
    const { fusion } = makeFusion({ getSessionQuery: () => mounted })
    expect(fusion.getCapabilities().sessionQuery.available).toBe(false)
    expect((await fusion.getLineage('sess-1')).available).toBe(false)
    mounted = engine.engine
    expect(fusion.getCapabilities().sessionQuery.available).toBe(true)
    expect((await fusion.getLineage('sess-1')).available).toBe(true)
  })

  it('treats a throwing resolver as an absent service', async () => {
    const { fusion } = makeFusion({
      getSessionQuery: () => {
        throw new Error('context disposed')
      },
    })
    expect(fusion.getCapabilities().sessionQuery.available).toBe(false)
    expect((await fusion.getLineage('x')).reason).toBe('session_query_unavailable')
  })
})

// ---------------------------------------------------------------------------
// Search (deep-query degradation surface).
// ---------------------------------------------------------------------------

describe('searchSessions', () => {
  const rows = [
    sidecarRow({
      agent: 'claude',
      session_id: 's-c',
      title: 'Fix the Parser bug',
      project: '/proj/parser',
    }),
    sidecarRow({
      agent: 'dsh',
      session_id: 's-d',
      title: 'refactor session store',
      project: '/proj/other',
    }),
    sidecarRow({
      agent: 'codex',
      session_id: 's-x',
      title: 'unrelated',
      project: '/work/parser-tools',
    }),
  ]

  it('filters by title/project (case-insensitive) without sessionQuery', async () => {
    const { fusion } = makeFusion({ rows })
    const result = await fusion.searchSessions('parser')
    expect(result.mode).toBe('filter-only')
    expect(result.items.map((i) => [i.session.sessionId, i.matchedBy])).toEqual([
      ['s-c', 'title'],
      ['s-x', 'project'],
    ])
    expect(result.items[0]!.snippet).toBeNull()
  })

  it('ranks full-text hits first and deduplicates local matches', async () => {
    const engine = fakeEngine()
    engine.setHits([{ id: 's-d', snippet: '…parser deep hit…' }])
    const { fusion } = makeFusion({ rows, getSessionQuery: () => engine.engine })
    const result = await fusion.searchSessions('parser', { limit: 10 })
    expect(result.mode).toBe('full-text')
    expect(engine.searchCalls).toEqual([{ query: 'parser', limit: 10 }])
    expect(result.items.map((i) => [i.session.sessionId, i.matchedBy])).toEqual([
      ['s-d', 'full-text'],
      ['s-c', 'title'],
      ['s-x', 'project'],
    ])
    expect(result.items[0]!.snippet).toBe('…parser deep hit…')
  })

  it('falls back to filter-only when the engine search fails', async () => {
    const engine = fakeEngine()
    engine.setHits(new Error('index rebuilding'))
    const { fusion } = makeFusion({ rows, getSessionQuery: () => engine.engine })
    const result = await fusion.searchSessions('parser')
    expect(result.mode).toBe('filter-only')
    expect(result.items.map((i) => i.session.sessionId)).toEqual(['s-c', 's-x'])
  })

  it('skips engine hits that have no unified counterpart', async () => {
    const engine = fakeEngine()
    engine.setHits([{ id: 'ghost', snippet: 'gone' }])
    const { fusion } = makeFusion({ rows, getSessionQuery: () => engine.engine })
    const result = await fusion.searchSessions('parser')
    expect(result.items.every((i) => i.session.sessionId !== 'ghost')).toBe(true)
  })

  it('returns no items (and calls no engine) for an empty query', async () => {
    const engine = fakeEngine()
    const { fusion } = makeFusion({ rows, getSessionQuery: () => engine.engine })
    const result = await fusion.searchSessions('   ')
    expect(result.items).toEqual([])
    expect(engine.searchCalls).toEqual([])
  })

  it('applies the result limit', async () => {
    const { fusion } = makeFusion({ rows })
    const result = await fusion.searchSessions('parser', { limit: 1 })
    expect(result.items).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
// Lifecycle and capabilities.
// ---------------------------------------------------------------------------

describe('lifecycle', () => {
  it('reports the dsh feed and live-session count in capabilities', () => {
    const { fusion, dshEvents } = makeFusion()
    expect(fusion.getCapabilities().dshEvents).toEqual({ available: true, liveSessions: 0 })
    dshEvents.emitCreated(makeDshSession('a').session)
    dshEvents.emitCreated(makeDshSession('b').session)
    expect(fusion.getCapabilities().dshEvents.liveSessions).toBe(2)
  })

  it('marks the dsh feed unavailable when not injected (sidecar-only fusion)', () => {
    const row = sidecarRow({ agent: 'claude', session_id: 's-1' })
    const store = fakeStore([row])
    const fusion = new FusionQuery({ store, dshEvents: null })
    fusion.start()
    expect(fusion.getCapabilities().dshEvents).toEqual({ available: false, liveSessions: 0 })
    expect(fusion.getUnifiedSessions()).toHaveLength(1)
  })

  it('stop() disposes the subscriptions and drops cached state', async () => {
    const { fusion, dshEvents } = makeFusion()
    const fake = makeDshSession('sess-1')
    dshEvents.emitCreated(fake.session)
    fusion.ingestSidecarEvent(sidecarEvent('sess-1'))
    expect(dshEvents.handlerCount).toBe(3)

    fusion.stop()
    expect(dshEvents.disposeCalls).toBe(3)
    expect(dshEvents.handlerCount).toBe(0)
    expect(fusion.getCapabilities().dshEvents.liveSessions).toBe(0)
    expect((await fusion.getSessionTimeline('sess-1')).entries).toEqual([])
    // Idempotent.
    fusion.stop()
    expect(dshEvents.disposeCalls).toBe(3)
  })

  it('start() is idempotent and can re-subscribe after stop()', () => {
    const { fusion, dshEvents } = makeFusion()
    fusion.start() // second call: no duplicate handlers
    expect(dshEvents.handlerCount).toBe(3)
    fusion.stop()
    fusion.start()
    expect(dshEvents.handlerCount).toBe(3)
    dshEvents.emitCreated(makeDshSession('again').session)
    expect(fusion.getCapabilities().dshEvents.liveSessions).toBe(1)
  })
})
