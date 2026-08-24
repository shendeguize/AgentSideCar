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
): TimelinePageWire {
  return {
    sessionId: 's1',
    entries: entries.map((e) => ({ origin: 'dsh', text: '', extra: null, ...e })),
    cursor: null,
    nextCursor,
    sources: { dshLive: true, dshCold: false, sidecarReplay: false, sidecarBuffer: false },
  }
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

  it('coalesces concurrent calls and no-ops before ready', async () => {
    const gate = deferred<TimelinePageWire>()
    let calls = 0
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => Promise.resolve(detailWire()),
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
      fetchPageFn: () => {
        calls += 1
        return gate.promise
      },
    })
    await store.refreshNewest() // before open(): dropped
    expect(calls).toBe(0)
    await store.open()
    await settle()
    const first = store.refreshNewest()
    void store.refreshNewest() // while in flight: dropped, not queued
    expect(calls).toBe(1)
    gate.resolve(page([], null))
    await first
    expect(calls).toBe(1)
    expect(store.getState().refreshing).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// dispose() + pure helpers.
// ---------------------------------------------------------------------------

describe('DetailStore.dispose', () => {
  it('drops late settlements silently', async () => {
    const gate = deferred<SessionDetailWire>()
    const store = new DetailStore('s1', {
      hint: HINT,
      fetchDetailFn: () => gate.promise,
      fetchLineageFn: () => Promise.resolve(LINEAGE_OK),
    })
    const opening = store.open()
    store.dispose()
    gate.resolve(detailWire())
    await opening
    expect(store.getState().ready).toBe(false)
    expect(store.getState().timeline.entries).toHaveLength(0)
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
