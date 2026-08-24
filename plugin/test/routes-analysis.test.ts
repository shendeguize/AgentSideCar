/**
 * Tests for the M3 `POST action` analysis dispatcher (src/routes.ts +
 * fake AnalysisApi / real idle DaemonSupervisor).
 *
 * Same harness philosophy as routes-action.test.ts: a real `node:http`
 * server whose request listener is the dsh webServer carrier shape. The
 * analysis surface is a plain-object fake (`RoutesDeps.analysis` is
 * structural), so the gate ordering, every status-code mapping, and the
 * degradation ladder are pinned without touching engine internals.
 */

import { createServer, request as httpRequest, type IncomingHttpHeaders, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, describe, expect, it } from 'vitest'
import {
  ANALYSIS_DISCLAIMER,
  type AnalysisInput,
  type AnalysisResult,
} from '../src/analysis.ts'
import { SessionStore } from '../src/session-store.ts'
import { DaemonSupervisor } from '../src/supervisor.ts'
import {
  API_PREFIX,
  createRoutes,
  type AnalysisApi,
  type AnalysisTargetRequest,
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
        throw new Error('spawn must not be reached in analysis route tests')
      },
      detectLaunchAgent: async () => false,
      log: () => {},
    },
    { policy: 'off' },
  )
}

/** Minimal gateway fake: analysis dispatch lives inside the gateway-backed
 * action handler, so the deps must carry one (its methods stay unreached). */
function makeIdleGateway(): InjectGatewayApi {
  return {
    prepare: async () => ({ ok: false, errorCode: 'target_not_found' }),
    execute: async () => ({ outcome: 'failed', errorCode: 'token_missing' }),
  }
}

function makeInput(over: Partial<AnalysisInput> = {}): AnalysisInput {
  return {
    kind: 'session',
    title: 'demo session',
    summaryText: 'summary line',
    ...over,
  }
}

function completedResult(over: Partial<AnalysisResult> = {}): AnalysisResult {
  return {
    outcome: 'completed',
    analysisSessionId: 'ana-1',
    summary: 'looks healthy',
    truncated: false,
    disclaimer: ANALYSIS_DISCLAIMER,
    ...over,
  }
}

interface FakeAnalysis {
  api: AnalysisApi
  buildCalls: AnalysisTargetRequest[]
  requestCalls: AnalysisInput[]
  followupCalls: Array<{ analysisSessionId: string; question: string }>
  cancelCalls: string[]
  /** Next values the fake resolves with; mutate per test. */
  results: {
    build: AnalysisInput | null
    request: AnalysisResult
    followup: AnalysisResult
  }
  available: boolean
}

function makeAnalysis(): FakeAnalysis {
  const buildCalls: AnalysisTargetRequest[] = []
  const requestCalls: AnalysisInput[] = []
  const followupCalls: Array<{ analysisSessionId: string; question: string }> = []
  const cancelCalls: string[] = []
  const results: FakeAnalysis['results'] = {
    build: makeInput(),
    request: completedResult(),
    followup: completedResult(),
  }
  const fake: FakeAnalysis = {
    buildCalls,
    requestCalls,
    followupCalls,
    cancelCalls,
    results,
    available: true,
    api: {
      engine: {
        request: async (input) => {
          requestCalls.push(input)
          return results.request
        },
        followup: async (analysisSessionId, question) => {
          followupCalls.push({ analysisSessionId, question })
          return results.followup
        },
        cancel: async (analysisSessionId) => {
          cancelCalls.push(analysisSessionId)
        },
      },
      buildInput: async (req) => {
        buildCalls.push(req)
        return results.build
      },
      available: () => fake.available,
    },
  }
  return fake
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
  supervisor: DaemonSupervisor
  analysis: FakeAnalysis
  gates: { inject: boolean; analysis: boolean }
  logs: LogEntry[]
  close(): Promise<void>
}

const harnesses: Harness[] = []

afterEach(async () => {
  for (const harness of harnesses.splice(0)) await harness.close()
})

