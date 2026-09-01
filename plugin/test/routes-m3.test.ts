/**
 * Tests for the M3 route surface (src/routes.ts + src/fusion.ts wiring):
 * upgraded `GET session/<id>`, `GET session/<id>/timeline` pagination,
 * `GET lineage/<id>`, `GET search`, `GET projects`, plus the 501
 * degradation when no fusion is wired and the guard/405 envelope.
 *
 * Harness: the same real `node:http` carrier as routes.test.ts. The fusion
 * dependency is a REAL FusionQuery over fake faces (dsh feed, sessionQuery,
 * replay), so pagination cursors and degradation semantics are the real
 * ones, not fixtures of this test's own invention.
 */

import { createServer, request as httpRequest, type IncomingHttpHeaders, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, describe, expect, it } from 'vitest'

import type { SessionRow } from '../src/bridge.ts'
import {
  FusionQuery,
  type DshEventFace,
  type DshSessionEventFace,
  type DshSessionFace,
  type SessionQueryFace,
  type SidecarEventFace,
  type SidecarReplayFace,
} from '../src/fusion.ts'
import { API_PREFIX, createRoutes, type FusionApi, type Routes } from '../src/routes.ts'
import { SessionStore } from '../src/session-store.ts'
import { DaemonSupervisor } from '../src/supervisor.ts'

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

function row(id: string, over: Partial<SessionRow> = {}): SessionRow {
  return {
    agent: 'claude',
    session_id: id,
    project: '/tmp/proj',
    transcript: '/tmp/proj/t.jsonl',
    updated_at: 1_000_000, // epoch seconds
    title: 'title',
    status: 'working',
    extra: {},
    parent_id: null,
    ...over,
  }
}

function sidecarEv(sessionId: string, seq: number, over: Partial<SidecarEventFace> = {}): SidecarEventFace {
  return {
    ts: `2026-08-24T00:00:0${seq}Z`,
    agent: 'claude',
    session_id: sessionId,
    kind: 'assistant',
    text: `event ${seq}`,
    extra: { seq },
    ...over,
  }
}

/** Capturing in-process dsh feed: tests emit through it after start(). */
class FakeDshFeed implements DshEventFace {
  private readonly handlers = new Map<string, Set<(...args: unknown[]) => void>>()
  private readonly resident = new Map<string, DshSessionFace>()
  readonly getCalls: string[] = []

  on(event: string, handler: (...args: never[]) => void): () => void {
    let set = this.handlers.get(event)
    if (set === undefined) {
      set = new Set()
      this.handlers.set(event, set)
    }
    set.add(handler as (...args: unknown[]) => void)
    return () => {
      set.delete(handler as (...args: unknown[]) => void)
    }
  }

  emit(event: string, ...args: unknown[]): void {
    const session = args[0]
    if (event === 'session/created' && isDshSession(session)) {
      this.resident.set(session.id, session)
    } else if (event === 'session/disposed' && isDshSession(session)) {
      this.resident.delete(session.id)
    }
    for (const handler of this.handlers.get(event) ?? []) handler(...args)
  }

  seed(session: DshSessionFace): void {
    this.resident.set(session.id, session)
  }

  get(sessionId: string): DshSessionFace | undefined {
    this.getCalls.push(sessionId)
    return this.resident.get(sessionId)
  }
}

function isDshSession(value: unknown): value is DshSessionFace {
  return typeof value === 'object' && value !== null && typeof (value as { id?: unknown }).id === 'string'
}

function dshSession(
  id: string,
  events: DshSessionEventFace[],
  header: Partial<DshSessionFace['header']> = {},
): DshSessionFace {
  return { id, header: { createdAt: 1_000_000_000, cwd: '/tmp/proj', ...header }, events }
}

function makeSupervisor(): DaemonSupervisor {
  return new DaemonSupervisor(
    {
      ping: async () => null,
      spawnDaemon: () => {
        throw new Error('spawn must not be reached in route tests')
      },
      detectLaunchAgent: async () => false,
      log: () => {},
    },
    { policy: 'off' },
  )
}

interface FusionParts {
  feed?: FakeDshFeed
  getSessionQuery?: () => SessionQueryFace | null
  replay?: SidecarReplayFace
}

interface Harness {
  store: SessionStore
  fusion: FusionQuery | undefined
  routes: Routes
  base: URL
  server: Server
  supervisor: DaemonSupervisor
  close(): Promise<void>
}

const harnesses: Harness[] = []

