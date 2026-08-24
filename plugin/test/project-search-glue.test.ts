/**
 * Unit tests for the project-correlation and search glue stores
 * (client/project-glue.ts + client/search-glue.ts). Node environment with
 * injected transports and clocks; no real network.
 */

import { describe, expect, it } from 'vitest'
import { ApiError } from '../src/client/api.ts'
import {
  ProjectsStore,
  findProjectSessionHint,
} from '../src/client/project-glue.ts'
import { SearchStore } from '../src/client/search-glue.ts'
import type { ProjectGroupVM } from '../src/client/board/project-view-logic.ts'
import type { SearchResponseVM } from '../src/client/dsh-tools/logic.ts'

const settle = (): Promise<void> => new Promise((resolve) => { setTimeout(resolve, 0) })

function groups(): ProjectGroupVM[] {
  return [
    {
      project: '/proj',
      agents: ['claude', 'dsh'],
      sessions: [
        { agent: 'dsh', sessionId: 'd1', status: 'working', title: 'T', lastActivityAt: 2 },
        { agent: 'claude', sessionId: 'c1', status: 'idle', title: '', lastActivityAt: 1 },
      ],
      lastActivityAt: 2,
    },
  ]
}

// ---------------------------------------------------------------------------
// ProjectsStore.
// ---------------------------------------------------------------------------

describe('ProjectsStore', () => {
  it('refresh() loads groups and clears loading/error', async () => {
    const store = new ProjectsStore({ fetchProjectsFn: () => Promise.resolve({ groups: groups() }) })
    await store.refresh()
    const state = store.getState()
    expect(state.groups).toHaveLength(1)
    expect(state.loading).toBe(false)
    expect(state.error).toBeNull()
    expect(state.loadedAt).not.toBeNull()
  })

  it('keeps stale groups through a failed refresh and banners the reason', async () => {
    let fail = false
    const store = new ProjectsStore({
      fetchProjectsFn: () =>
        fail
          ? Promise.reject(new ApiError('http', 'fusion_not_wired', 501))
          : Promise.resolve({ groups: groups() }),
      minRefreshMs: 0,
    })
    await store.refresh()
    fail = true
    await store.refresh()
    const state = store.getState()
    expect(state.groups).toHaveLength(1) // stale data stays visible
    expect(state.error).toBe('fusion_not_wired')
  })

  it('notifySnapshot throttles below minRefreshMs and dedups in-flight', async () => {
    let now = 0
    let calls = 0
    const store = new ProjectsStore({
      fetchProjectsFn: () => {
        calls += 1
        return Promise.resolve({ groups: [] })
      },
      minRefreshMs: 5_000,
      now: () => now,
    })
    store.notifySnapshot() // first attempt dials
    await settle()
    now = 1_000
    store.notifySnapshot() // < 5s: throttled
    await settle()
    expect(calls).toBe(1)
    now = 6_000
    store.notifySnapshot() // past the window: dials again
    await settle()
    expect(calls).toBe(2)
  })

  it('dispose() drops late settlements', async () => {
    let release: (() => void) | null = null
    const store = new ProjectsStore({
      fetchProjectsFn: () =>
        new Promise((resolve) => {
          release = () => { resolve({ groups: groups() }) }
        }),
    })
    const refreshing = store.refresh()
    store.dispose()
    release?.()
    await refreshing
    expect(store.getState().groups).toHaveLength(0)
  })
})