async function startHarness(
  init: {
    withAnalysis?: boolean
    withAnalysisGate?: boolean
    /** When set, wired as the AnalysisApi.modelConfigured probe (A-1). */
    modelConfigured?: () => boolean
  } = {},
): Promise<Harness> {
  const store = new SessionStore()
  const supervisor = makeSupervisor()
  const analysis = makeAnalysis()
  const gates = { inject: false, analysis: false }
  const logs: LogEntry[] = []
  const routes = createRoutes({
    store,
    supervisor,
    guardOptions: { allowWriteActions: () => gates.inject },
    injectGateway: makeIdleGateway(),
    ...(init.withAnalysisGate === false
      ? {}
      : { analysisEnabled: () => gates.analysis }),
    ...(init.withAnalysis === false
      ? {}
      : {
          analysis: {
            ...analysis.api,
            ...(init.modelConfigured !== undefined
              ? { modelConfigured: init.modelConfigured }
              : {}),
          },
        }),
    log: (level, msg, meta) => {
      logs.push({ level, msg, ...(meta !== undefined ? { meta } : {}) })
    },
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
    supervisor,
    analysis,
    gates,
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

function postAction(base: URL, envelope: unknown): Promise<HttpReply> {
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
        res.on('end', () => resolve({ status: res.statusCode ?? 0, headers: res.headers, body }))
      },
    )
    req.on('error', reject)
    req.write(JSON.stringify(envelope))
    req.end()
  })
}

const REQUEST_ENVELOPE = {
  type: 'analysis.request',
  targetKind: 'session',
  targetId: 's1',
  question: 'anything odd?',
}

const FOLLOWUP_ENVELOPE = {
  type: 'analysis.followup',
  analysisSessionId: 'ana-1',
  question: 'and next steps?',
}

const CANCEL_ENVELOPE = { type: 'analysis.cancel', analysisSessionId: 'ana-1' }

// ---------------------------------------------------------------------------
// The analysis write gate (parallel to — and independent of — inject's).
// ---------------------------------------------------------------------------

describe('analysis write gate', () => {
  it.each([
    ['analysis.request', REQUEST_ENVELOPE],
    ['analysis.followup', FOLLOWUP_ENVELOPE],
  ])('refuses %s with 403 analysis_disabled while the gate is closed', async (_type, envelope) => {
    const harness = await startHarness()

    const reply = await postAction(harness.base, envelope)
    expect(reply.status).toBe(403)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'analysis_disabled' })
    expect(harness.analysis.buildCalls).toHaveLength(0)
    expect(harness.analysis.requestCalls).toHaveLength(0)
    expect(harness.analysis.followupCalls).toHaveLength(0)
    expect(harness.analysis.cancelCalls).toHaveLength(0)
  })

  it('lets analysis.cancel bypass the closed gate (F3: kill-switch cleanup stays reachable)', async () => {
    const harness = await startHarness()

    // Gate closed (kill switch flipped mid-session): cancel still reaches
    // the engine — it spends no tokens and only stops/cleans up…
    const cancel = await postAction(harness.base, CANCEL_ENVELOPE)
    expect(cancel.status).toBe(200)
    expect(JSON.parse(cancel.body)).toEqual({ ok: true, analysisSessionId: 'ana-1' })
    expect(harness.analysis.cancelCalls).toEqual(['ana-1'])

    // …while request/followup stay locked out.
    const request = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(request.status).toBe(403)
    expect(JSON.parse(request.body).reason).toBe('analysis_disabled')
    expect(harness.analysis.requestCalls).toHaveLength(0)
  })

  it('cancel bypasses even the absent-gate-dep wiring but never the availability probe', async () => {
    // analysisEnabled dep absent (fail-closed for request/followup)…
    const noGate = await startHarness({ withAnalysisGate: false })
    const cancel = await postAction(noGate.base, CANCEL_ENVELOPE)
    expect(cancel.status).toBe(200)
    expect(noGate.analysis.cancelCalls).toEqual(['ana-1'])

    // …and with no analysis surface wired at all, cancel past the closed
    // gate still answers the honest 501 (bypass skips only the gate, not
    // the availability ladder).
    const noAnalysis = await startHarness({ withAnalysis: false })
    const unavailable = await postAction(noAnalysis.base, CANCEL_ENVELOPE)
    expect(unavailable.status).toBe(501)
    expect(JSON.parse(unavailable.body)).toEqual({ reason: 'analysis_unavailable' })
  })

  it('is independent of inject.enabled in both directions', async () => {
    const harness = await startHarness()

    // inject open, analysis closed: analysis.* still refused.
    harness.gates.inject = true
    const closed = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(closed.status).toBe(403)
    expect(JSON.parse(closed.body).reason).toBe('analysis_disabled')

    // analysis open, inject closed: analysis passes, inject.* still gated.
    harness.gates.inject = false
    harness.gates.analysis = true
    const open = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(open.status).toBe(200)
    const inject = await postAction(harness.base, {
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 's1' },
      mode: 'queue',
      message: 'x',
    })
    expect(inject.status).toBe(403)
    expect(JSON.parse(inject.body).reason).toBe('inject_disabled')
  })

  it('fails closed when the analysisEnabled dep is absent (older wiring)', async () => {
    const harness = await startHarness({ withAnalysisGate: false })

    const reply = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(reply.status).toBe(403)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'analysis_disabled' })
  })
})