afterEach(async () => {
  for (const harness of harnesses.splice(0)) await harness.close()
})

/**
 * @param withFusion - false mounts the M1/M2-style deps (no fusion) so the
 * 501 degradation contract is exercised on the same harness.
 */
async function startHarness(withFusion: boolean, parts: FusionParts = {}): Promise<Harness> {
  const store = new SessionStore()
  const supervisor = makeSupervisor()
  const fusion = withFusion
    ? new FusionQuery({
        store,
        dshEvents: parts.feed ?? null,
        getSessionQuery: parts.getSessionQuery ?? null,
        replay: parts.replay ?? null,
        // Fixed clock right after the fixtures' updated_at (epoch seconds
        // 1_000_000 → ms 1_000_000_000): every row is inside the window.
        now: () => 1_000_000_000 + 60_000,
      })
    : undefined
  fusion?.start()
  const routes = createRoutes({
    store,
    supervisor,
    guardOptions: { allowWriteActions: () => false },
    ...(fusion !== undefined ? { fusion: fusion satisfies FusionApi } : {}),
    log: () => {},
  })
  const server = createServer((req, res) => {
    routes.handle(req, res).catch(() => {
      if (!res.headersSent) {
        res.writeHead(400)
        res.end()
      }
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as AddressInfo
  const harness: Harness = {
    store,
    fusion,
    routes,
    base: new URL(`http://127.0.0.1:${port}`),
    server,
    supervisor,
    close: async () => {
      routes.dispose()
      fusion?.stop()
      await new Promise<void>((resolve) => {
        server.close(() => resolve())
        server.closeAllConnections()
      })
      await supervisor.stop()
    },
  }
  harnesses.push(harness)
  return harness
}

interface HttpReply {
  status: number
  headers: IncomingHttpHeaders
  json: Record<string, unknown>
}

function get(
  base: URL,
  path: string,
  init: { method?: string; headers?: Record<string, string> } = {},
): Promise<HttpReply> {
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      {
        host: base.hostname,
        port: base.port,
        path,
        method: init.method ?? 'GET',
        headers: init.headers,
        agent: false,
      },
      (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk: string) => {
          body += chunk
        })
        res.on('end', () =>
          resolve({
            status: res.statusCode ?? 0,
            headers: res.headers,
            json: body === '' ? {} : (JSON.parse(body) as Record<string, unknown>),
          }),
        )
      },
    )
    req.on('error', reject)
    req.end()
  })
}

interface TimelineReply {
  sessionId: string
  entries: Array<{ origin: string; seq: number | null; ts: number; kind: string; text: string }>
  cursor: { seq: number | null; ts: number } | null
  nextCursor: string | null
  sources: Record<string, boolean>
  sourceOutcomes: Record<string, string>
  degraded: boolean
  reason: string | null
}

// ---------------------------------------------------------------------------
// Degradation without a wired fusion (M1/M2-style deps).
// ---------------------------------------------------------------------------

describe('M3 endpoints without fusion', () => {
  it('answers 501 fusion_not_wired on every new path and keeps the M1 session placeholder', async () => {
    const h = await startHarness(false)
    h.store.applySnapshot([row('s1')])

    for (const path of ['projects', 'search?q=x', 'lineage/s1', 'session/s1/timeline']) {
      const reply = await get(h.base, `${API_PREFIX}/${path}`)
      expect(reply.status, path).toBe(501)
      expect(reply.json.reason, path).toBe('fusion_not_wired')
    }

    const detail = await get(h.base, `${API_PREFIX}/session/s1`)
    expect(detail.status).toBe(200)
    expect(detail.json.timeline).toBeNull()
    expect(detail.json.timelineNote).toBe('timeline_not_available_until_m3')
  })
})

// ---------------------------------------------------------------------------
// GET session/<id> upgrade.
// ---------------------------------------------------------------------------

