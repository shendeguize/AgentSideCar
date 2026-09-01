/**
 * Tests for the `archive.*` slice of the `POST action` dispatcher.
 *
 * Same harness philosophy as routes-action.test.ts: a real `node:http`
 * server carrying the dsh webServer shape, with the daemon archive ops
 * faked as a plain object (RoutesDeps takes a structural `ArchiveApi`).
 *
 * The two behaviours worth pinning beyond plumbing:
 * - the inject write gate must NOT apply. Archiving hides a row from this
 *   board and touches no vendor file or process, so requiring `inject.enabled`
 *   would gate an observation-level action behind a write permission.
 * - malformed envelopes must be rejected before the daemon is reached, so a
 *   typo can never turn into a partial batch.
 */

import { createServer, request as httpRequest, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, describe, expect, it } from 'vitest'
import { SessionStore } from '../src/session-store.ts'
import { DaemonSupervisor } from '../src/supervisor.ts'
import {
  API_PREFIX,
  createRoutes,
  type ArchiveApi,
  type ArchiveTarget,
  type InjectGatewayApi,
  type Routes,
} from '../src/routes.ts'

/**
 * The action route keeps its M1 no-gateway placeholder (write gate, then
 * 501) ahead of envelope parsing, so every dispatcher test needs a gateway
 * present even when it is never called. The real plugin always assembles
 * one.
 */
const UNUSED_GATEWAY: InjectGatewayApi = {
  prepare: () => {
    throw new Error('inject must not be reached in archive route tests')
  },
  execute: () => {
    throw new Error('inject must not be reached in archive route tests')
  },
}

function makeSupervisor(): DaemonSupervisor {
  return new DaemonSupervisor(
    {
      ping: async () => null,
      spawnDaemon: () => {
        throw new Error('spawn must not be reached in archive route tests')
      },
      detectLaunchAgent: async () => false,
      log: () => {},
    },
    { policy: 'off' },
  )
}

interface ArchiveCall {
  op: 'preview' | 'apply' | 'unarchive' | 'list'
  idleSeconds?: number
  statuses?: readonly string[]
  targets?: readonly ArchiveTarget[] | 'all'
  token?: string
}

interface FakeArchive {
  api: ArchiveApi
  calls: ArchiveCall[]
  /** Set to make every op reject with this coded daemon error. */
  failure: { code: string } | null
}

function makeArchive(): FakeArchive {
  const calls: ArchiveCall[] = []
  const state: FakeArchive = {
    calls,
    failure: null,
    api: {
      preview: async (idleSeconds, statuses) => {
        calls.push({ op: 'preview', idleSeconds, ...(statuses !== undefined ? { statuses } : {}) })
        if (state.failure !== null) throw Object.assign(new Error('x'), state.failure)
        return { idle_seconds: idleSeconds, candidates: [], count: 0, token: 'tok' }
      },
      apply: async (targets, token) => {
        calls.push({ op: 'apply', targets, token })
        if (state.failure !== null) throw Object.assign(new Error('x'), state.failure)
        return { count: targets.length, requested: targets.length }
      },
      unarchive: async (targets) => {
        calls.push({ op: 'unarchive', targets })
        if (state.failure !== null) throw Object.assign(new Error('x'), state.failure)
        return { count: targets === 'all' ? 7 : targets.length }
      },
      list: async () => {
        calls.push({ op: 'list' })
        if (state.failure !== null) throw Object.assign(new Error('x'), state.failure)
        return { archived: [] }
      },
    },
  }
  return state
}

interface Harness {
  base: URL
  server: Server
  routes: Routes
  archive: FakeArchive
  inject: { enabled: boolean }
  close(): Promise<void>
}

const harnesses: Harness[] = []

afterEach(async () => {
  for (const harness of harnesses.splice(0)) await harness.close()
})

async function startHarness(init: { withArchive?: boolean } = {}): Promise<Harness> {
  const supervisor = makeSupervisor()
  const archive = makeArchive()
  const inject = { enabled: false }
  const routes = createRoutes({
    store: new SessionStore(),
    supervisor,
    guardOptions: { allowWriteActions: () => inject.enabled },
    injectGateway: UNUSED_GATEWAY,
    ...(init.withArchive === false ? {} : { archive: archive.api }),
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
    base: new URL(`http://127.0.0.1:${port}`),
    server,
    routes,
    archive,
    inject,
    close: async () => {
      routes.dispose()
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

function postAction(
  base: URL,
  envelope: unknown,
): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      {
        host: base.hostname,
        port: base.port,
        path: `${API_PREFIX}/action`,
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        agent: false,
      },
      (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk: string) => {
          body += chunk
        })
        res.on('end', () => resolve({ status: res.statusCode ?? 0, body }))
      },
    )
    req.on('error', reject)
    req.write(JSON.stringify(envelope))
    req.end()
  })
}

