/**
 * Tests for the M2 `POST action` dispatcher (src/routes.ts + fake
 * InjectGateway / real idle DaemonSupervisor).
 *
 * Same harness philosophy as routes.test.ts: a real `node:http` server
 * whose request listener is the dsh webServer carrier shape, because the
 * action route now reads a real request body stream. The gateway is a
 * plain-object fake (RoutesDeps takes `Pick<InjectGateway, ...>`), so every
 * status-code mapping is pinned without touching gateway internals.
 */

import { createServer, request as httpRequest, type IncomingHttpHeaders, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, describe, expect, it } from 'vitest'
import type {
  ExecuteRequest,
  InjectPlan,
  InjectResult,
  PrepareRequest,
  PrepareResult,
} from '../src/inject-gateway.ts'
import { SessionStore } from '../src/session-store.ts'
import { DaemonSupervisor } from '../src/supervisor.ts'
import {
  API_PREFIX,
  createRoutes,
  MAX_ACTION_BODY_BYTES,
  type InjectGatewayApi,
  type Routes,
} from '../src/routes.ts'

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

/** Real supervisor, never started: state stays 'probe', retry() is a no-op. */
function makeSupervisor(): DaemonSupervisor {
  return new DaemonSupervisor(
    {
      ping: async () => null,
      spawnDaemon: () => {
        throw new Error('spawn must not be reached in action route tests')
      },
      detectLaunchAgent: async () => false,
      log: () => {},
    },
    { policy: 'off' },
  )
}

function makePlan(over: Partial<InjectPlan> = {}): InjectPlan {
  return {
    target: { agent: 'claude', sessionId: 's1' },
    mode: 'queue',
    targetStatus: { agent: 'claude', sessionId: 's1', status: 'waiting' },
    messagePreview: { bytes: 5, head: 'hello' },
    ...over,
  }
}

interface FakeGateway {
  api: InjectGatewayApi
  prepareCalls: PrepareRequest[]
  executeCalls: ExecuteRequest[]
  /** Next result each fake method resolves with; mutate per test. */
  results: { prepare: PrepareResult; execute: InjectResult }
}

function makeGateway(): FakeGateway {
  const prepareCalls: PrepareRequest[] = []
  const executeCalls: ExecuteRequest[] = []
  const results: FakeGateway['results'] = {
    prepare: {
      ok: true,
      requestId: 'req-1',
      confirmToken: 'tok-1',
      plan: makePlan(),
      expiresAt: 60_000,
    },
    execute: { outcome: 'delivered' },
  }
  const api: InjectGatewayApi = {
    prepare: async (req) => {
      prepareCalls.push(req)
      return results.prepare
    },
    execute: async (req) => {
      executeCalls.push(req)
      return results.execute
    },
  }
  return { api, prepareCalls, executeCalls, results }
}

interface LogEntry {
  level: string
  msg: string
  meta?: object
}

interface Harness {
  base: URL
  server: Server
  routes: Routes
  store: SessionStore
  supervisor: DaemonSupervisor
  gateway: FakeGateway
  inject: { enabled: boolean }
  logs: LogEntry[]
  close(): Promise<void>
}

const harnesses: Harness[] = []

afterEach(async () => {
  for (const harness of harnesses.splice(0)) await harness.close()
})

async function startHarness(init: { withGateway?: boolean } = {}): Promise<Harness> {
  const store = new SessionStore()
  const supervisor = makeSupervisor()
  const gateway = makeGateway()
  const inject = { enabled: false }
  const logs: LogEntry[] = []
  const routes = createRoutes({
    store,
    supervisor,
    guardOptions: { allowWriteActions: () => inject.enabled },
    ...(init.withGateway === false ? {} : { injectGateway: gateway.api }),
    log: (level, msg, meta) => {
      logs.push({ level, msg, ...(meta !== undefined ? { meta } : {}) })
    },
  })
  // Carrier shape of the dsh webServer: dispatch, catch, 400.
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
    store,
    supervisor,
    gateway,
    inject,
    logs,
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

/** POST the action envelope as JSON (raw strings pass through untouched). */
function postAction(base: URL, envelope: unknown): Promise<HttpReply> {
  return request(base, `${API_PREFIX}/action`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: typeof envelope === 'string' ? envelope : JSON.stringify(envelope),
  })
}

const PREPARE_ENVELOPE = {
  type: 'inject.prepare',
  target: { agent: 'claude', sessionId: 's1' },
  mode: 'queue',
  message: 'hello',
}

const EXECUTE_ENVELOPE = {
  type: 'inject.execute',
  requestId: 'req-1',
  confirmToken: 'tok-1',
  message: 'hello',
}

// ---------------------------------------------------------------------------
// inject.prepare.
// ---------------------------------------------------------------------------