describe('GET session/<id> with fusion', () => {
  it('returns the store row, the unified row, and the newest timeline page', async () => {
    const h = await startHarness(true)
    h.store.applySnapshot([row('s1', { title: 'merge me' })])
    h.fusion!.ingestSidecarEvent(sidecarEv('s1', 1))
    h.fusion!.ingestSidecarEvent(sidecarEv('s1', 2))

    const reply = await get(h.base, `${API_PREFIX}/session/s1`)
    expect(reply.status).toBe(200)
    expect((reply.json.session as { session_id: string }).session_id).toBe('s1')
    const unified = reply.json.unified as { sessionId: string; origin: string; title: string }
    expect(unified.sessionId).toBe('s1')
    expect(unified.origin).toBe('sidecar')
    const timeline = reply.json.timeline as unknown as TimelineReply
    expect(timeline.entries.map((e) => e.seq)).toEqual([1, 2])
    expect((timeline as unknown as Record<string, unknown>).events).toBeUndefined()
    expect(timeline.nextCursor).toBeNull()
    expect(timeline.sources.sidecarBuffer).toBe(true)
    expect(timeline.sourceOutcomes.buffer).toBe('succeeded')
    expect(timeline.degraded).toBe(false)
    expect(timeline.reason).toBeNull()
  })

  it('resolves a dsh-live session the sidecar has not seen (session: null, unified: dsh-live)', async () => {
    const feed = new FakeDshFeed()
    const h = await startHarness(true, { feed })
    const live = dshSession('live1', [
      { type: 'message/user', seq: 1, time: 1_000_000_100, data: { text: 'hi' } },
    ])
    feed.emit('session/created', live)
    feed.emit('session/event', live, live.events[0])

    const reply = await get(h.base, `${API_PREFIX}/session/live1`)
    expect(reply.status).toBe(200)
    expect(reply.json.session).toBeNull()
    const unified = reply.json.unified as { origin: string; live: boolean }
    expect(unified.origin).toBe('dsh-live')
    expect(unified.live).toBe(true)
    const timeline = reply.json.timeline as unknown as TimelineReply
    expect(timeline.entries.map((e) => e.kind)).toEqual(['message/user'])
    expect(timeline.sources.dshLive).toBe(true)
  })

  it('late-binds a resident dsh session before deciding the detail target is missing', async () => {
    const feed = new FakeDshFeed()
    const resident = dshSession('late-live', [
      { type: 'agent/inbox/spliced', seq: 8, time: 1_000_000_800, data: {} },
    ])
    feed.seed(resident)
    const h = await startHarness(true, { feed })

    const reply = await get(h.base, `${API_PREFIX}/session/late-live`)

    expect(reply.status).toBe(200)
    expect(feed.getCalls).toEqual(['late-live'])
    expect((reply.json.unified as { sessionId: string }).sessionId).toBe('late-live')
    const timeline = reply.json.timeline as unknown as TimelineReply
    expect(timeline.entries.map((entry) => entry.seq)).toEqual([8])
    expect(timeline.sourceOutcomes.liveSession).toBe('succeeded')
  })

  it('404s an id neither the store nor fusion knows', async () => {
    const h = await startHarness(true)
    const reply = await get(h.base, `${API_PREFIX}/session/ghost`)
    expect(reply.status).toBe(404)
    expect(reply.json.reason).toBe('session_not_found')
  })
})

// ---------------------------------------------------------------------------
// GET session/<id>/timeline pagination.
// ---------------------------------------------------------------------------

