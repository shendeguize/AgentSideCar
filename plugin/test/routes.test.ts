/**
 * Tests for the M1 route layer (src/routes.ts).
 *
 * Primary harness: a real `node:http` server whose request listener is
 * exactly the dsh webServer carrier shape — it calls `routes.handle` and
 * answers 400 on rejection, mirroring
 * `packages/host/webserver/src/index.ts:181-194`. That is the most
 * faithful mount because the dsh prefix-handler contract IS plain
 * `(IncomingMessage, ServerResponse)`.
 *
 * Structural req/res mocks are used only where a real loopback socket
 * cannot express the scenario: a non-loopback peer address and SSE
 * write backpressure.
 */

import { EventEmitter } from 'node:events'
import { createServer, request as httpRequest, type IncomingHttpHeaders, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import { connect as netConnect, type AddressInfo } from 'node:net'
import { afterEach, describe, expect, it } from 'vitest'
import type { SessionRow } from '../src/bridge.ts'
import type { InjectErrorCode } from '../src/inject-gateway.ts'
import { SessionStore } from '../src/session-store.ts'
import { DaemonSupervisor } from '../src/supervisor.ts'
import {
  API_PREFIX,
  createRoutes,
  type InjectGatewayApi,
  type Routes,
  type RoutesOptions,
} from '../src/routes.ts'

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

function row(id: string, over: Partial<SessionRow> = {}): SessionRow {
  return {
    agent: 'claude',
    session_id: id,
    project: '/tmp/proj',
    transcript: '/tmp/proj/t.jsonl',
    updated_at: 1000,
    title: 'title',
    status: 'working',
    extra: {},
    parent_id: null,
    ...over,
  }
}

/**
 * Real supervisor, never probing: constructed idle it reports 'probe' /
 * lastPing null; `start()` under policy 'off' transitions to 'defer',
 * which is the cheapest way to fire a genuine onStateChange.
 */
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

interface LogEntry {
  level: string
  msg: string
}

interface Fixture {
  store: SessionStore
  supervisor: DaemonSupervisor
  routes: Routes
  inject: { enabled: boolean }
  logs: LogEntry[]
}

function makeFixture(
  opts: RoutesOptions = {},
  injectGateway?: InjectGatewayApi,
): Fixture {
  const store = new SessionStore()
  const supervisor = makeSupervisor()
  const inject = { enabled: false }
  const logs: LogEntry[] = []
  const routes = createRoutes(
    {
      store,
      supervisor,
      guardOptions: { allowWriteActions: () => inject.enabled },
      ...(injectGateway === undefined ? {} : { injectGateway }),
      log: (level, msg) => {
        logs.push({ level, msg })
      },
    },
    opts,
  )
  return { store, supervisor, routes, inject, logs }
}

interface Harness extends Fixture {
  base: URL
  server: Server
  close(): Promise<void>
}

const harnesses: Harness[] = []
const fixtures: Fixture[] = []

afterEach(async () => {
  for (const fixture of fixtures.splice(0)) {
    fixture.routes.dispose()
    await fixture.supervisor.stop()
  }
  for (const harness of harnesses.splice(0)) await harness.close()
})

async function startHarness(
  opts: RoutesOptions = {},
  injectGateway?: InjectGatewayApi,
): Promise<Harness> {
  const fixture = makeFixture(opts, injectGateway)
  // Carrier shape of the dsh webServer: dispatch, catch, 400.
  const server = createServer((req, res) => {
    fixture.routes.handle(req, res).catch(() => {
      if (!res.headersSent) {
        res.writeHead(400)
        res.end()
      }
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as AddressInfo
  const harness: Harness = {
    ...fixture,
    base: new URL(`http://127.0.0.1:${port}`),
    server,
    close: async () => {
      fixture.routes.dispose()
      await new Promise<void>((resolve) => {
        server.close(() => resolve())
        server.closeAllConnections()
      })
      await fixture.supervisor.stop()
    },
  }
  harnesses.push(harness)
  return harness
}

// ---------------------------------------------------------------------------
// HTTP helpers (node:http with agent:false so no sockets outlive a test).
// ---------------------------------------------------------------------------

interface HttpReply {
  status: number
  headers: IncomingHttpHeaders
  body: string
}

function request(
  base: URL,
  path: string,
  init: { method?: string; headers?: Record<string, string>; body?: string } = {},
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
        res.on('end', () => resolve({ status: res.statusCode ?? 0, headers: res.headers, body }))
      },
    )
    req.on('error', reject)
    if (init.body !== undefined) req.write(init.body)
    req.end()
  })
}

/** Send exact header lines so duplicate singleton fields reach rawHeaders. */
function rawRequest(base: URL, path: string, headerLines: string[]): Promise<{ status: number; raw: string }> {
  return new Promise((resolve, reject) => {
    const socket = netConnect({
      host: base.hostname,
      port: Number(base.port),
    })
    let raw = ''
    let settled = false
    const fail = (error: Error): void => {
      if (settled) return
      settled = true
      reject(error)
    }
    socket.setEncoding('utf8')
    socket.setTimeout(2000, () => {
      socket.destroy()
      fail(new Error('timed out waiting for raw HTTP response'))
    })
    socket.on('error', fail)
    socket.on('data', (chunk: string) => {
      raw += chunk
    })
    socket.on('end', () => {
      if (settled) return
      const match = /^HTTP\/1\.[01] (\d{3})\b/.exec(raw)
      if (match === null) {
        fail(new Error(`invalid raw HTTP response: ${raw}`))
        return
      }
      settled = true
      resolve({ status: Number(match[1]), raw })
    })
    socket.on('connect', () => {
      socket.end([
        `GET ${path} HTTP/1.1`,
        ...headerLines,
        'Connection: close',
        '',
        '',
      ].join('\r\n'))
    })
  })
}

function raceTimeout<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out waiting for ${what}`)), ms)
    promise.then(
      (value) => {
        clearTimeout(timer)
        resolve(value)
      },
      (error: unknown) => {
        clearTimeout(timer)
        reject(error instanceof Error ? error : new Error(String(error)))
      },
    )
  })
}

// ---------------------------------------------------------------------------
// SSE reader (fetch + incremental frame parsing).
// ---------------------------------------------------------------------------

interface SseFrame {
  event?: string
  data?: string
  comment?: string
}

function parseFrame(raw: string): SseFrame {
  const frame: SseFrame = {}
  for (const line of raw.split('\n')) {
    if (line.startsWith(':')) {
      frame.comment = (frame.comment === undefined ? '' : `${frame.comment}\n`) + line.slice(1).trim()
    } else if (line.startsWith('event:')) {
      frame.event = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      const piece = line.slice('data:'.length).trim()
      frame.data = frame.data === undefined ? piece : `${frame.data}\n${piece}`
    }
  }
  return frame
}

interface SseConnection {
  nextFrame(timeoutMs?: number): Promise<SseFrame>
  /** Skip frames until an `event: state`; returns its parsed JSON data. */
  nextState(timeoutMs?: number): Promise<Record<string, any>>
  /** Skip frames until a comment frame; returns the comment text. */
  nextComment(timeoutMs?: number): Promise<string>
  /** Resolves when the server ends the stream. */
  waitEnd(timeoutMs?: number): Promise<void>
  close(): void
}

async function openStream(harness: Harness): Promise<SseConnection> {
  const controller = new AbortController()
  const response = await fetch(new URL(`${API_PREFIX}/stream`, harness.base), {
    signal: controller.signal,
  })
  expect(response.status).toBe(200)
  expect(response.headers.get('content-type')).toBe('text/event-stream')
  expect(response.headers.get('cache-control')).toBe('no-cache')
  const reader = response.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const nextFrame = async (timeoutMs = 2000): Promise<SseFrame> => {
    const deadline = Date.now() + timeoutMs
    for (;;) {
      const boundary = buffer.indexOf('\n\n')
      if (boundary !== -1) {
        const raw = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        return parseFrame(raw)
      }
      const remaining = deadline - Date.now()
      if (remaining <= 0) throw new Error('timed out waiting for sse frame')
      const chunk = await raceTimeout(reader.read(), remaining, 'sse chunk')
      if (chunk.done) throw new Error('sse stream ended while waiting for a frame')
      buffer += decoder.decode(chunk.value, { stream: true })
    }
  }

  return {
    nextFrame,
    nextState: async (timeoutMs = 2000) => {
      const deadline = Date.now() + timeoutMs
      for (;;) {
        const frame = await nextFrame(Math.max(1, deadline - Date.now()))
        if (frame.event === 'state' && frame.data !== undefined) {
          return JSON.parse(frame.data) as Record<string, any>
        }
      }
    },
    nextComment: async (timeoutMs = 2000) => {
      const deadline = Date.now() + timeoutMs
      for (;;) {
        const frame = await nextFrame(Math.max(1, deadline - Date.now()))
        if (frame.comment !== undefined) return frame.comment
      }
    },
    waitEnd: async (timeoutMs = 2000) => {
      for (;;) {
        const chunk = await raceTimeout(reader.read(), timeoutMs, 'sse stream end')
        if (chunk.done) return
      }
    },
    close: () => {
      controller.abort()
    },
  }
}

// ---------------------------------------------------------------------------
// Structural mocks (only for scenarios a real loopback socket cannot express).
// ---------------------------------------------------------------------------

function mockReq(init: { method?: string; url?: string; remoteAddress?: string } = {}): IncomingMessage {
  return {
    method: init.method ?? 'GET',
    url: init.url ?? `${API_PREFIX}/state`,
    headers: { host: '127.0.0.1:3000' },
    socket: { remoteAddress: init.remoteAddress ?? '127.0.0.1' },
    resume: () => {},
  } as unknown as IncomingMessage
}

class MockRes extends EventEmitter {
  statusCode: number | undefined
  headers: Record<string, unknown> = {}
  body = ''
  ended = false
  destroyed = false
  /** Return value of write(): false simulates permanent backpressure. */
  writeReturn = true

  writeHead(status: number, headers?: Record<string, unknown>): this {
    this.statusCode = status
    if (headers !== undefined) Object.assign(this.headers, headers)
    return this
  }

  write(chunk: unknown): boolean {
    this.body += String(chunk)
    return this.writeReturn
  }

  end(chunk?: unknown): this {
    if (chunk !== undefined) this.body += String(chunk)
    this.ended = true
    return this
  }

  destroy(): this {
    this.destroyed = true
    this.emit('close')
    return this
  }

  asRes(): ServerResponse {
    return this as unknown as ServerResponse
  }
}

// ---------------------------------------------------------------------------
// Guard integration.
// ---------------------------------------------------------------------------

describe('guard integration', () => {
  it('rejects a non-loopback peer with the verdict status and reason as JSON', async () => {
    const fixture = makeFixture()
    fixtures.push(fixture)
    const res = new MockRes()
    await fixture.routes.handle(mockReq({ remoteAddress: '192.168.1.5' }), res.asRes())
    expect(res.statusCode).toBe(403)
    expect(res.ended).toBe(true)
    expect(JSON.parse(res.body)).toEqual({ reason: 'remote_not_loopback' })
  })

  it('rejects a forged Host header on a real loopback connection (DNS rebinding gate)', async () => {
    const harness = await startHarness()
    const reply = await request(harness.base, `${API_PREFIX}/state`, {
      headers: { host: 'evil.example' },
    })
    expect(reply.status).toBe(403)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'host_not_loopback' })
  })

  it('rejects an HTTPS Origin on the real cleartext loopback carrier without route data', async () => {
    const harness = await startHarness()
    harness.store.applySnapshot([row('must-not-leak')])
    const reply = await request(harness.base, `${API_PREFIX}/state`, {
      headers: { origin: `https://${harness.base.host}` },
    })
    expect(reply.status).toBe(403)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'origin_mismatch' })
  })

  it.each([
    {
      name: 'same duplicate Host',
      headers: (authority: string) => [`Host: ${authority}`, `host: ${authority}`],
      reason: 'host_not_loopback',
    },
    {
      name: 'different duplicate Host',
      headers: (authority: string) => [`HOST: ${authority}`, 'Host: evil.example'],
      reason: 'host_not_loopback',
    },
    {
      name: 'same duplicate Origin',
      headers: (authority: string) => [
        `Host: ${authority}`,
        `Origin: http://${authority}`,
        `origin: http://${authority}`,
      ],
      reason: 'origin_mismatch',
    },
    {
      name: 'different duplicate Origin',
      headers: (authority: string) => [
        `Host: ${authority}`,
        `ORIGIN: http://${authority}`,
        'Origin: http://evil.example',
      ],
      reason: 'origin_mismatch',
    },
  ])('rejects $name from raw TCP before any route body', async ({ headers, reason }) => {
    const harness = await startHarness()
    harness.store.applySnapshot([row('must-not-leak')])
    const reply = await rawRequest(
      harness.base,
      `${API_PREFIX}/state`,
      headers(harness.base.host),
    )
    expect(reply.status).toBe(403)
    expect(reply.raw).toContain(`"reason":"${reason}"`)
    expect(reply.raw).not.toContain('must-not-leak')
  })
})

