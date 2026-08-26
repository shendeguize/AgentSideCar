/**
 * Unit tests for the session-detail glue store (client/detail-glue.ts):
 * initial load, pagination, listen-mode coalescing, lineage degradation
 * routing, header derivation, and dispose semantics. Node environment
 * with injected transports; no real network.
 */

import { describe, expect, it } from 'vitest'
import { ApiError } from '../src/client/api.ts'
import {
  DetailStore,
  findCardHint,
  headerFromDetailWire,
  type DetailHeaderHint,
} from '../src/client/detail-glue.ts'
import type { SessionDetailWire, UnifiedSessionWire } from '../src/client/detail/transport.ts'
import type { TimelinePageWire } from '../src/client/detail/logic.ts'
import type { LineageResponseVM } from '../src/client/dsh-tools/logic.ts'

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

const HINT: DetailHeaderHint = {
  agent: 'dsh', title: 'demo', project: '/p', status: 'working',
}

function unified(overrides: Partial<UnifiedSessionWire> = {}): UnifiedSessionWire {
  return {
    agent: 'dsh',
    sessionId: 's1',
    origin: 'dsh-live',
    live: true,
    status: 'working',
    title: 'fused title',
    project: '/proj',
    lastActivityAt: 1_724_000_000_000,
    lastEvent: null,
    lastSeq: 3,
    gap: false,
    parentId: null,
    extra: {},
    ...overrides,
  }
}

function page(
  entries: Array<{ seq: number | null; ts: number; kind: string }>,
  nextCursor: string | null,
  health: 'legacy' | 'healthy' | 'partial' = 'legacy',
): TimelinePageWire {
  const base = {
    sessionId: 's1',
    entries: entries.map((e) => ({ origin: 'dsh', text: '', extra: null, ...e })),
    cursor: null,
    nextCursor,
    sources: { dshLive: true, dshCold: false, sidecarReplay: false, sidecarBuffer: false },
  }
  if (health === 'legacy') return base as TimelinePageWire
  if (health === 'healthy') {
    return {
      ...base,
      sourceOutcomes: {
        liveSession: 'succeeded',
        sessionQuery: 'unavailable',
        sidecarReplay: 'not_found',
        buffer: 'not_found',
      },
      degraded: false,
      reason: null,
    } as TimelinePageWire
  }
  return {
    ...base,
    sourceOutcomes: {
      liveSession: 'succeeded',
      sessionQuery: 'unavailable',
      sidecarReplay: 'source_failed',
      buffer: 'not_found',
    },
    degraded: true,
    reason: 'partial_source_failure',
  } as TimelinePageWire
}

function detailWire(overrides: Partial<SessionDetailWire> = {}): SessionDetailWire {
  return {
    session: null,
    unified: unified(),
    timeline: page([{ seq: 2, ts: 2_000, kind: 'user' }, { seq: 3, ts: 3_000, kind: 'assistant' }], 'CUR1'),
    ...overrides,
  }
}