// ---------------------------------------------------------------------------
// Honest degradation past the gate.
// ---------------------------------------------------------------------------

describe('analysis availability degradation', () => {
  it('answers 501 analysis_unavailable when no analysis surface is wired', async () => {
    const harness = await startHarness({ withAnalysis: false })
    harness.gates.analysis = true

    for (const envelope of [REQUEST_ENVELOPE, FOLLOWUP_ENVELOPE, CANCEL_ENVELOPE]) {
      const reply = await postAction(harness.base, envelope)
      expect(reply.status).toBe(501)
      expect(JSON.parse(reply.body)).toEqual({ reason: 'analysis_unavailable' })
    }
  })

  it('answers 501 analysis_unavailable when the agents service is not bound', async () => {
    const harness = await startHarness()
    harness.gates.analysis = true
    harness.analysis.available = false

    const reply = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(reply.status).toBe(501)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'analysis_unavailable' })
    expect(harness.analysis.buildCalls).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// Model pre-check (A-1): no resolvable analysis model → honest 403 before
// any session is created. Followup/cancel are not gated (an established
// session carries its model); an absent probe skips the pre-check.
// ---------------------------------------------------------------------------

describe('analysis model pre-check (A-1)', () => {
  it('pre-rejects analysis.request with 403 analysis_model_unconfigured', async () => {
    const harness = await startHarness({ modelConfigured: () => false })
    harness.gates.analysis = true

    const reply = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(reply.status).toBe(403)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'analysis_model_unconfigured' })
    // Nothing downstream ran: no input assembled, no session created.
    expect(harness.analysis.buildCalls).toHaveLength(0)
    expect(harness.analysis.requestCalls).toHaveLength(0)
  })

  it('does not gate followup or cancel (established sessions keep their model)', async () => {
    const harness = await startHarness({ modelConfigured: () => false })
    harness.gates.analysis = true

    const followup = await postAction(harness.base, FOLLOWUP_ENVELOPE)
    expect(followup.status).toBe(200)
    expect(harness.analysis.followupCalls).toHaveLength(1)

    const cancel = await postAction(harness.base, CANCEL_ENVELOPE)
    expect(cancel.status).toBe(200)
    expect(harness.analysis.cancelCalls).toEqual(['ana-1'])
  })

  it('passes requests through when the probe reports a model', async () => {
    const harness = await startHarness({ modelConfigured: () => true })
    harness.gates.analysis = true

    const reply = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(reply.status).toBe(200)
    expect(harness.analysis.requestCalls).toHaveLength(1)
  })

  it('ranks below the gate and the availability probe', async () => {
    // Gate closed wins over the model pre-check…
    const gated = await startHarness({ modelConfigured: () => false })
    const closed = await postAction(gated.base, REQUEST_ENVELOPE)
    expect(closed.status).toBe(403)
    expect(JSON.parse(closed.body).reason).toBe('analysis_disabled')

    // …and agents-less unavailability wins too (model config is moot).
    const unavailable = await startHarness({ modelConfigured: () => false })
    unavailable.gates.analysis = true
    unavailable.analysis.available = false
    const reply = await postAction(unavailable.base, REQUEST_ENVELOPE)
    expect(reply.status).toBe(501)
    expect(JSON.parse(reply.body).reason).toBe('analysis_unavailable')
  })
})

// ---------------------------------------------------------------------------
// analysis.request.
// ---------------------------------------------------------------------------