// ---------------------------------------------------------------------------
// GET state.
// ---------------------------------------------------------------------------

describe('GET state', () => {
  it('returns daemon + board + capabilities, with capabilities read live', async () => {
    const harness = await startHarness()
    harness.store.applySnapshot([
      row('older', { updated_at: 5 }),
      row('newer', { updated_at: 9, status: 'waiting' }),
    ])

    const reply = await request(harness.base, `${API_PREFIX}/state`)
    expect(reply.status).toBe(200)
    expect(reply.headers['content-type']).toContain('application/json')
    const body = JSON.parse(reply.body)
    expect(body.daemon).toEqual({ state: 'probe', lastPing: null })
    expect(body.board.sessions.map((s: any) => s.session_id)).toEqual(['newer', 'older'])
    expect(body.board.streamHealth).toBe('unknown')
    expect(typeof body.board.lastReconcileAt).toBe('number')
    // No dispose dep in this harness, so the capability is false regardless
    // of the write gate (dsh-dispose.test.ts covers the matrix).
    expect(body.capabilities).toEqual({ inject: false, dispose: false })

    harness.inject.enabled = true
    const flipped = JSON.parse((await request(harness.base, `${API_PREFIX}/state`)).body)
    expect(flipped.capabilities).toEqual({ inject: true, dispose: false })
  })

  it('projects only the derived verdict and never raw extra or parent topology', async () => {
    const harness = await startHarness()
    const secret = 'PRIVATE-parent-value'
    harness.store.applySnapshot([
      row('safe-wire', {
        status: 'waiting',
        extra: { nested: { token: secret } },
        parent_id: secret,
      }),
    ])

    const reply = await request(harness.base, `${API_PREFIX}/state`)
    const session = JSON.parse(reply.body).board.sessions[0]
    expect(session.inject_eligibility).toEqual({
      allowed: false,
      reason: 'child_session',
    })
    expect(session).not.toHaveProperty('extra')
    expect(session).not.toHaveProperty('parent_id')
    expect(session).not.toHaveProperty('transcript')
    expect(reply.body).not.toContain(secret)
  })

  it('projects remote_session when explicit remote and child markers coexist', async () => {
    const harness = await startHarness()
    const secret = 'PRIVATE-remote-child-value'
    harness.store.applySnapshot([
      row('remote-child', {
        status: 'waiting',
        extra: { host: secret },
        parent_id: secret,
      }),
    ])

    const reply = await request(harness.base, `${API_PREFIX}/state`)
    const session = JSON.parse(reply.body).board.sessions[0]
    expect(session.inject_eligibility).toEqual({
      allowed: false,
      reason: 'remote_session',
    })
    expect(reply.body).not.toContain(secret)
  })

  it('answers 405 with an Allow header for non-GET methods', async () => {
    const harness = await startHarness()
    const reply = await request(harness.base, `${API_PREFIX}/state`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    })
    expect(reply.status).toBe(405)
    expect(reply.headers.allow).toBe('GET')
    expect(JSON.parse(reply.body)).toEqual({ reason: 'method_not_allowed' })
  })
})