const LINEAGE_OK: LineageResponseVM = {
  available: true,
  trace: {
    target: { header: { id: 's1', createdAt: 1 }, live: true, persisted: true },
    ancestors: [],
    descendants: [],
    complete: true,
  },
  reason: null,
}

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (err: unknown) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  let reject!: (err: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const settle = (): Promise<void> => new Promise((resolve) => { setTimeout(resolve, 0) })

// ---------------------------------------------------------------------------
// open().
// ---------------------------------------------------------------------------

describe('DetailStore.open', () => {
  it('loads header + newest page and resolves lineage for dsh sessions', async () => {
    const lineageCalls: string[] = []
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.resolve(detailWire()),
      fetchLineageFn: (id) => {
        lineageCalls.push(id)
        return Promise.resolve(LINEAGE_OK)
      },
    })
    await store.open()
    await settle()
    const state = store.getState()
    expect(state.ready).toBe(true)
    expect(state.loading).toBe(false)
    expect(state.header.title).toBe('fused title') // wire wins over the hint
    expect(state.timeline.entries).toHaveLength(2)
    expect(state.hasMore).toBe(true)
    expect(lineageCalls).toEqual(['s1'])
    expect(state.lineage.available).toBe(true)
    expect(state.lineage.trace).not.toBeNull()
  })

  it('degrades lineage client-side for non-dsh sessions without dialing', async () => {
    let dialed = 0
    const store = new DetailStore('s1', {
      hint: { ...HINT, agent: 'claude' },
      fetchDetailFn: () =>
        Promise.resolve(detailWire({ unified: unified({ agent: 'claude' }) })),
      fetchLineageFn: () => {
        dialed += 1
        return Promise.resolve(LINEAGE_OK)
      },
    })
    await store.open()
    await settle()
    expect(dialed).toBe(0)
    const { lineage } = store.getState()
    expect(lineage.loading).toBe(false)
    expect(lineage.available).toBe(false)
    expect(lineage.reason).toBe('not_dsh_session')
  })

  it('answers the pre-M3 placeholder (timeline null) as fusion_not_wired', async () => {
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.resolve(detailWire({ timeline: null })),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
    })
    await store.open()
    const state = store.getState()
    expect(state.ready).toBe(false)
    expect(state.error).toBe('fusion_not_wired')
  })

  it('maps an ApiError to its reason and still resolves the lineage slice', async () => {
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.reject(new ApiError('http', 'session_not_found', 404)),
      fetchLineageFn: () => Promise.reject(new ApiError('http', 'fusion_not_wired', 501)),
    })
    await store.open()
    await settle()
    const state = store.getState()
    expect(state.error).toBe('session_not_found')
    expect(state.ready).toBe(false)
    expect(state.lineage.loading).toBe(false)
    expect(state.lineage.error).toBe('fusion_not_wired')
  })

  it('is idempotent: a second open() never re-dials', async () => {
    let calls = 0
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => {
        calls += 1
        return Promise.resolve(detailWire())
      },
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
    })
    await store.open()
    await store.open()
    expect(calls).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// Independent SSE detail refresher.
// ---------------------------------------------------------------------------

describe('DetailStore detail refresher', () => {
  it('invalidates old eligible detail and commits only the latest queued deny', async () => {
    const oldEligible = deferred<SessionDetailWire>()
    const latestDeny = deferred<SessionDetailWire>()
    const signals: AbortSignal[] = []
    let calls = 0
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: (_id, opts) => {
        signals.push(opts?.signal as AbortSignal)
        calls += 1
        if (calls === 1) {
          return Promise.resolve(detailWire({
            unified: unified({ title: 'initial', status: 'idle' }),
          }))
        }
        return calls === 2 ? oldEligible.promise : latestDeny.promise
      },
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
    })
    await store.open()
    await settle()

    // First SSE starts an eligible detail fetch. A newer deny snapshot must
    // invalidate it synchronously, while further bursts collapse into the
    // same one queued refresh carrying only the latest generation.
    store.notifySnapshot({
      agent: 'dsh', title: 'eligible card', project: '/proj', status: 'idle',
    })
    expect(calls).toBe(2)
    store.notifySnapshot({
      agent: 'dsh', title: 'deny card', project: '/proj', status: 'working',
    })
    store.notifySnapshot({
      agent: 'dsh', title: 'latest deny card', project: '/proj', status: 'working',
    })
    expect(calls).toBe(2)
    expect(signals[1]?.aborted).toBe(true)
    expect(store.getState().header).toMatchObject({
      title: 'latest deny card',
      status: 'working',
    })

    oldEligible.resolve(detailWire({
      unified: unified({ title: 'stale eligible response', status: 'idle' }),
    }))
    await settle()
    expect(calls).toBe(3)
    // The stale allow never publishes while the latest deny is pending.
    expect(store.getState().header).toMatchObject({
      title: 'latest deny card',
      status: 'working',
    })

    latestDeny.resolve(detailWire({
      unified: unified({ title: 'authoritative deny', status: 'working' }),
      timeline: page([{ seq: 99, ts: 99_000, kind: 'error' }], null, 'partial'),
    }))
    await settle()
    expect(store.getState().header).toMatchObject({
      title: 'authoritative deny',
      status: 'working',
    })
    // An independent detail response cannot roll back timeline ownership.
    expect(store.getState().timeline.entries.map((entry) => entry.seq)).toEqual([2, 3])
    expect(store.getState().timeline.nextCursor).toBe('CUR1')
    expect(store.getState().timelineHealth.kind).toBe('healthy')
  })
})

// ---------------------------------------------------------------------------
// loadMore().
// ---------------------------------------------------------------------------