describe('POST action analysis.request', () => {
  it('builds the input from the adapter, drives the engine, and answers 200 with the result', async () => {
    const harness = await startHarness()
    harness.gates.analysis = true
    harness.analysis.results.request = completedResult({ tokensHint: 42 })

    const reply = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toEqual({
      outcome: 'completed',
      analysisSessionId: 'ana-1',
      summary: 'looks healthy',
      truncated: false,
      tokensHint: 42,
      disclaimer: ANALYSIS_DISCLAIMER,
    })
    expect(harness.analysis.buildCalls).toEqual([
      { targetKind: 'session', targetId: 's1', question: 'anything odd?' },
    ])
    expect(harness.analysis.requestCalls).toEqual([makeInput()])
  })

  it('passes cross-agent requests through without a targetId', async () => {
    const harness = await startHarness()
    harness.gates.analysis = true

    const reply = await postAction(harness.base, {
      type: 'analysis.request',
      targetKind: 'cross-agent',
    })
    expect(reply.status).toBe(200)
    expect(harness.analysis.buildCalls).toEqual([{ targetKind: 'cross-agent' }])
  })

  it('answers 404 target_not_found when the adapter resolves no input', async () => {
    const harness = await startHarness()
    harness.gates.analysis = true
    harness.analysis.results.build = null

    const reply = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(reply.status).toBe(404)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'target_not_found' })
    expect(harness.analysis.requestCalls).toHaveLength(0)
  })

  it.each([
    ['analysis_disabled', 403, 'failed'],
    ['too_many_active', 429, 'failed'],
    ['create_failed', 502, 'failed'],
    ['timeout', 504, 'timeout'],
  ] as const)('maps a %s result to %i', async (errorCode, status, outcome) => {
    const harness = await startHarness()
    harness.gates.analysis = true
    harness.analysis.results.request = {
      outcome,
      truncated: false,
      errorCode,
      disclaimer: ANALYSIS_DISCLAIMER,
    }

    const reply = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(reply.status).toBe(status)
    const body = JSON.parse(reply.body) as Record<string, unknown>
    expect(body.outcome).toBe(outcome)
    expect(body.errorCode).toBe(errorCode)
  })

  it('answers 502 for a result with an unknown error code', async () => {
    const harness = await startHarness()
    harness.gates.analysis = true
    harness.analysis.results.request = {
      outcome: 'failed',
      truncated: false,
      errorCode: 'not_in_the_vocabulary' as never,
      disclaimer: ANALYSIS_DISCLAIMER,
    }

    const reply = await postAction(harness.base, REQUEST_ENVELOPE)
    expect(reply.status).toBe(502)
  })

  it.each([
    ['unknown targetKind', { type: 'analysis.request', targetKind: 'universe' }],
    ['missing targetKind', { type: 'analysis.request', targetId: 's1' }],
    ['session without targetId', { type: 'analysis.request', targetKind: 'session' }],
    ['project with empty targetId', { type: 'analysis.request', targetKind: 'project', targetId: '' }],
    ['non-string targetId', { type: 'analysis.request', targetKind: 'session', targetId: 42 }],
    ['non-string question', { type: 'analysis.request', targetKind: 'cross-agent', question: 42 }],
  ])('answers 400 invalid_request for %s without touching the adapter', async (_name, envelope) => {
    const harness = await startHarness()
    harness.gates.analysis = true

    const reply = await postAction(harness.base, envelope)
    expect(reply.status).toBe(400)
    expect(JSON.parse(reply.body).reason).toBe('invalid_request')
    expect(harness.analysis.buildCalls).toHaveLength(0)
    expect(harness.analysis.requestCalls).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// analysis.followup.
// ---------------------------------------------------------------------------

describe('POST action analysis.followup', () => {
  it('forwards analysisSessionId/question and answers 200 for completed', async () => {
    const harness = await startHarness()
    harness.gates.analysis = true

    const reply = await postAction(harness.base, FOLLOWUP_ENVELOPE)
    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body).outcome).toBe('completed')
    expect(harness.analysis.followupCalls).toEqual([
      { analysisSessionId: 'ana-1', question: 'and next steps?' },
    ])
  })

  it('answers 200 for a cancelled result (terminal outcome, not a transport failure)', async () => {
    const harness = await startHarness()
    harness.gates.analysis = true
    harness.analysis.results.followup = {
      outcome: 'failed',
      truncated: false,
      errorCode: 'cancelled',
      detail: 'unknown or already-cancelled analysis session',
      disclaimer: ANALYSIS_DISCLAIMER,
    }

    const reply = await postAction(harness.base, FOLLOWUP_ENVELOPE)
    expect(reply.status).toBe(200)
    const body = JSON.parse(reply.body) as Record<string, unknown>
    expect(body.outcome).toBe('failed')
    expect(body.errorCode).toBe('cancelled')
  })

  it('maps a followup timeout to 504 while the session stays referenced', async () => {
    const harness = await startHarness()
    harness.gates.analysis = true
    harness.analysis.results.followup = {
      outcome: 'timeout',
      analysisSessionId: 'ana-1',
      truncated: false,
      errorCode: 'timeout',
      disclaimer: ANALYSIS_DISCLAIMER,
    }

    const reply = await postAction(harness.base, FOLLOWUP_ENVELOPE)
    expect(reply.status).toBe(504)
    expect(JSON.parse(reply.body).analysisSessionId).toBe('ana-1')
  })

  it.each([
    ['missing question', { type: 'analysis.followup', analysisSessionId: 'ana-1' }],
    ['empty question', { type: 'analysis.followup', analysisSessionId: 'ana-1', question: '' }],
    ['missing analysisSessionId', { type: 'analysis.followup', question: 'x' }],
    ['non-string analysisSessionId', { type: 'analysis.followup', analysisSessionId: 7, question: 'x' }],
  ])('answers 400 invalid_request for %s', async (_name, envelope) => {
    const harness = await startHarness()
    harness.gates.analysis = true

    const reply = await postAction(harness.base, envelope)
    expect(reply.status).toBe(400)
    expect(JSON.parse(reply.body).reason).toBe('invalid_request')
    expect(harness.analysis.followupCalls).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// analysis.cancel.
// ---------------------------------------------------------------------------

describe('POST action analysis.cancel', () => {
  it('cancels through the engine and answers 200 (idempotent contract)', async () => {
    const harness = await startHarness()
    harness.gates.analysis = true

    const reply = await postAction(harness.base, CANCEL_ENVELOPE)
    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toEqual({ ok: true, analysisSessionId: 'ana-1' })
    expect(harness.analysis.cancelCalls).toEqual(['ana-1'])

    // Second cancel of the same id: the engine treats it as a no-op; the
    // route stays 200 either way.
    const again = await postAction(harness.base, CANCEL_ENVELOPE)
    expect(again.status).toBe(200)
    expect(harness.analysis.cancelCalls).toEqual(['ana-1', 'ana-1'])
  })

  it('answers 400 invalid_request without an analysisSessionId', async () => {
    const harness = await startHarness()
    harness.gates.analysis = true

    const reply = await postAction(harness.base, { type: 'analysis.cancel' })
    expect(reply.status).toBe(400)
    expect(JSON.parse(reply.body).reason).toBe('invalid_request')
    expect(harness.analysis.cancelCalls).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// Log hygiene (S8): analyzed content never reaches the route log.
// ---------------------------------------------------------------------------

describe('analysis log hygiene', () => {
  it('never writes questions, summaries, or built input text into the route log', async () => {
    const MARKER = 'SECRET_ANALYSIS_MARKER_do_not_log_9d3b'
    const harness = await startHarness()
    harness.gates.analysis = true
    harness.analysis.results.build = makeInput({
      title: `title ${MARKER}`,
      summaryText: `summary ${MARKER}`,
    })
    harness.analysis.results.request = completedResult({ summary: `insight ${MARKER}` })
    harness.analysis.results.followup = completedResult({ summary: `more ${MARKER}` })

    const requested = await postAction(harness.base, {
      ...REQUEST_ENVELOPE,
      question: MARKER,
    })
    expect(requested.status).toBe(200)
    expect(requested.body).toContain(MARKER) // the response carries the summary…

    await postAction(harness.base, { ...FOLLOWUP_ENVELOPE, question: MARKER })
    await postAction(harness.base, CANCEL_ENVELOPE)

    expect(harness.logs.length).toBeGreaterThan(0)
    expect(JSON.stringify(harness.logs)).not.toContain(MARKER) // …the log never does
  })
})