// ---------------------------------------------------------------------------
// GET session/<id>.
// ---------------------------------------------------------------------------

describe('GET session/<id>', () => {
  it('returns the SessionView with the M3 timeline placeholder, decoding the id', async () => {
    const harness = await startHarness()
    harness.store.applySnapshot([row('plain-id'), row('needs/escape')])

    const reply = await request(harness.base, `${API_PREFIX}/session/plain-id`)
    expect(reply.status).toBe(200)
    const body = JSON.parse(reply.body)
    expect(body.session.session_id).toBe('plain-id')
    expect(body.session.agent).toBe('claude')
    expect(body.session.gap).toBe(false)
    expect(body.timeline).toBeNull()
    expect(body.timelineNote).toBe('timeline_not_available_until_m3')

    const encoded = await request(harness.base, `${API_PREFIX}/session/needs%2Fescape`)
    expect(encoded.status).toBe(200)
    expect(JSON.parse(encoded.body).session.session_id).toBe('needs/escape')
  })

  it('answers 404 for an unknown or empty id', async () => {
    const harness = await startHarness()
    harness.store.applySnapshot([row('known')])

    const missing = await request(harness.base, `${API_PREFIX}/session/nope`)
    expect(missing.status).toBe(404)
    expect(JSON.parse(missing.body)).toEqual({ reason: 'session_not_found' })

    const empty = await request(harness.base, `${API_PREFIX}/session/`)
    expect(empty.status).toBe(404)
    expect(JSON.parse(empty.body)).toEqual({ reason: 'session_not_found' })
  })
})

