/**
 * Unit tests for the AI-analysis glue store (client/analysis-glue.ts):
 * envelope builders, the conversation state machine over the wire
 * contract (200 results, error-code statuses via ApiError, the cancelled
 * terminal), and dispose semantics. Node environment with an injected
 * postAction; no real network, no token consumption.
 */

import { describe, expect, it } from 'vitest'
import { ApiError, type ActionEnvelope } from '../src/client/api.ts'
import {
  ANALYSIS_POST_TIMEOUT_MS,
  AnalysisStore,
  analysisCancelEnvelope,
  analysisFollowupEnvelope,
  analysisRequestEnvelope,
  type AnalysisResultWire,
} from '../src/client/analysis-glue.ts'

function completed(overrides: Partial<AnalysisResultWire> = {}): AnalysisResultWire {
  return {
    outcome: 'completed',
    analysisSessionId: 'ana-1',
    summary: 'looks healthy',
    truncated: false,
    tokensHint: 128,
    disclaimer: 'AI 分析仅供参考',
    ...overrides,
  }
}

interface Posted {
  body: ActionEnvelope
  timeoutMs: number | undefined
}

function makeStore(script: Array<unknown | Error>): { store: AnalysisStore; posted: Posted[] } {
  const posted: Posted[] = []
  const store = new AnalysisStore({
    postActionFn: (body, opts) => {
      posted.push({ body, timeoutMs: opts?.timeoutMs })
      const next = script.shift()
      if (next === undefined) return Promise.reject(new Error('unexpected extra post'))
      return next instanceof Error ? Promise.reject(next) : Promise.resolve(next)
    },
  })
  return { store, posted }
}

// ---------------------------------------------------------------------------
// Envelope builders.
// ---------------------------------------------------------------------------

describe('envelope builders', () => {
  it('build the wire contracts verbatim and drop blank questions', () => {
    expect(analysisRequestEnvelope({ targetKind: 'session', targetId: 's1', question: ' why ' }))
      .toEqual({ type: 'analysis.request', targetKind: 'session', targetId: 's1', question: ' why ' })
    expect(analysisRequestEnvelope({ targetKind: 'cross-agent', question: '  ' }))
      .toEqual({ type: 'analysis.request', targetKind: 'cross-agent' })
    expect(analysisFollowupEnvelope('ana-1', 'and then?'))
      .toEqual({ type: 'analysis.followup', analysisSessionId: 'ana-1', question: 'and then?' })
    expect(analysisCancelEnvelope('ana-1'))
      .toEqual({ type: 'analysis.cancel', analysisSessionId: 'ana-1' })
  })
})

// ---------------------------------------------------------------------------
// start().
// ---------------------------------------------------------------------------