describe('DetailStore.loadMore', () => {
  async function readyStore(pages: Record<string, TimelinePageWire>): Promise<{
    store: DetailStore
    cursors: Array<string | null | undefined>
  }> {
    const cursors: Array<string | null | undefined> = []
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.resolve(detailWire()),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
      fetchPageFn: (_id, opts) => {
        cursors.push(opts?.cursor)
        const key = opts?.cursor ?? ''
        const next = pages[key]
        if (next === undefined) return Promise.reject(new ApiError('http', 'invalid_cursor', 400))
        return Promise.resolve(next)
      },
    })
    await store.open()
    await settle()
    return { store, cursors }
  }

  it('pages older history via the cursor and dedups overlaps', async () => {
    const { store, cursors } = await readyStore({
      CUR1: page([{ seq: 1, ts: 1_000, kind: 'user' }, { seq: 2, ts: 2_000, kind: 'user' }], null),
    })
    await store.loadMore()
    expect(cursors).toEqual(['CUR1'])
    const state = store.getState()
    expect(state.timeline.entries.map((e) => e.seq)).toEqual([1, 2, 3])
    expect(state.hasMore).toBe(false)
    expect(state.timeline.reachedStart).toBe(true)
  })

  it('keeps shown entries and surfaces the reason when a page fails', async () => {
    const { store } = await readyStore({}) // every cursor rejects
    await store.loadMore()
    const state = store.getState()
    expect(state.timeline.entries).toHaveLength(2)
    expect(state.error).toBe('invalid_cursor')
    expect(state.loading).toBe(false)
  })

  it('no-ops without a cursor or before ready', async () => {
    const { store, cursors } = await readyStore({
      CUR1: page([], null),
    })
    await store.loadMore() // consumes CUR1 → no cursor left
    await store.loadMore()
    expect(cursors).toEqual(['CUR1'])
  })

  it('ignores an older loadMore settlement after a newest refresh starts', async () => {
    const older = deferred<TimelinePageWire>()
    const newest = deferred<TimelinePageWire>()
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.resolve(detailWire({
        timeline: page(
          [{ seq: 2, ts: 2_000, kind: 'user' }, { seq: 3, ts: 3_000, kind: 'assistant' }],
          'CUR1',
          'partial',
        ),
      })),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
      fetchPageFn: (_id, opts) => opts?.cursor === 'CUR1' ? older.promise : newest.promise,
    })
    await store.open()
    await settle()

    const paging = store.loadMore()
    const refreshing = store.refreshNewest()
    newest.resolve(page([{ seq: 4, ts: 4_000, kind: 'assistant' }], null, 'healthy'))
    await refreshing
    older.resolve(page([{ seq: 1, ts: 1_000, kind: 'user' }], null, 'partial'))
    await paging

    const state = store.getState()
    expect(state.timeline.entries.map((entry) => entry.seq)).toEqual([2, 3, 4])
    expect(state.timeline.nextCursor).toBe('CUR1')
    expect(state.timelineHealth.kind).toBe('healthy')
    expect(state.loading).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Listen mode.
// ---------------------------------------------------------------------------

describe('DetailStore listen mode', () => {
  it('toggling on refetches the newest window and highlights appended keys', async () => {
    let newestCalls = 0
    const store = new DetailStore('s1', {
      hint: HINT,
      listenLimit: 50,
      fetchDetailFn: () => Promise.resolve(detailWire()),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
      fetchPageFn: (_id, opts) => {
        expect(opts?.cursor).toBeUndefined()
        expect(opts?.limit).toBe(50)
        newestCalls += 1
        return Promise.resolve(page([{ seq: 4, ts: 4_000, kind: 'assistant' }], 'IGNORED'))
      },
    })
    await store.open()
    await settle()
    store.toggleListen()
    await settle()
    expect(newestCalls).toBe(1)
    const state = store.getState()
    expect(state.listening).toBe(true)
    expect(state.timeline.entries.map((e) => e.seq)).toEqual([2, 3, 4])
    expect(state.timeline.newKeys).toHaveLength(1)
    // Listen pages never advance the history cursor.
    expect(state.timeline.nextCursor).toBe('CUR1')
  })

  it('coalesces snapshot bursts to one in-flight + one queued refetch', async () => {
    const gate = deferred<TimelinePageWire>()
    let calls = 0
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.resolve(detailWire()),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
      fetchPageFn: () => {
        calls += 1
        return calls === 1 ? gate.promise : Promise.resolve(page([], null))
      },
    })
    await store.open()
    await settle()
    store.toggleListen() // refetch #1 (parked on the gate)
    store.notifySnapshot(null)
    store.notifySnapshot(null)
    store.notifySnapshot(null)
    expect(calls).toBe(1)
    gate.resolve(page([], null))
    await settle()
    expect(calls).toBe(2) // the burst collapsed into exactly one follow-up
  })

  it('refreshes the header from the live card and ignores refetch while off', async () => {
    let pageCalls = 0
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.resolve(detailWire()),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
      fetchPageFn: () => {
        pageCalls += 1
        return Promise.resolve(page([], null))
      },
    })
    await store.open()
    await settle()
    store.notifySnapshot({ agent: 'dsh', title: 'fused title', project: '/proj', status: 'idle' })
    expect(store.getState().header.status).toBe('idle')
    expect(pageCalls).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// Manual newest-window refresh (UX-07 button + UX-05 delivered loop).