// ---------------------------------------------------------------------------
// GET stream (SSE).
// ---------------------------------------------------------------------------

describe('GET stream (SSE)', () => {
  it('sends the initial snapshot, then pushes on store and supervisor changes', async () => {
    const harness = await startHarness()
    harness.store.applySnapshot([row('s1', { updated_at: 111 })])

    const sse = await openStream(harness)
    const initial = await sse.nextState()
    expect(initial.daemon).toEqual({ state: 'probe', lastPing: null })
    expect(initial.board.sessions.map((s: any) => s.session_id)).toEqual(['s1'])
    expect(initial.capabilities).toEqual({ inject: false, dispose: false })

    harness.store.applySnapshot([row('s1', { updated_at: 111 }), row('s2', { updated_at: 999 })])
    const afterStore = await sse.nextState()
    expect(afterStore.board.sessions.map((s: any) => s.session_id)).toEqual(['s2', 's1'])

    // policy 'off' start(): probe → defer, a genuine supervisor transition.
    harness.supervisor.start()
    const afterSupervisor = await sse.nextState()
    expect(afterSupervisor.daemon.state).toBe('defer')

    sse.close()
  })

  it('pushes eligibility transitions from authoritative status snapshots', async () => {
    const harness = await startHarness()
    harness.store.applySnapshot([row('transition', { status: 'working' })])
    const sse = await openStream(harness)

    const initial = await sse.nextState()
    expect(initial.board.sessions[0].inject_eligibility).toEqual({
      allowed: false,
      reason: 'working_session',
    })

    harness.store.applySnapshot([row('transition', { status: 'waiting' })])
    const updated = await sse.nextState()
    expect(updated.board.sessions[0].inject_eligibility).toEqual({
      allowed: true,
      reason: 'eligible',
    })
    sse.close()
  })

  it('emits heartbeat comment frames on the configured cadence', async () => {
    const harness = await startHarness({ sseHeartbeatMs: 25 })
    const sse = await openStream(harness)
    await sse.nextState()
    const comment = await sse.nextComment()
    expect(comment).toBe('hb')
    sse.close()
  })

  it('rejects connections above maxSseClients with 503', async () => {
    const harness = await startHarness({ maxSseClients: 1 })
    const sse = await openStream(harness)
    await sse.nextState()

    const rejected = await request(harness.base, `${API_PREFIX}/stream`)
    expect(rejected.status).toBe(503)
    expect(JSON.parse(rejected.body)).toEqual({ reason: 'too_many_stream_clients' })

    sse.close()
  })

  it('drops a backpressured connection once its pending buffer exceeds the limit', async () => {
    const fixture = makeFixture({ sseBufferLimit: 2, sseHeartbeatMs: 60_000 })
    fixtures.push(fixture)
    const res = new MockRes()
    res.writeReturn = false // every write reports backpressure, drain never fires
    await fixture.routes.handle(mockReq({ url: `${API_PREFIX}/stream` }), res.asRes())
    expect(res.statusCode).toBe(200)
    expect(res.body).toContain('event: state') // initial snapshot was written

    fixture.store.applySnapshot([row('a')]) // pending 1
    fixture.store.applySnapshot([row('a')]) // pending 2
    expect(res.destroyed).toBe(false)
    fixture.store.applySnapshot([row('a')]) // pending 3 > limit → dropped
    expect(res.destroyed).toBe(true)
    expect(fixture.logs.some((l) => l.level === 'warn' && l.msg === 'sse client dropped')).toBe(true)

    // The dropped client is fully detached: further changes write nothing.
    const bodyAfterDrop = res.body
    fixture.store.applySnapshot([row('b')])
    expect(res.body).toBe(bodyAfterDrop)
  })
})