describe('GET session/<id>/timeline', () => {
  it('pages backward through the merged timeline via nextCursor round-trips', async () => {
    const h = await startHarness(true)
    h.store.applySnapshot([row('sx')])
    for (let seq = 1; seq <= 5; seq += 1) h.fusion!.ingestSidecarEvent(sidecarEv('sx', seq))

    const page1 = (await get(h.base, `${API_PREFIX}/session/sx/timeline?limit=2`))
      .json as unknown as TimelineReply
    expect(page1.sessionId).toBe('sx')
    expect(page1.entries.map((e) => e.seq)).toEqual([4, 5])
    expect(page1.nextCursor).not.toBeNull()

    const page2 = (
      await get(
        h.base,
        `${API_PREFIX}/session/sx/timeline?limit=2&cursor=${encodeURIComponent(page1.nextCursor!)}`,
      )
    ).json as unknown as TimelineReply
    expect(page2.entries.map((e) => e.seq)).toEqual([2, 3])
    expect(page2.nextCursor).not.toBeNull()

    const page3 = (
      await get(
        h.base,
        `${API_PREFIX}/session/sx/timeline?limit=2&cursor=${encodeURIComponent(page2.nextCursor!)}`,
      )
    ).json as unknown as TimelineReply
    expect(page3.entries.map((e) => e.seq)).toEqual([1])
    expect(page3.cursor).toBeNull()
    expect(page3.nextCursor).toBeNull()
  })

  it('pulls history through the replay seam and reports the source', async () => {
    const replayed: Array<{ sessionId: string; afterSeq?: number | null }> = []
    const replay: SidecarReplayFace = {
      replay: async (request) => {
        replayed.push(request)
        return [sidecarEv('sr', 1), sidecarEv('sr', 2)]
      },
    }
    const h = await startHarness(true, { replay })
    h.store.applySnapshot([row('sr')])

    const page = (await get(h.base, `${API_PREFIX}/session/sr/timeline`))
      .json as unknown as TimelineReply
    expect(replayed).toHaveLength(1)
    expect(page.entries.map((e) => e.seq)).toEqual([1, 2])
    expect(page.sources.sidecarReplay).toBe(true)
  })

  it('records an unsupported replay without calling the page degraded', async () => {
    // `replay_unsupported` is an answer, not a breakage: the session's
    // transcript shape has no history source (or the daemon predates the
    // op). The outcome is still reported for observability, but the page
    // stays healthy so the detail view does not warn about lost data.
    const replay: SidecarReplayFace = {
      replay: async () => {
        throw new Error('replay_unsupported: daemon predates the op')
      },
    }
    const h = await startHarness(true, { replay })
    h.store.applySnapshot([row('sd')])
    h.fusion!.ingestSidecarEvent(sidecarEv('sd', 7))

    const page = (await get(h.base, `${API_PREFIX}/session/sd/timeline`))
      .json as unknown as TimelineReply
    expect(page.entries.map((e) => e.seq)).toEqual([7])
    expect(page.sources.sidecarReplay).toBe(false)
    expect(page.sources.sidecarBuffer).toBe(true)
    expect(page.sourceOutcomes.sidecarReplay).toBe('replay_unsupported')
    expect(page.degraded).toBe(false)
    expect(page.reason).toBeNull()
  })

  it('still degrades when the replay seam fails for an unknown reason', async () => {
    const replay: SidecarReplayFace = {
      replay: async () => {
        throw new Error('socket hung up')
      },
    }
    const h = await startHarness(true, { replay })
    h.store.applySnapshot([row('sf')])
    h.fusion!.ingestSidecarEvent(sidecarEv('sf', 7))

    const page = (await get(h.base, `${API_PREFIX}/session/sf/timeline`))
      .json as unknown as TimelineReply
    expect(page.sourceOutcomes.sidecarReplay).toBe('source_failed')
    expect(page.degraded).toBe(true)
    expect(page.reason).toBe('partial_source_failure')
  })

  it('returns a healthy empty entries page without inventing timeline.events', async () => {
    const engine: SessionQueryFace = {
      traceSession: async () => {
        throw new Error('unused')
      },
      readSession: async () => ({ events: [] }),
      searchSessions: async () => ({ items: [] }),
    }
    const replay: SidecarReplayFace = { replay: async () => [] }
    const h = await startHarness(true, { getSessionQuery: () => engine, replay })
    h.store.applySnapshot([row('empty')])

    const reply = await get(h.base, `${API_PREFIX}/session/empty/timeline`)
    const page = reply.json as unknown as TimelineReply & { events?: unknown }

    expect(reply.status).toBe(200)
    expect(page.entries).toEqual([])
    expect(page.events).toBeUndefined()
    expect(page.sourceOutcomes.sessionQuery).toBe('succeeded')
    expect(page.sourceOutcomes.sidecarReplay).toBe('succeeded')
    expect(page.degraded).toBe(false)
    expect(page.reason).toBeNull()
  })

  it('keeps a board-known target 200 when all usable sources fail, with redacted diagnostics', async () => {
    const sensitive = '/Users/private/transcript session-id prompt-secret'
    const engine: SessionQueryFace = {
      traceSession: async () => {
        throw new Error('unused')
      },
      readSession: async () => {
        throw new Error(`read failed ${sensitive}`)
      },
      searchSessions: async () => ({ items: [] }),
    }
    const replay: SidecarReplayFace = {
      replay: async () => {
        throw new Error(`daemon failed ${sensitive}`)
      },
    }
    const h = await startHarness(true, { getSessionQuery: () => engine, replay })
    h.store.applySnapshot([row('known-empty')])

    const reply = await get(h.base, `${API_PREFIX}/session/known-empty/timeline`)
    const page = reply.json as unknown as TimelineReply

    expect(reply.status).toBe(200)
    expect(page.entries).toEqual([])
    expect(page.degraded).toBe(true)
    expect(page.reason).toBe('all_sources_failed')
    expect(page.sourceOutcomes.sessionQuery).toBe('source_failed')
    expect(page.sourceOutcomes.sidecarReplay).toBe('source_failed')
    expect(JSON.stringify(reply.json)).not.toContain(sensitive)
  })

  it('decodes percent-encoded ids in the <id>/timeline split', async () => {
    const h = await startHarness(true)
    h.store.applySnapshot([row('a/b')])
    h.fusion!.ingestSidecarEvent(sidecarEv('a/b', 1))

    const page = (await get(h.base, `${API_PREFIX}/session/a%2Fb/timeline`))
      .json as unknown as TimelineReply
    expect(page.sessionId).toBe('a/b')
    expect(page.entries).toHaveLength(1)
  })

  it('rejects malformed cursors and limits with 400', async () => {
    const h = await startHarness(true)
    h.store.applySnapshot([row('sx')])

    for (const cursor of ['nonsense', '1~', '~5', 'a~b', '-1~100']) {
      const reply = await get(
        h.base,
        `${API_PREFIX}/session/sx/timeline?cursor=${encodeURIComponent(cursor)}`,
      )
      expect(reply.status, cursor).toBe(400)
      expect(reply.json.reason, cursor).toBe('invalid_cursor')
    }
    for (const limit of ['0', '-3', 'abc', '2.5', '9999']) {
      const reply = await get(h.base, `${API_PREFIX}/session/sx/timeline?limit=${limit}`)
      expect(reply.status, limit).toBe(400)
      expect(reply.json.reason, limit).toBe('invalid_limit')
    }
  })

  it('404s a session no source and no board row knows', async () => {
    const h = await startHarness(true)
    const reply = await get(h.base, `${API_PREFIX}/session/ghost/timeline`)
    expect(reply.status).toBe(404)
    expect(reply.json.reason).toBe('session_not_found')
  })
})