describe('POST action inject.prepare', () => {
  it('forwards target/mode/message to the gateway and returns the issued confirmation', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true

    const reply = await postAction(harness.base, PREPARE_ENVELOPE)
    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toEqual({
      requestId: 'req-1',
      confirmToken: 'tok-1',
      plan: makePlan(),
      expiresAt: 60_000,
    })
    expect(harness.gateway.prepareCalls).toEqual([
      { target: { agent: 'claude', sessionId: 's1' }, mode: 'queue', message: 'hello' },
    ])
  })

  it.each([
    ['inject_disabled', 403],
    ['invalid_message', 422],
    ['target_not_found', 404],
    ['target_dead', 409],
    ['too_many_pending', 429],
    ['unsupported_agent', 422], // issued at prepare since M2 review F-6
  ] as const)('maps a %s rejection to %i with reason and detail', async (errorCode, status) => {
    const harness = await startHarness()
    harness.inject.enabled = true
    harness.gateway.results.prepare = { ok: false, errorCode, detail: 'why' }

    const reply = await postAction(harness.base, PREPARE_ENVELOPE)
    expect(reply.status).toBe(status)
    expect(JSON.parse(reply.body)).toEqual({ reason: errorCode, detail: 'why' })
  })

  it('omits detail from the rejection body when the gateway gave none', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true
    harness.gateway.results.prepare = { ok: false, errorCode: 'target_not_found' }

    const reply = await postAction(harness.base, PREPARE_ENVELOPE)
    expect(reply.status).toBe(404)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'target_not_found' })
  })

  it('answers 400 invalid_request for a malformed envelope without calling the gateway', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true

    const reply = await postAction(harness.base, { ...PREPARE_ENVELOPE, mode: 'yolo' })
    expect(reply.status).toBe(400)
    expect(JSON.parse(reply.body).reason).toBe('invalid_request')

    const noTarget = await postAction(harness.base, { type: 'inject.prepare', mode: 'queue', message: 'x' })
    expect(noTarget.status).toBe(400)
    expect(JSON.parse(noTarget.body).reason).toBe('invalid_request')

    expect(harness.gateway.prepareCalls).toHaveLength(0)
  })

  it('answers 403 inject_disabled at the write gate, before the gateway', async () => {
    const harness = await startHarness()

    const reply = await postAction(harness.base, PREPARE_ENVELOPE)
    expect(reply.status).toBe(403)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'inject_disabled' })
    expect(harness.gateway.prepareCalls).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// inject.execute.
// ---------------------------------------------------------------------------

describe('POST action inject.execute', () => {
  it('forwards requestId/confirmToken/message and answers 200 for delivered', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true

    const reply = await postAction(harness.base, EXECUTE_ENVELOPE)
    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toEqual({ outcome: 'delivered' })
    expect(harness.gateway.executeCalls).toEqual([
      { requestId: 'req-1', confirmToken: 'tok-1', message: 'hello' },
    ])
  })

  it('passes the replayed flag through on an idempotent repeat', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true
    harness.gateway.results.execute = { outcome: 'delivered', replayed: true }

    const reply = await postAction(harness.base, EXECUTE_ENVELOPE)
    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toEqual({ outcome: 'delivered', replayed: true })
  })

  it.each([
    ['executor_error', 502],
    ['unsupported_agent', 422],
    ['token_missing', 401],
    ['token_expired', 401],
    ['token_reused', 409],
    ['token_mismatch', 409],
  ] as const)('maps a failed outcome with %s to %i', async (errorCode, status) => {
    const harness = await startHarness()
    harness.inject.enabled = true
    harness.gateway.results.execute = { outcome: 'failed', errorCode, detail: 'why' }

    const reply = await postAction(harness.base, EXECUTE_ENVELOPE)
    expect(reply.status).toBe(status)
    expect(JSON.parse(reply.body)).toEqual({ outcome: 'failed', errorCode, detail: 'why' })
  })

  it('answers 502 for failed outcomes with executor-native or missing codes', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true

    harness.gateway.results.execute = { outcome: 'failed', errorCode: 'audit_error' }
    const native = await postAction(harness.base, EXECUTE_ENVELOPE)
    expect(native.status).toBe(502)

    harness.gateway.results.execute = { outcome: 'failed' }
    const codeless = await postAction(harness.base, EXECUTE_ENVELOPE)
    expect(codeless.status).toBe(502)
  })

  it('answers 200 for outcome unknown and never re-fires the gateway (S6)', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true
    harness.gateway.results.execute = {
      outcome: 'unknown',
      errorCode: 'send_timeout',
      detail: 'no receipt',
    }

    const reply = await postAction(harness.base, EXECUTE_ENVELOPE)
    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toEqual({
      outcome: 'unknown',
      errorCode: 'send_timeout',
      detail: 'no receipt',
    })
    // The terminal-unknown invariant: exactly one dispatch per HTTP request.
    expect(harness.gateway.executeCalls).toHaveLength(1)
  })

  it('answers 400 invalid_request for a malformed envelope without calling the gateway', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true

    const reply = await postAction(harness.base, {
      type: 'inject.execute',
      requestId: 'req-1',
      message: 'hello',
    })
    expect(reply.status).toBe(400)
    expect(JSON.parse(reply.body).reason).toBe('invalid_request')
    expect(harness.gateway.executeCalls).toHaveLength(0)
  })

  it('answers 403 inject_disabled at the write gate, before the gateway', async () => {
    const harness = await startHarness()

    const reply = await postAction(harness.base, EXECUTE_ENVELOPE)
    expect(reply.status).toBe(403)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'inject_disabled' })
    expect(harness.gateway.executeCalls).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// daemon.retry.