// ---------------------------------------------------------------------------

describe('DetailStore.refreshNewest', () => {
  it('pulls the newest window with in-flight feedback and highlights appends', async () => {
    const gate = deferred<TimelinePageWire>()
    const store = new DetailStore('s1', {
      hint: HINT,
      listenLimit: 50,
      fetchDetailFn: () => Promise.resolve(detailWire()),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
      fetchPageFn: (_id, opts) => {
        expect(opts?.cursor).toBeUndefined()
        expect(opts?.limit).toBe(50)
        return gate.promise
      },
    })
    await store.open()
    await settle()
    const done = store.refreshNewest()
    expect(store.getState().refreshing).toBe(true)
    gate.resolve(page([{ seq: 4, ts: 4_000, kind: 'assistant' }], 'IGNORED'))
    await done
    const state = store.getState()
    expect(state.refreshing).toBe(false)
    expect(state.timeline.entries.map((e) => e.seq)).toEqual([2, 3, 4])
    // The appended entry gets the listen-merge highlight…
    expect(state.timeline.newKeys).toHaveLength(1)
    // …and the history cursor never moves.
    expect(state.timeline.nextCursor).toBe('CUR1')
  })

  it('surfaces the failure reason and keeps shown data', async () => {
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.resolve(detailWire()),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
      fetchPageFn: () => Promise.reject(new ApiError('timeout', 'request_timeout')),
    })
    await store.open()
    await settle()
    await store.refreshNewest()
    const state = store.getState()
    expect(state.refreshing).toBe(false)
    expect(state.error).toBe('request_timeout')
    expect(state.timeline.entries).toHaveLength(2)
  })

  it('lets a newer refresh supersede an older out-of-order response', async () => {
    const firstGate = deferred<TimelinePageWire>()
    const secondGate = deferred<TimelinePageWire>()
    const signals: AbortSignal[] = []
    let calls = 0
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.resolve(detailWire()),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
      fetchPageFn: (_id, opts) => {
        calls += 1
        signals.push(opts?.signal as AbortSignal)
        return calls === 1 ? firstGate.promise : secondGate.promise
      },
    })
    await store.refreshNewest() // before open(): dropped
    expect(calls).toBe(0)
    await store.open()
    await settle()
    const first = store.refreshNewest()
    const second = store.refreshNewest()
    expect(calls).toBe(2)
    expect(signals[0]?.aborted).toBe(true)
    secondGate.resolve(page([{ seq: 5, ts: 5_000, kind: 'assistant' }], null, 'healthy'))
    await second
    firstGate.resolve(page([{ seq: 4, ts: 4_000, kind: 'assistant' }], null, 'partial'))
    await first
    const state = store.getState()
    expect(state.timeline.entries.map((entry) => entry.seq)).toEqual([2, 3, 5])
    expect(state.timelineHealth.kind).toBe('healthy')
    expect(state.refreshing).toBe(false)
  })

  it('aggregates degraded health across pages until an explicit healthy refresh', async () => {
    let newestCalls = 0
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.resolve(detailWire({
        timeline: page(
          [{ seq: 3, ts: 3_000, kind: 'assistant' }],
          'CUR1',
          'healthy',
        ),
      })),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
      fetchPageFn: (_id, opts) => {
        if (opts?.cursor === 'CUR1') {
          return Promise.resolve(page([{ seq: 2, ts: 2_000, kind: 'user' }], 'CUR2', 'partial'))
        }
        if (opts?.cursor === 'CUR2') {
          return Promise.resolve(page([{ seq: 1, ts: 1_000, kind: 'user' }], null, 'healthy'))
        }
        newestCalls += 1
        return Promise.resolve(
          page(
            [{ seq: 4 + newestCalls, ts: 4_000 + newestCalls, kind: 'assistant' }],
            null,
            newestCalls === 1 ? 'legacy' : 'healthy',
          ),
        )
      },
    })
    await store.open()
    await settle()
    await store.loadMore()
    await store.loadMore()

    expect(store.getState().timeline.entries.map((entry) => entry.seq)).toEqual([1, 2, 3])
    expect(store.getState().timelineHealth.kind).toBe('partial')

    await store.refreshNewest()
    expect(store.getState().timelineHealth.kind).toBe('partial')
    expect(store.getState().timeline.entries.map((entry) => entry.seq)).toEqual([1, 2, 3, 5])

    await store.refreshNewest()
    expect(store.getState().timelineHealth).toMatchObject({
      kind: 'healthy',
      legacy: false,
    })
    expect(store.getState().timeline.entries.map((entry) => entry.seq)).toEqual([1, 2, 3, 5, 6])
  })
})

