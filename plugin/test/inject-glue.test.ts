/**
 * Unit tests for the S5 inject integration glue (client/inject-glue.ts):
 * the M2 action wire bodies, the value-vs-throw error posture of the
 * onPrepare/onExecute callbacks (the panel contract: ApiError resolves as
 * a value, anything else propagates), the delivered-only refresh hook, and
 * the card-selection → panel-target mapping. Node environment, injected
 * transport fakes throughout.
 */

import { describe, expect, it } from 'vitest'
import { ApiError, type RequestOptions } from '../src/client/api.ts'
import type { SessionCardVM } from '../src/client/board/logic.ts'
import {
  createInjectActions,
  executeEnvelope,
  EXECUTE_TIMEOUT_MS,
  findInjectTarget,
  prepareEnvelope,
  type PostActionFn,
} from '../src/client/inject-glue.ts'
import { DEFAULT_SEND_TIMEOUT_MS, HARD_TIMEOUT_BUFFER_MS } from '../src/send-cli.ts'
import { isApiErrorLike } from '../src/client/inject/logic.ts'
import type {
  InjectResultView,
  PanelExecuteRequest,
  PanelPrepareRequest,
  PrepareSuccess,
} from '../src/client/inject/logic.ts'

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

const PREPARE_REQ: PanelPrepareRequest = {
  target: { agent: 'dsh', sessionId: 'sess-1' },
  mode: 'queue',
  message: 'hello',
}

const EXECUTE_REQ: PanelExecuteRequest = {
  requestId: 'req-1',
  confirmToken: 'tok-1',
  message: 'hello',
}

const PREPARE_OK: PrepareSuccess = {
  requestId: 'req-1',
  confirmToken: 'tok-1',
  plan: {
    target: { agent: 'dsh', sessionId: 'sess-1' },
    mode: 'queue',
    targetStatus: { agent: 'dsh', sessionId: 'sess-1', status: 'waiting' },
    messagePreview: { bytes: 5, head: 'hello' },
  },
  expiresAt: 60_000,
}

function makeCard(overrides: Partial<SessionCardVM> = {}): SessionCardVM {
  return {
    agent: 'claude',
    sessionId: 'sess-1',
    status: 'waiting',
    title: 'demo session',
    project: '/tmp/demo',
    updatedAtMs: 1_724_000_000_000,
    lastEvent: null,
    gap: false,
    ...overrides,
  }
}

/** Transport fake answering from a queue; records every posted body + opts. */
function capturePost(
  outcomes: Array<{ value: unknown } | { thrown: unknown }>,
): { post: PostActionFn; bodies: unknown[]; opts: Array<RequestOptions | undefined> } {
  const bodies: unknown[] = []
  const opts: Array<RequestOptions | undefined> = []
  const post: PostActionFn = (body, requestOpts) => {
    bodies.push(body)
    opts.push(requestOpts)
    const next = outcomes.shift()
    if (next === undefined) throw new Error('unexpected extra post call')
    if ('thrown' in next) return Promise.reject(next.thrown)
    return Promise.resolve(next.value)
  }
  return { post, bodies, opts }
}

// ---------------------------------------------------------------------------
// Wire bodies (must mirror the routes.ts action dispatcher contract).
// ---------------------------------------------------------------------------

describe('action envelopes', () => {
  it('prepareEnvelope builds the exact inject.prepare dispatcher body', () => {
    expect(prepareEnvelope(PREPARE_REQ)).toEqual({
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 'sess-1' },
      mode: 'queue',
      message: 'hello',
    })
  })

  it('executeEnvelope builds the exact inject.execute dispatcher body', () => {
    expect(executeEnvelope(EXECUTE_REQ)).toEqual({
      type: 'inject.execute',
      requestId: 'req-1',
      confirmToken: 'tok-1',
      message: 'hello',
    })
  })
})

// ---------------------------------------------------------------------------
// onPrepare.
// ---------------------------------------------------------------------------

describe('createInjectActions onPrepare', () => {
  it('posts the prepare envelope and resolves the success body verbatim', async () => {
    const { post, bodies } = capturePost([{ value: PREPARE_OK }])
    const actions = createInjectActions({ post })
    await expect(actions.onPrepare(PREPARE_REQ)).resolves.toBe(PREPARE_OK)
    expect(bodies).toEqual([prepareEnvelope(PREPARE_REQ)])
  })

  it('resolves an ApiError as a value the panel can classify structurally', async () => {
    const err = new ApiError('http', 'target_not_found', 404)
    const { post } = capturePost([{ thrown: err }])
    const actions = createInjectActions({ post })
    const resolved = await actions.onPrepare(PREPARE_REQ)
    expect(resolved).toBe(err)
    // Cross-module structural handshake: the real ApiError instance passes
    // the panel logic's import-free guard.
    expect(isApiErrorLike(resolved)).toBe(true)
  })

  it('propagates a non-ApiError throw (the panel defensive catch owns it)', async () => {
    const { post } = capturePost([{ thrown: new TypeError('boom') }])
    const actions = createInjectActions({ post })
    await expect(actions.onPrepare(PREPARE_REQ)).rejects.toThrow('boom')
  })
})