// ---------------------------------------------------------------------------
// GET lineage/<id>.
// ---------------------------------------------------------------------------

describe('GET lineage/<id>', () => {
  const trace = {
    target: {
      header: { id: 'child', createdAt: 1, parentSession: 'parent' },
      live: false,
      persisted: true,
    },
    ancestors: [{ header: { id: 'parent', createdAt: 0 }, live: false, persisted: true }],
    descendants: [],
    complete: true,
  }

  it('returns the trace when sessionQuery answers', async () => {
    const engine: SessionQueryFace = {
      traceSession: async () => trace,
      readSession: async () => ({ events: [] }),
      searchSessions: async () => ({ items: [] }),
    }
    const h = await startHarness(true, { getSessionQuery: () => engine })
    const reply = await get(h.base, `${API_PREFIX}/lineage/child`)
    expect(reply.status).toBe(200)
    expect(reply.json.available).toBe(true)
    expect(reply.json.reason).toBeNull()
    expect((reply.json.trace as typeof trace).ancestors[0]!.header.id).toBe('parent')
  })

  it('degrades to available:false when sessionQuery is absent (still 200)', async () => {
    const h = await startHarness(true)
    const reply = await get(h.base, `${API_PREFIX}/lineage/anything`)
    expect(reply.status).toBe(200)
    expect(reply.json).toEqual({
      available: false,
      trace: null,
      reason: 'session_query_unavailable',
    })
  })

  it('degrades to trace_failed when the engine throws (still 200)', async () => {
    const engine: SessionQueryFace = {
      traceSession: async () => {
        throw new Error('unknown session')
      },
      readSession: async () => ({ events: [] }),
      searchSessions: async () => ({ items: [] }),
    }
    const h = await startHarness(true, { getSessionQuery: () => engine })
    const reply = await get(h.base, `${API_PREFIX}/lineage/nope`)
    expect(reply.status).toBe(200)
    expect(reply.json.available).toBe(false)
    expect(reply.json.reason).toBe('trace_failed')
  })
})

// ---------------------------------------------------------------------------
// GET search.
// ---------------------------------------------------------------------------