// ---------------------------------------------------------------------------
// POST action (M2 placeholder behind the live write gate).
// ---------------------------------------------------------------------------

describe('POST action', () => {
  const envelope = JSON.stringify({ requestId: 'r1', method: 'inject.prepare', args: {} })

  it('answers 403 inject_disabled while the write gate is off', async () => {
    const harness = await startHarness()
    const reply = await request(harness.base, `${API_PREFIX}/action`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: envelope,
    })
    expect(reply.status).toBe(403)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'inject_disabled' })
  })

  it('answers 501 not_implemented_until_m2 once the write gate is open', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true
    const reply = await request(harness.base, `${API_PREFIX}/action`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: envelope,
    })
    expect(reply.status).toBe(501)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'not_implemented_until_m2' })
  })

  it('answers 415 for a non-JSON POST (guard layer 4) before the write gate', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true
    const reply = await request(harness.base, `${API_PREFIX}/action`, {
      method: 'POST',
      headers: { 'content-type': 'text/plain' },
      body: 'nope',
    })
    expect(reply.status).toBe(415)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'unsupported_media_type' })
  })

  it('answers 405 for non-POST methods (dispatcher placeholder)', async () => {
    const harness = await startHarness()
    const reply = await request(harness.base, `${API_PREFIX}/action`)
    expect(reply.status).toBe(405)
    expect(reply.headers.allow).toBe('POST')
  })

  it.each([
    ['unsupported_agent', 422],
    ['dead_session', 409],
    ['working_session', 409],
    ['child_session', 422],
    ['remote_session', 422],
    ['invalid_session', 422],
  ] as const)('maps eligibility reason %s consistently in both phases', async (reason, status) => {
    let errorCode: InjectErrorCode = reason
    const gateway: InjectGatewayApi = {
      prepare: async () => ({ ok: false, errorCode }),
      execute: async () => ({ outcome: 'failed', errorCode }),
    }
    const harness = await startHarness({}, gateway)
    harness.inject.enabled = true

    const prepared = await request(harness.base, `${API_PREFIX}/action`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        type: 'inject.prepare',
        target: { agent: 'claude', sessionId: 's1' },
        mode: 'queue',
        message: 'hello',
      }),
    })
    expect(prepared.status).toBe(status)
    expect(JSON.parse(prepared.body)).toEqual({ reason })

    errorCode = reason
    const executed = await request(harness.base, `${API_PREFIX}/action`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        type: 'inject.execute',
        requestId: 'req-1',
        confirmToken: 'token',
        message: 'hello',
      }),
    })
    expect(executed.status).toBe(status)
    expect(JSON.parse(executed.body)).toEqual({
      outcome: 'failed',
      errorCode: reason,
    })
  })
})