describe('AnalysisStore.start', () => {
  it('folds a completed result into phase ready with the disclaimer', async () => {
    const { store, posted } = makeStore([completed()])
    await store.start({ targetKind: 'session', targetId: 's1' })
    expect(posted[0]?.body).toEqual({
      type: 'analysis.request', targetKind: 'session', targetId: 's1',
    })
    // Transport deadline outlives the engine's ~60s turn bound.
    expect(posted[0]?.timeoutMs).toBe(ANALYSIS_POST_TIMEOUT_MS)
    const state = store.getState()
    expect(state.phase).toBe('ready')
    expect(state.analysisSessionId).toBe('ana-1')
    expect(state.exchanges).toEqual([
      { question: null, summary: 'looks healthy', truncated: false, tokensHint: 128 },
    ])
    expect(state.messages).toEqual([
      { role: 'assistant', content: 'looks healthy' },
    ])
    expect(state.disclaimer).toBe('AI 分析仅供参考')
  })

  it.each([
    ['analysis_disabled', new ApiError('http', 'analysis_disabled', 403), 'analysis_disabled'],
    ['analysis_unavailable', new ApiError('http', 'analysis_unavailable', 501), 'analysis_unavailable'],
    ['pre-analysis host unknown_action', new ApiError('http', 'unknown_action', 400), 'analysis_unavailable'],
    ['target_not_found', new ApiError('http', 'target_not_found', 404), 'target_not_found'],
    ['engine timeout (504, session disposed)', new ApiError('http', 'timeout', 504), 'timeout'],
    ['too_many_active', new ApiError('http', 'too_many_active', 429), 'too_many_active'],
    ['transport network', new ApiError('network', 'network_error'), 'network_error'],
  ])('%s is a terminal failure', async (_label, err, code) => {
    const { store } = makeStore([err])
    await store.start({ targetKind: 'session', targetId: 's1' })
    const state = store.getState()
    expect(state.phase).toBe('failed')
    expect(state.errorCode).toBe(code)
  })

  it('restarts a fresh conversation from the failed terminal', async () => {
    const { store, posted } = makeStore([
      new ApiError('http', 'timeout', 504),
      completed({ analysisSessionId: 'ana-2' }),
    ])
    await store.start({ targetKind: 'session', targetId: 's1' })
    await store.start({ targetKind: 'session', targetId: 's1' })
    expect(posted).toHaveLength(2)
    const state = store.getState()
    expect(state.phase).toBe('ready')
    expect(state.analysisSessionId).toBe('ana-2')
    expect(state.errorCode).toBeNull()
  })

  it('guards against double starts while live', async () => {
    const { store, posted } = makeStore([completed()])
    await store.start({ targetKind: 'session', targetId: 's1' })
    await store.start({ targetKind: 'session', targetId: 's1' }) // phase ready → no-op
    expect(posted).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
// followup().
// ---------------------------------------------------------------------------

describe('AnalysisStore.followup', () => {
  async function readyStore(script: Array<unknown | Error>): Promise<{
    store: AnalysisStore
    posted: Posted[]
  }> {
    const { store, posted } = makeStore([completed(), ...script])
    await store.start({ targetKind: 'session', targetId: 's1' })
    return { store, posted }
  }

  it('appends the exchange with the question and truncation flag', async () => {
    const { store, posted } = await readyStore([
      completed({ summary: 'deeper', truncated: true, tokensHint: 42 }),
    ])
    await store.followup(' and then? ')
    expect(posted[1]?.body).toEqual({
      type: 'analysis.followup', analysisSessionId: 'ana-1', question: 'and then?',
    })
    const state = store.getState()
    expect(state.phase).toBe('ready')
    expect(state.exchanges).toHaveLength(2)
    expect(state.exchanges[1]).toEqual({
      question: 'and then?', summary: 'deeper', truncated: true, tokensHint: 42,
    })
    expect(state.messages).toEqual([
      { role: 'assistant', content: 'looks healthy' },
      { role: 'user', content: 'and then?' },
      { role: 'assistant', content: 'deeper', truncated: true },
    ])
  })

  it('keeps the session usable on an engine turn timeout (notice, not terminal)', async () => {
    const { store } = await readyStore([new ApiError('http', 'timeout', 504)])
    await store.followup('slow one')
    const state = store.getState()
    expect(state.phase).toBe('ready')
    expect(state.noticeCode).toBe('timeout')
    expect(state.errorCode).toBeNull()
  })

  it('treats a 200 cancelled result as the stopped terminal', async () => {
    const { store } = await readyStore([
      {
        outcome: 'failed', errorCode: 'cancelled', truncated: false,
        disclaimer: 'AI 分析仅供参考', analysisSessionId: 'ana-1',
      },
    ])
    await store.followup('too late')
    expect(store.getState().phase).toBe('stopped')
  })

  it('fails terminally on non-retryable followup errors', async () => {
    const { store } = await readyStore([new ApiError('http', 'analysis_disabled', 403)])
    await store.followup('q')
    expect(store.getState().phase).toBe('failed')
    expect(store.getState().errorCode).toBe('analysis_disabled')
  })

  it('no-ops on blank questions and outside phase ready', async () => {
    const { store, posted } = await readyStore([])
    await store.followup('   ')
    expect(posted).toHaveLength(1) // only the start call
  })
})

// ---------------------------------------------------------------------------
// stop() + dispose().
// ---------------------------------------------------------------------------

describe('AnalysisStore.stop', () => {
  it('posts the cancel envelope and lands in phase stopped', async () => {
    const { store, posted } = makeStore([completed(), { ok: true, analysisSessionId: 'ana-1' }])
    await store.start({ targetKind: 'session', targetId: 's1' })
    await store.stop()
    expect(posted[1]?.body).toEqual({ type: 'analysis.cancel', analysisSessionId: 'ana-1' })
    expect(store.getState().phase).toBe('stopped')
  })

  it('drops the in-flight followup settle after a mid-flight stop', async () => {
    let releaseFollowup: ((r: AnalysisResultWire) => void) | null = null
    const posted: Posted[] = []
    const store = new AnalysisStore({
      postActionFn: (body, opts) => {
        posted.push({ body, timeoutMs: opts?.timeoutMs })
        if (body.type === 'analysis.request') return Promise.resolve(completed())
        if (body.type === 'analysis.followup') {
          return new Promise((resolve) => {
            releaseFollowup = resolve as (r: AnalysisResultWire) => void
          })
        }
        return Promise.resolve({ ok: true, analysisSessionId: 'ana-1' })
      },
    })
    await store.start({ targetKind: 'session', targetId: 's1' })
    const asking = store.followup('hanging')
    await store.stop() // cancel settles while the followup hangs
    expect(store.getState().phase).toBe('stopped')
    releaseFollowup?.(completed({ summary: 'late' }))
    await asking
    // The late settle was dropped: no new exchange, phase stays stopped.
    expect(store.getState().phase).toBe('stopped')
    expect(store.getState().exchanges).toHaveLength(1)
  })

  it('a failed cancel keeps the session with a retry notice', async () => {
    const { store } = makeStore([completed(), new ApiError('network', 'network_error')])
    await store.start({ targetKind: 'session', targetId: 's1' })
    await store.stop()
    const state = store.getState()
    expect(state.phase).toBe('ready')
    expect(state.noticeCode).toBe('cancel_failed')
  })
})

describe('AnalysisStore.dispose', () => {
  it('drops late settlements silently', async () => {
    let release: ((r: AnalysisResultWire) => void) | null = null
    const store = new AnalysisStore({
      postActionFn: () =>
        new Promise((resolve) => { release = resolve as (r: AnalysisResultWire) => void }),
    })
    const starting = store.start({ targetKind: 'session', targetId: 's1' })
    store.dispose()
    release?.(completed())
    await starting
    expect(store.getState().exchanges).toHaveLength(0)
  })

  // F2: unmount/remount must not strand engine session slots — dispose
  // releases the live session with a fire-and-forget cancel.

  it('releases the live engine session with a fire-and-forget cancel', async () => {
    const { store, posted } = makeStore([completed(), { ok: true, analysisSessionId: 'ana-1' }])
    await store.start({ targetKind: 'session', targetId: 's1' })
    store.dispose()
    expect(posted).toHaveLength(2)
    expect(posted[1]?.body).toEqual({ type: 'analysis.cancel', analysisSessionId: 'ana-1' })
  })

  it('cancels the session created by a request that settles after dispose', async () => {
    let release: ((r: AnalysisResultWire) => void) | null = null
    const posted: Posted[] = []
    const store = new AnalysisStore({
      postActionFn: (body, opts) => {
        posted.push({ body, timeoutMs: opts?.timeoutMs })
        if (body.type === 'analysis.request') {
          return new Promise((resolve) => {
            release = resolve as (r: AnalysisResultWire) => void
          })
        }
        return Promise.resolve({ ok: true })
      },
    })
    const starting = store.start({ targetKind: 'session', targetId: 's1' })
    store.dispose() // the view unmounts while the request is in flight…
    release?.(completed({ analysisSessionId: 'ana-9' }))
    await starting
    // …and the orphaned engine session is still released.
    expect(posted).toHaveLength(2)
    expect(posted[1]?.body).toEqual({ type: 'analysis.cancel', analysisSessionId: 'ana-9' })
  })

  it('sends no cancel without a live session (idle, stopped, failed)', async () => {
    const idle = makeStore([])
    idle.store.dispose()
    expect(idle.posted).toHaveLength(0)

    const stopped = makeStore([completed(), { ok: true, analysisSessionId: 'ana-1' }])
    await stopped.store.start({ targetKind: 'session', targetId: 's1' })
    await stopped.store.stop() // user already cancelled: nothing left to release
    stopped.store.dispose()
    expect(stopped.posted).toHaveLength(2)

    const failed = makeStore([new ApiError('http', 'timeout', 504)])
    await failed.store.start({ targetKind: 'session', targetId: 's1' })
    failed.store.dispose() // engine already disposed the timed-out session
    expect(failed.posted).toHaveLength(1)
  })

  it('swallows a failing dispose cancel silently (idempotent, nothing to surface)', async () => {
    const { store, posted } = makeStore([completed(), new ApiError('network', 'network_error')])
    await store.start({ targetKind: 'session', targetId: 's1' })
    expect(() => { store.dispose() }).not.toThrow()
    expect(posted).toHaveLength(2)
    await Promise.resolve() // let the rejected fire-and-forget settle
  })
})