describe('findProjectSessionHint', () => {
  it('resolves a session inside the loaded groups (project from the group)', () => {
    expect(findProjectSessionHint(groups(), 'c1')).toEqual({
      agent: 'claude', title: '', project: '/proj', status: 'idle',
    })
    expect(findProjectSessionHint(groups(), 'missing')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// SearchStore.
// ---------------------------------------------------------------------------

function searchResponse(overrides: Partial<SearchResponseVM> = {}): SearchResponseVM {
  return {
    mode: 'full-text',
    query: 'demo',
    project: null,
    items: [
      {
        session: { agent: 'dsh', sessionId: 'd1', title: 'demo title', project: '/p', status: 'idle' },
        matchedBy: 'full-text',
        snippet: 'a demo snippet',
      },
    ],
    ...overrides,
  }
}

describe('SearchStore', () => {
  it('submit() dials with the trimmed query and normalizes the items', async () => {
    const seen: Array<{ q?: string; project?: string | null; limit?: number }> = []
    const store = new SearchStore({
      fetchSearchFn: (opts) => {
        seen.push({ q: opts?.q, project: opts?.project ?? null, limit: opts?.limit })
        return Promise.resolve(searchResponse())
      },
      limit: 20,
    })
    store.setQuery('  demo  ')
    await store.submit()
    expect(seen).toEqual([{ q: 'demo', project: null, limit: 20 }])
    const state = store.getState()
    expect(state.items).toHaveLength(1)
    expect(state.items[0]?.matchedBy).toBe('full-text')
    expect(state.items[0]?.snippet?.some((seg) => seg.highlight)).toBe(true)
    expect(state.mode).toBe('full-text')
    expect(state.submittedQuery).toBe('demo')
    expect(state.loading).toBe(false)
  })

  it('adopts the filter-only degradation mode from the response', async () => {
    const store = new SearchStore({
      fetchSearchFn: () =>
        Promise.resolve(searchResponse({ mode: 'filter-only', items: [] })),
    })
    store.setQuery('x')
    await store.submit()
    expect(store.getState().mode).toBe('filter-only')
    expect(store.getState().items).toHaveLength(0)
  })

  it('a blank query with no project filter clears locally without dialing', async () => {
    let calls = 0
    const store = new SearchStore({
      fetchSearchFn: () => {
        calls += 1
        return Promise.resolve(searchResponse())
      },
    })
    store.setQuery('demo')
    await store.submit()
    store.setQuery('   ')
    await store.submit()
    expect(calls).toBe(1)
    const state = store.getState()
    expect(state.items).toHaveLength(0)
    expect(state.submittedQuery).toBe('')
    expect(state.error).toBeNull()
  })

  it('keeps a fixed project filter and dials blank-query project searches', async () => {
    const seen: Array<string | null | undefined> = []
    const store = new SearchStore({
      project: '/proj',
      fetchSearchFn: (opts) => {
        seen.push(opts?.project)
        return Promise.resolve(searchResponse({ query: '', project: '/proj', items: [] }))
      },
    })
    await store.submit() // blank query BUT a project filter → dials
    expect(seen).toEqual(['/proj'])
  })

  it('maps failures to the reason and keeps previous results', async () => {
    let fail = false
    const store = new SearchStore({
      fetchSearchFn: () =>
        fail
          ? Promise.reject(new ApiError('timeout', 'request_timeout'))
          : Promise.resolve(searchResponse()),
    })
    store.setQuery('demo')
    await store.submit()
    fail = true
    await store.submit()
    const state = store.getState()
    expect(state.error).toBe('request_timeout')
    expect(state.items).toHaveLength(1)
  })

  it('ignores out-of-order settles via the submit ticket', async () => {
    const resolvers: Array<(r: SearchResponseVM) => void> = []
    const store = new SearchStore({
      fetchSearchFn: () =>
        new Promise((resolve) => { resolvers.push(resolve) }),
    })
    store.setQuery('one')
    void store.submit()
    store.setQuery('two')
    void store.submit()
    // The first (stale) settle arrives after the second dial started.
    resolvers[0]?.(searchResponse({ query: 'one' }))
    await settle()
    expect(store.getState().submittedQuery).toBe('') // stale settle dropped
    resolvers[1]?.(searchResponse({ query: 'two' }))
    await settle()
    expect(store.getState().submittedQuery).toBe('two')
  })
})