// ---------------------------------------------------------------------------

describe('POST action daemon.retry', () => {
  it('invokes supervisor.retry and returns the state, without the write gate', async () => {
    const harness = await startHarness()
    // inject.enabled stays false: daemon management is not injection.
    let retryCalls = 0
    const original = harness.supervisor.retry.bind(harness.supervisor)
    harness.supervisor.retry = () => {
      retryCalls += 1
      original()
    }

    const reply = await postAction(harness.base, { type: 'daemon.retry' })
    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toEqual({ state: 'probe' })
    expect(retryCalls).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// Envelope validation and the M1 fallback.
// ---------------------------------------------------------------------------

describe('POST action envelope validation', () => {
  it('answers 400 unknown_action for unknown, missing, or non-object types', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true

    for (const envelope of [
      { type: 'nope' },
      { method: 'inject.prepare' }, // M1 test shape: no `type` field
      [1, 2, 3],
      42,
      null,
    ]) {
      const reply = await postAction(harness.base, envelope)
      expect(reply.status).toBe(400)
      expect(JSON.parse(reply.body)).toEqual({ reason: 'unknown_action' })
    }
    expect(harness.gateway.prepareCalls).toHaveLength(0)
    expect(harness.gateway.executeCalls).toHaveLength(0)
  })

  it('answers 400 invalid_json for a non-JSON body', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true

    const reply = await postAction(harness.base, 'this is not json {')
    expect(reply.status).toBe(400)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'invalid_json' })
  })

  it('answers 400 body_too_large above the 64 KiB bound', async () => {
    const harness = await startHarness()
    harness.inject.enabled = true

    const oversized = JSON.stringify({
      type: 'inject.prepare',
      message: 'x'.repeat(MAX_ACTION_BODY_BYTES + 1024),
    })
    const reply = await postAction(harness.base, oversized)
    expect(reply.status).toBe(400)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'body_too_large' })
    expect(harness.gateway.prepareCalls).toHaveLength(0)
  })

  it('falls back to the M1 placeholder (write gate, then 501) without a gateway', async () => {
    const harness = await startHarness({ withGateway: false })

    const gated = await postAction(harness.base, { type: 'daemon.retry' })
    expect(gated.status).toBe(403)
    expect(JSON.parse(gated.body)).toEqual({ reason: 'inject_disabled' })

    harness.inject.enabled = true
    const open = await postAction(harness.base, { type: 'daemon.retry' })
    expect(open.status).toBe(501)
    expect(JSON.parse(open.body)).toEqual({ reason: 'not_implemented_until_m2' })
  })
})

// ---------------------------------------------------------------------------
// Log hygiene (S8): message bodies never reach the route log.
// ---------------------------------------------------------------------------

describe('action log hygiene', () => {
  it('never writes the message body or its preview into the route log', async () => {
    const MARKER = 'SECRET_MARKER_do_not_log_5f2c'
    const harness = await startHarness()
    harness.inject.enabled = true
    // Real gateways echo the message head inside plan.messagePreview; the
    // fake does too, so a leak through response-logging would be caught.
    harness.gateway.results.prepare = {
      ok: true,
      requestId: 'req-1',
      confirmToken: 'tok-1',
      plan: makePlan({ messagePreview: { bytes: MARKER.length, head: MARKER } }),
      expiresAt: 60_000,
    }

    const prepared = await postAction(harness.base, { ...PREPARE_ENVELOPE, message: MARKER })
    expect(prepared.status).toBe(200)
    expect(prepared.body).toContain(MARKER) // the response may carry the preview…

    await postAction(harness.base, { ...EXECUTE_ENVELOPE, message: MARKER })

    harness.gateway.results.prepare = { ok: false, errorCode: 'target_dead' }
    await postAction(harness.base, { ...PREPARE_ENVELOPE, message: MARKER })

    expect(harness.logs.length).toBeGreaterThan(0)
    expect(JSON.stringify(harness.logs)).not.toContain(MARKER) // …the log never does
  })
})