// ---------------------------------------------------------------------------
// Dispatch fallthrough.
// ---------------------------------------------------------------------------

describe('dispatch', () => {
  it('answers 404 for unknown subpaths and the bare prefix', async () => {
    const harness = await startHarness()
    const unknown = await request(harness.base, `${API_PREFIX}/nope`)
    expect(unknown.status).toBe(404)
    expect(JSON.parse(unknown.body)).toEqual({ reason: 'not_found' })

    const bare = await request(harness.base, API_PREFIX)
    expect(bare.status).toBe(404)
    expect(JSON.parse(bare.body)).toEqual({ reason: 'not_found' })
  })
})

// ---------------------------------------------------------------------------
// dispose.
// ---------------------------------------------------------------------------

describe('dispose', () => {
  it('ends SSE streams, unsubscribes, refuses later requests, and is idempotent', async () => {
    const harness = await startHarness()
    harness.store.applySnapshot([row('s1')])
    const sse = await openStream(harness)
    await sse.nextState()

    harness.routes.dispose()
    await sse.waitEnd()

    // Listeners are gone: mutating the store after dispose must not throw.
    harness.store.applySnapshot([row('s2')])
    harness.supervisor.start()

    const after = await request(harness.base, `${API_PREFIX}/state`)
    expect(after.status).toBe(503)
    expect(JSON.parse(after.body)).toEqual({ reason: 'shutting_down' })

    harness.routes.dispose() // second call is a no-op
  })
})