describe('GET search', () => {
  it('filter-only mode matches titles and projects without sessionQuery', async () => {
    const h = await startHarness(true)
    h.store.applySnapshot([
      row('alpha', { title: 'fix the parser', project: '/tmp/proj' }),
      row('beta', { title: 'other work', project: '/work/beta' }),
    ])

    const reply = await get(h.base, `${API_PREFIX}/search?q=parser`)
    expect(reply.status).toBe(200)
    expect(reply.json.mode).toBe('filter-only')
    const items = reply.json.items as Array<{ session: { sessionId: string }; matchedBy: string }>
    expect(items).toHaveLength(1)
    expect(items[0]!.session.sessionId).toBe('alpha')
    expect(items[0]!.matchedBy).toBe('title')
  })

  it('full-text mode surfaces engine hits with snippets', async () => {
    const engine: SessionQueryFace = {
      traceSession: async () => {
        throw new Error('unused')
      },
      readSession: async () => ({ events: [] }),
      searchSessions: async () => ({
        items: [{ header: { id: 'dsh1' }, bestMatch: { seq: 3, snippet: '…the hit…' } }],
      }),
    }
    const h = await startHarness(true, { getSessionQuery: () => engine })
    h.store.applySnapshot([row('dsh1', { agent: 'dsh', title: 'dsh run' })])

    const reply = await get(h.base, `${API_PREFIX}/search?q=hit`)
    expect(reply.status).toBe(200)
    expect(reply.json.mode).toBe('full-text')
    const items = reply.json.items as Array<{ matchedBy: string; snippet: string | null }>
    expect(items[0]!.matchedBy).toBe('full-text')
    expect(items[0]!.snippet).toBe('…the hit…')
  })

  it('filters by project (exact after trailing-slash normalization) and supports project-only queries', async () => {
    const h = await startHarness(true)
    h.store.applySnapshot([
      row('alpha', { title: 'fix the parser', project: '/tmp/proj' }),
      row('beta', { title: 'fix the tests', project: '/work/beta' }),
    ])

    const combined = await get(
      h.base,
      `${API_PREFIX}/search?q=fix&project=${encodeURIComponent('/work/beta/')}`,
    )
    const combinedItems = combined.json.items as Array<{ session: { sessionId: string } }>
    expect(combinedItems.map((i) => i.session.sessionId)).toEqual(['beta'])

    const projectOnly = await get(
      h.base,
      `${API_PREFIX}/search?project=${encodeURIComponent('/tmp/proj')}`,
    )
    expect(projectOnly.status).toBe(200)
    expect(projectOnly.json.mode).toBe('filter-only')
    const projectItems = projectOnly.json.items as Array<{
      session: { sessionId: string }
      matchedBy: string
    }>
    expect(projectItems.map((i) => i.session.sessionId)).toEqual(['alpha'])
    expect(projectItems[0]!.matchedBy).toBe('project')
  })

  it('rejects a query with neither q nor project', async () => {
    const h = await startHarness(true)
    const reply = await get(h.base, `${API_PREFIX}/search`)
    expect(reply.status).toBe(400)
    expect(reply.json.reason).toBe('invalid_request')
  })
})

// ---------------------------------------------------------------------------
// GET projects.
// ---------------------------------------------------------------------------

describe('GET projects', () => {
  it('returns cross-agent correlation groups', async () => {
    const h = await startHarness(true)
    h.store.applySnapshot([
      row('a1', { agent: 'claude', project: '/tmp/proj' }),
      row('a2', { agent: 'dsh', project: '/tmp/proj/', updated_at: 1_000_100 }),
      row('b1', { agent: 'codex', project: '/work/beta' }),
    ])

    const reply = await get(h.base, `${API_PREFIX}/projects`)
    expect(reply.status).toBe(200)
    const groups = reply.json.groups as Array<{
      project: string
      agents: string[]
      sessions: unknown[]
    }>
    expect(groups).toHaveLength(2)
    const proj = groups.find((g) => g.project === '/tmp/proj')!
    expect(proj.agents).toEqual(['claude', 'dsh'])
    expect(proj.sessions).toHaveLength(2)
  })
})

// ---------------------------------------------------------------------------
// Envelope: guard interception and method discipline on the new paths.
// ---------------------------------------------------------------------------

describe('M3 endpoint envelope', () => {
  it('guard layer 2 rejects a non-loopback Host on every new endpoint', async () => {
    const h = await startHarness(true)
    for (const path of ['projects', 'search?q=x', 'lineage/s1', 'session/s1/timeline']) {
      const reply = await get(h.base, `${API_PREFIX}/${path}`, {
        headers: { host: 'evil.example' },
      })
      expect(reply.status, path).toBe(403)
      expect(reply.json.reason, path).toBe('host_not_loopback')
    }
  })

  it('answers 405 with Allow: GET on non-GET methods', async () => {
    const h = await startHarness(true)
    for (const path of ['projects', 'search?q=x', 'lineage/s1', 'session/s1/timeline']) {
      const reply = await get(h.base, `${API_PREFIX}/${path}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
      })
      expect(reply.status, path).toBe(405)
      expect(reply.headers.allow, path).toBe('GET')
    }
  })
})