// ---------------------------------------------------------------------------
// onExecute.
// ---------------------------------------------------------------------------

describe('createInjectActions onExecute', () => {
  it('posts the execute envelope and fires onDelivered exactly for delivered', async () => {
    let refreshed = 0
    const delivered: InjectResultView = { outcome: 'delivered' }
    const { post, bodies } = capturePost([{ value: delivered }])
    const actions = createInjectActions({ post, onDelivered: () => { refreshed += 1 } })
    await expect(actions.onExecute(EXECUTE_REQ)).resolves.toBe(delivered)
    expect(bodies).toEqual([executeEnvelope(EXECUTE_REQ)])
    expect(refreshed).toBe(1)
  })

  it('never fires onDelivered for an unknown outcome (S6 terminal posture)', async () => {
    let refreshed = 0
    const unknown: InjectResultView = { outcome: 'unknown', errorCode: 'send_timeout' }
    const { post } = capturePost([{ value: unknown }])
    const actions = createInjectActions({ post, onDelivered: () => { refreshed += 1 } })
    await expect(actions.onExecute(EXECUTE_REQ)).resolves.toBe(unknown)
    expect(refreshed).toBe(0)
  })

  it('resolves an ApiError as a value without firing onDelivered', async () => {
    let refreshed = 0
    const err = new ApiError('http', 'token_expired', 401)
    const { post } = capturePost([{ thrown: err }])
    const actions = createInjectActions({ post, onDelivered: () => { refreshed += 1 } })
    const resolved = await actions.onExecute(EXECUTE_REQ)
    expect(resolved).toBe(err)
    expect(isApiErrorLike(resolved)).toBe(true)
    expect(refreshed).toBe(0)
  })

  it('propagates a non-ApiError throw (panel maps it to terminal unknown)', async () => {
    const { post } = capturePost([{ thrown: new TypeError('boom') }])
    const actions = createInjectActions({ post })
    await expect(actions.onExecute(EXECUTE_REQ)).rejects.toThrow('boom')
  })
})

// ---------------------------------------------------------------------------
// Execute timeout budget (M2 review F-3).
// ---------------------------------------------------------------------------

describe('execute timeout budget (M2 review F-3)', () => {
  it('outlives the send-cli server budget with margin (pinned to host constants)', () => {
    // Path two's server worst case is the CLI timeout plus the hard-kill
    // buffer (35s); the client deadline must exceed it by a clear margin or
    // every slow-but-honest receipt becomes a fabricated terminal unknown.
    expect(EXECUTE_TIMEOUT_MS).toBeGreaterThanOrEqual(
      DEFAULT_SEND_TIMEOUT_MS + HARD_TIMEOUT_BUFFER_MS + 5_000,
    )
  })

  it('onExecute posts with the execute deadline; onPrepare keeps the data-layer default', async () => {
    const delivered: InjectResultView = { outcome: 'delivered' }
    const { post, opts } = capturePost([{ value: PREPARE_OK }, { value: delivered }])
    const actions = createInjectActions({ post })
    await actions.onPrepare(PREPARE_REQ)
    await actions.onExecute(EXECUTE_REQ)
    expect(opts).toEqual([undefined, { timeoutMs: EXECUTE_TIMEOUT_MS }])
  })
})

// ---------------------------------------------------------------------------
// Target mapping.
// ---------------------------------------------------------------------------

describe('findInjectTarget', () => {
  it('maps the selected card onto agent/sessionId/title', () => {
    const cards = [makeCard(), makeCard({ sessionId: 'sess-2', agent: 'dsh', title: 'other' })]
    expect(findInjectTarget(cards, 'sess-2')).toEqual({
      agent: 'dsh',
      sessionId: 'sess-2',
      title: 'other',
    })
  })

  it('omits the title for blank/whitespace card titles', () => {
    expect(findInjectTarget([makeCard({ title: '   ' })], 'sess-1')).toEqual({
      agent: 'claude',
      sessionId: 'sess-1',
    })
  })

  it('resolves null when unselected or when the session left the snapshot', () => {
    expect(findInjectTarget([makeCard()], null)).toBeNull()
    expect(findInjectTarget([makeCard()], 'sess-gone')).toBeNull()
    expect(findInjectTarget([], 'sess-1')).toBeNull()
  })
})