describe('POST action archive.preview', () => {
  it('forwards the threshold and status filter, with inject still disabled', async () => {
    const harness = await startHarness()

    const reply = await postAction(harness.base, {
      type: 'archive.preview',
      idleSeconds: 7200,
      statuses: ['idle', 'dead'],
    })

    expect(harness.inject.enabled).toBe(false)
    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toMatchObject({ token: 'tok', count: 0 })
    expect(harness.archive.calls).toEqual([
      { op: 'preview', idleSeconds: 7200, statuses: ['idle', 'dead'] },
    ])
  })

  it.each([
    ['a missing threshold', { type: 'archive.preview' }],
    ['a zero threshold', { type: 'archive.preview', idleSeconds: 0 }],
    ['a negative threshold', { type: 'archive.preview', idleSeconds: -1 }],
    ['a non-numeric threshold', { type: 'archive.preview', idleSeconds: '2h' }],
    ['non-string statuses', { type: 'archive.preview', idleSeconds: 60, statuses: [1] }],
  ])('rejects %s before reaching the daemon', async (_label, envelope) => {
    const harness = await startHarness()

    const reply = await postAction(harness.base, envelope)

    expect(reply.status).toBe(400)
    expect(JSON.parse(reply.body).reason).toBe('invalid_request')
    expect(harness.archive.calls).toEqual([])
  })
})

describe('POST action archive.apply', () => {
  it('passes the reviewed targets and the preview token through verbatim', async () => {
    const harness = await startHarness()
    const targets = [
      { agent: 'claude', sessionId: 's1' },
      { agent: 'codex', sessionId: 's2' },
    ]

    const reply = await postAction(harness.base, { type: 'archive.apply', targets, token: 'tok' })

    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toEqual({ count: 2, requested: 2 })
    expect(harness.archive.calls).toEqual([{ op: 'apply', targets, token: 'tok' }])
  })

  it.each([
    ['no token', { type: 'archive.apply', targets: [{ agent: 'a', sessionId: 'b' }] }],
    ['an empty target list', { type: 'archive.apply', targets: [], token: 'tok' }],
    ['a malformed target', { type: 'archive.apply', targets: [{ agent: 'a' }], token: 'tok' }],
  ])('rejects an apply with %s', async (_label, envelope) => {
    const harness = await startHarness()

    const reply = await postAction(harness.base, envelope)

    expect(reply.status).toBe(400)
    expect(harness.archive.calls).toEqual([])
  })

  it('maps a rejected token to 409 rather than a generic failure', async () => {
    const harness = await startHarness()
    harness.archive.failure = { code: 'invalid_token' }

    const reply = await postAction(harness.base, {
      type: 'archive.apply',
      targets: [{ agent: 'claude', sessionId: 's1' }],
      token: 'stale',
    })

    expect(reply.status).toBe(409)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'invalid_token' })
  })

  it('maps an unreachable daemon to 503', async () => {
    const harness = await startHarness()
    harness.archive.failure = { code: 'connection_failed' }

    const reply = await postAction(harness.base, {
      type: 'archive.apply',
      targets: [{ agent: 'claude', sessionId: 's1' }],
      token: 'tok',
    })

    expect(reply.status).toBe(503)
  })
})

describe('POST action archive.unarchive', () => {
  it('supports the bulk form', async () => {
    const harness = await startHarness()

    const reply = await postAction(harness.base, { type: 'archive.unarchive', all: true })

    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toEqual({ count: 7 })
    expect(harness.archive.calls).toEqual([{ op: 'unarchive', targets: 'all' }])
  })

  it('rejects an envelope carrying neither targets nor all', async () => {
    const harness = await startHarness()

    const reply = await postAction(harness.base, { type: 'archive.unarchive' })

    expect(reply.status).toBe(400)
    expect(harness.archive.calls).toEqual([])
  })
})

describe('POST action archive.* without daemon support', () => {
  it('answers 501 archive_unavailable instead of pretending to succeed', async () => {
    const harness = await startHarness({ withArchive: false })

    const reply = await postAction(harness.base, { type: 'archive.list' })

    expect(reply.status).toBe(501)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'archive_unavailable' })
  })
})