// ---------------------------------------------------------------------------
// dispose() + pure helpers.
// ---------------------------------------------------------------------------

describe('DetailStore.dispose', () => {
  it('drops late settlements silently', async () => {
    const gate = deferred<SessionDetailWire>()
    let signal: AbortSignal | undefined
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: (_id, opts) => {
        signal = opts?.signal as AbortSignal | undefined
        return gate.promise
      },
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
    })
    const opening = store.open()
    store.dispose()
    expect(signal?.aborted).toBe(true)
    gate.resolve(detailWire())
    await opening
    expect(store.getState().ready).toBe(false)
    expect(store.getState().timeline.entries).toHaveLength(0)
  })

  it('isolates a pending old target from the newly mounted target', async () => {
    const oldGate = deferred<SessionDetailWire>()
    let oldSignal: AbortSignal | undefined
    const oldStore = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: (_id, opts) => {
        oldSignal = opts?.signal as AbortSignal | undefined
        return oldGate.promise
      },
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
    })
    const oldOpening = oldStore.open()
    oldStore.dispose()

    const newStore = new DetailStore('s2', {
      hint: { ...HINT, title: 'new target' },
      fetchDetailFn: () => Promise.resolve(detailWire({
        unified: unified({ sessionId: 's2', title: 'new target' }),
        timeline: {
          ...page([{ seq: 9, ts: 9_000, kind: 'assistant' }], null, 'healthy'),
          sessionId: 's2',
        },
      })),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
    })
    await newStore.open()
    oldGate.resolve(detailWire({
      timeline: page([{ seq: 1, ts: 1_000, kind: 'user' }], null, 'partial'),
    }))
    await oldOpening

    expect(oldSignal?.aborted).toBe(true)
    expect(oldStore.getState().ready).toBe(false)
    expect(oldStore.getState().timeline.entries).toEqual([])
    expect(newStore.getState().sessionId).toBe('s2')
    expect(newStore.getState().header.title).toBe('new target')
    expect(newStore.getState().timeline.entries.map((entry) => entry.seq)).toEqual([9])
  })
})

describe('pure helpers', () => {
  it('headerFromDetailWire prefers unified, falls back to session, else null', () => {
    expect(headerFromDetailWire(detailWire())?.title).toBe('fused title')
    const sessionOnly = detailWire({
      unified: null,
      session: {
        agent: 'claude', session_id: 's1', status: 'idle', title: 'card',
        project: '/c', updated_at: 1, last_event: null, gap: false,
      },
    })
    expect(headerFromDetailWire(sessionOnly)).toEqual({
      agent: 'claude', title: 'card', project: '/c', status: 'idle',
    })
    expect(headerFromDetailWire(detailWire({ unified: null, session: null }))).toBeNull()
  })

  it('findCardHint resolves controller cards by sessionId', () => {
    const cards = [
      { agent: 'dsh', sessionId: 'a', title: 't', project: '/p', status: 'working' },
    ]
    expect(findCardHint(cards, 'a')?.agent).toBe('dsh')
    expect(findCardHint(cards, 'missing')).toBeNull()
  })
})
