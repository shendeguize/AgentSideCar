/**
 * Unit tests for the AI bypass-analysis engine (design §5 pillar 3, T5.6).
 * A fake `ctx.agents.create` face records created sessions and every prompt
 * handed to followup; a scripted responder plays the model side, so the tests
 * can assert the gate, bounded-input truncation, timeout-cancel semantics,
 * incremental follow-up, UI cancel/dispose, the concurrency cap, the error
 * vocabulary, and the body-free logging contract. No real tokens are spent.
 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ANALYSIS_DISCLAIMER,
  ANALYSIS_GUIDANCE,
  AnalysisEngine,
  DEFAULT_ANALYSIS_TIMEOUT_MS,
  DEFAULT_MAX_ACTIVE_ANALYSES,
  DEFAULT_MAX_INPUT_CHARS,
  TRUNCATION_MARKER,
  type AnalysisAgentFace,
  type AnalysisCancelCauseFace,
  type AnalysisInput,
  type AnalysisLogEntry,
  type AnalysisSession,
  type AnalysisUserMessageFace,
} from '../src/analysis.ts'

// ---------------------------------------------------------------------------
// Fakes.
// ---------------------------------------------------------------------------

/** Scripted model: returns the reply text, or null to never answer (timeout). */
type Responder = (promptText: string) => string | null

class FakeDriver {
  readonly received: AnalysisUserMessageFace[] = []
  readonly cancelCauses: AnalysisCancelCauseFace[] = []
  respond: Responder
  followupThrows?: Error
  /** Token usage attached to each scripted reply's assistant/message event. */
  usage: { inputTokens: number; outputTokens: number } | undefined = {
    inputTokens: 100,
    outputTokens: 20,
  }

  private readonly derived: Array<{
    role: string
    content: Array<{ type: string; text?: string }>
  }> = []
  private readonly eventLog: Array<{ type: string; data?: unknown }> = []
  private turnPending = false

  readonly session: {
    deriveMessages(): Array<{ role: string; content: Array<{ type: string; text?: string }> }>
    readonly events: Array<{ type: string; data?: unknown }>
  }

  constructor(respond: Responder) {
    this.respond = respond
    const driver = this
    this.session = {
      deriveMessages: () => [...driver.derived],
      get events() {
        return [...driver.eventLog]
      },
    }
  }

  followup(message: AnalysisUserMessageFace): void {
    if (this.followupThrows) throw this.followupThrows
    this.received.push(message)
    const text = message.content
      .map((block) => (typeof block.text === 'string' ? block.text : ''))
      .join('')
    this.derived.push({ role: 'user', content: [{ type: 'text', text }] })
    this.eventLog.push({ type: 'user/message', data: {} })
    const reply = this.respond(text)
    if (reply === null) {
      this.turnPending = true
      return
    }
    this.derived.push({ role: 'assistant', content: [{ type: 'text', text: reply }] })
    this.eventLog.push({
      type: 'assistant/message',
      data: this.usage !== undefined ? { usage: this.usage } : {},
    })
  }

  cancel(cause: AnalysisCancelCauseFace): void {
    this.cancelCauses.push(cause)
    this.turnPending = false
  }

  whenIdle(): Promise<void> {
    // A pending (never-answered) turn never reaches quiescence.
    return this.turnPending ? new Promise<void>(() => {}) : Promise.resolve()
  }
}

interface FakeCreated {
  sessionId: string
  signal: AbortSignal | undefined
  driver: FakeDriver
  disposeCalls: number
}

function fakeWorld(opts?: {
  respond?: Responder
  createRejects?: Error
  createNever?: boolean
}): {
  create: AnalysisAgentFace['create']
  created: FakeCreated[]
} {
  const created: FakeCreated[] = []
  const respond: Responder = opts?.respond ?? (() => 'default insight')
  const create: AnalysisAgentFace['create'] = (options) => {
    if (opts?.createRejects) return Promise.reject(opts.createRejects)
    if (opts?.createNever) {
      created.push({
        sessionId: options.sessionId,
        signal: options.signal,
        driver: new FakeDriver(respond),
        disposeCalls: 0,
      })
      return new Promise<AnalysisSession>(() => {})
    }
    const driver = new FakeDriver(respond)
    const record: FakeCreated = {
      sessionId: options.sessionId,
      signal: options.signal,
      driver,
      disposeCalls: 0,
    }
    created.push(record)
    const handle: AnalysisSession = {
      agent: driver,
      dispose: async () => {
        record.disposeCalls += 1
      },
    }
    return Promise.resolve(handle)
  }
  return { create, created }
}

function makeEngine(opts?: {
  world?: ReturnType<typeof fakeWorld>
  allow?: () => boolean
  maxInputChars?: number
  analysisTimeoutMs?: number
  maxActiveSessions?: number
}): {
  engine: AnalysisEngine
  world: ReturnType<typeof fakeWorld>
  logEntries: AnalysisLogEntry[]
} {
  const world = opts?.world ?? fakeWorld()
  const logEntries: AnalysisLogEntry[] = []
  const engine = new AnalysisEngine({
    createAgent: world.create,
    allowAnalysis: opts?.allow ?? (() => true),
    log: (entry) => logEntries.push(entry),
    ...(opts?.maxInputChars !== undefined ? { maxInputChars: opts.maxInputChars } : {}),
    ...(opts?.analysisTimeoutMs !== undefined
      ? { analysisTimeoutMs: opts.analysisTimeoutMs }
      : {}),
    ...(opts?.maxActiveSessions !== undefined
      ? { maxActiveSessions: opts.maxActiveSessions }
      : {}),
  })
  return { engine, world, logEntries }
}

function input(overrides?: Partial<AnalysisInput>): AnalysisInput {
  return {
    kind: 'session',
    title: 'claude worker on repo X',
    summaryText: 'the observed session ran 3 tool calls and is now waiting',
    ...overrides,
  }
}

afterEach(() => {
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// Gate.
// ---------------------------------------------------------------------------

describe('analysis.enabled gate', () => {
  it('rejects request with analysis_disabled and never calls create', async () => {
    const { engine, world, logEntries } = makeEngine({ allow: () => false })

    const result = await engine.request(input())

    expect(result.outcome).toBe('failed')
    expect(result.errorCode).toBe('analysis_disabled')
    expect(result.disclaimer).toBe(ANALYSIS_DISCLAIMER)
    expect(world.created).toHaveLength(0)
    expect(engine.activeCount).toBe(0)
    expect(logEntries.some((e) => e.op === 'result' && e.errorCode === 'analysis_disabled')).toBe(
      true,
    )
  })

  it('gates followup live: a flip to disabled rejects later followups', async () => {
    let allowed = true
    const { engine } = makeEngine({ allow: () => allowed })
    const first = await engine.request(input())
    expect(first.outcome).toBe('completed')

    allowed = false
    const result = await engine.followup(first.analysisSessionId!, 'and now?')

    expect(result.outcome).toBe('failed')
    expect(result.errorCode).toBe('analysis_disabled')
  })
})

// ---------------------------------------------------------------------------
// Normal request.
// ---------------------------------------------------------------------------

describe('request', () => {
  it('creates a dedicated session, feeds guidance + bounded input, returns the reply', async () => {
    const world = fakeWorld({ respond: () => 'insight: everything nominal' })
    const { engine, logEntries } = makeEngine({ world })

    const result = await engine.request(input())

    expect(world.created).toHaveLength(1)
    expect(result.outcome).toBe('completed')
    expect(result.analysisSessionId).toBe(world.created[0]!.sessionId)
    expect(result.summary).toBe('insight: everything nominal')
    expect(result.truncated).toBe(false)
    expect(result.tokensHint).toBe(120)
    expect(result.disclaimer).toBe(ANALYSIS_DISCLAIMER)
    expect(engine.activeCount).toBe(1)

    const driver = world.created[0]!.driver
    expect(driver.received).toHaveLength(1)
    const prompt = driver.received[0]!
    expect(prompt.role).toBe('user')
    expect(prompt.source).toEqual({ kind: 'plugin', plugin: 'agent-sidecar' })
    expect(prompt.content).toHaveLength(1)
    const promptText = prompt.content[0]!.text!
    expect(promptText).toContain(ANALYSIS_GUIDANCE)
    expect(promptText).toContain('kind=session')
    expect(promptText).toContain('claude worker on repo X')
    expect(promptText).toContain('the observed session ran 3 tool calls and is now waiting')
    expect(promptText).not.toContain(TRUNCATION_MARKER)

    expect(logEntries.some((e) => e.op === 'create')).toBe(true)
    expect(
      logEntries.some((e) => e.op === 'result' && e.outcome === 'completed' && e.tokensHint === 120),
    ).toBe(true)
  })

  it('omits tokensHint when the adapter reported no usage', async () => {
    const world = fakeWorld({ respond: () => 'no accounting here' })
    const { engine } = makeEngine({ world })
    // Strip usage before the turn runs: create is recorded synchronously in
    // the fake, so mutate right after the engine call resolves the driver.
    const pending = engine.request(input())
    // The fake create resolves on a microtask; usage is read per-reply, so
    // set it on the driver as soon as it exists.
    await Promise.resolve()
    world.created[0]!.driver.usage = undefined
    const result = await pending

    expect(result.outcome).toBe('completed')
    expect(result.tokensHint).toBeUndefined()
  })

  it('maps a create rejection to create_failed and frees the slot', async () => {
    const world = fakeWorld({ createRejects: new Error('factory offline') })
    const { engine, logEntries } = makeEngine({ world, maxActiveSessions: 1 })

    const result = await engine.request(input())

    expect(result.outcome).toBe('failed')
    expect(result.errorCode).toBe('create_failed')
    expect(result.detail).toBe('factory offline')
    expect(result.analysisSessionId).toBeUndefined()
    expect(engine.activeCount).toBe(0)
    expect(logEntries.some((e) => e.op === 'result' && e.errorCode === 'create_failed')).toBe(true)

    // The reserved slot was released: a retry can proceed at cap 1.
    const retryWorld = fakeWorld()
    const retry = makeEngine({ world: retryWorld, maxActiveSessions: 1 })
    expect((await retry.engine.request(input())).outcome).toBe('completed')
  })

  it('folds a followup throw during establishment into create_failed and disposes', async () => {
    const world = fakeWorld()
    const { engine } = makeEngine({ world })
    const pending = engine.request(input())
    await Promise.resolve()
    world.created[0]!.driver.followupThrows = new Error('agent gone')
    const result = await pending

    expect(result.outcome).toBe('failed')
    expect(result.errorCode).toBe('create_failed')
    expect(world.created[0]!.disposeCalls).toBe(1)
    expect(engine.activeCount).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// Bounded input.
// ---------------------------------------------------------------------------

describe('input bounding', () => {
  it('truncates over-limit summaryText, marks it, and flags the result', async () => {
    const world = fakeWorld()
    const { engine, logEntries } = makeEngine({ world, maxInputChars: 100 })
    const huge = 'A'.repeat(5000)

    const result = await engine.request(input({ summaryText: huge }))

    expect(result.truncated).toBe(true)
    const promptText = world.created[0]!.driver.received[0]!.content[0]!.text!
    expect(promptText).toContain('A'.repeat(100) + TRUNCATION_MARKER)
    expect(promptText).not.toContain('A'.repeat(101))
    expect(promptText).toContain('已截断 / truncated')
    expect(
      logEntries.some((e) => e.op === 'create' && e.truncated === true && e.inputChars !== undefined),
    ).toBe(true)
  })

  it('bounds followup questions with the same limit', async () => {
    const world = fakeWorld()
    const { engine } = makeEngine({ world, maxInputChars: 50 })
    const first = await engine.request(input())

    const result = await engine.followup(first.analysisSessionId!, 'Q'.repeat(500))

    expect(result.truncated).toBe(true)
    const driver = world.created[0]!.driver
    const questionText = driver.received[1]!.content[0]!.text!
    expect(questionText).toBe('Q'.repeat(50) + TRUNCATION_MARKER)
  })

  it('exports the specified defaults', () => {
    expect(DEFAULT_MAX_INPUT_CHARS).toBe(8000)
    expect(DEFAULT_ANALYSIS_TIMEOUT_MS).toBe(60_000)
    expect(DEFAULT_MAX_ACTIVE_ANALYSES).toBe(4)
  })
})

// ---------------------------------------------------------------------------
// Timeout.
// ---------------------------------------------------------------------------

describe('timeout', () => {
  it('request timeout cancels the turn, disposes the session, returns timeout', async () => {
    vi.useFakeTimers()
    const world = fakeWorld({ respond: () => null })
    const { engine, logEntries } = makeEngine({ world })

    const pending = engine.request(input())
    await vi.advanceTimersByTimeAsync(DEFAULT_ANALYSIS_TIMEOUT_MS)
    const result = await pending

    expect(result.outcome).toBe('timeout')
    expect(result.errorCode).toBe('timeout')
    expect(result.analysisSessionId).toBeUndefined()
    expect(result.summary).toBeUndefined()
    const record = world.created[0]!
    expect(record.driver.cancelCauses).toEqual([{ kind: 'user' }])
    expect(record.disposeCalls).toBe(1)
    expect(engine.activeCount).toBe(0)
    expect(logEntries.some((e) => e.op === 'result' && e.outcome === 'timeout')).toBe(true)
  })

  it('a hanging create times out, aborts the creation signal, frees the slot', async () => {
    vi.useFakeTimers()
    const world = fakeWorld({ createNever: true })
    const { engine } = makeEngine({ world, analysisTimeoutMs: 5000 })

    const pending = engine.request(input())
    await vi.advanceTimersByTimeAsync(5000)
    const result = await pending

    expect(result.outcome).toBe('timeout')
    expect(world.created[0]!.signal?.aborted).toBe(true)
    expect(engine.activeCount).toBe(0)
  })

  it('followup timeout cancels the turn but keeps the session alive', async () => {
    vi.useFakeTimers()
    let hang = false
    const world = fakeWorld({ respond: () => (hang ? null : 'first insight') })
    const { engine } = makeEngine({ world })
    const first = await engine.request(input())
    expect(first.outcome).toBe('completed')

    hang = true
    const pending = engine.followup(first.analysisSessionId!, 'and then?')
    await vi.advanceTimersByTimeAsync(DEFAULT_ANALYSIS_TIMEOUT_MS)
    const result = await pending

    expect(result.outcome).toBe('timeout')
    expect(result.analysisSessionId).toBe(first.analysisSessionId)
    const record = world.created[0]!
    expect(record.driver.cancelCauses).toEqual([{ kind: 'user' }])
    expect(record.disposeCalls).toBe(0)
    expect(engine.activeCount).toBe(1)

    // The kept session accepts a later follow-up.
    hang = false
    const retry = await engine.followup(first.analysisSessionId!, 'retry?')
    expect(retry.outcome).toBe('completed')
  })
})

// ---------------------------------------------------------------------------
// Incremental followup.
// ---------------------------------------------------------------------------

describe('followup', () => {
  it('asks on the same session and returns only the new turn text and tokens', async () => {
    const replies = ['first insight', 'second, deeper insight']
    let turn = 0
    const world = fakeWorld({ respond: () => replies[turn++] ?? null })
    const { engine } = makeEngine({ world })

    const first = await engine.request(input())
    const second = await engine.followup(first.analysisSessionId!, 'dig deeper')

    expect(world.created).toHaveLength(1)
    expect(second.outcome).toBe('completed')
    expect(second.analysisSessionId).toBe(first.analysisSessionId)
    expect(second.summary).toBe('second, deeper insight')
    expect(second.summary).not.toContain('first insight')
    expect(second.tokensHint).toBe(120)

    const driver = world.created[0]!.driver
    expect(driver.received).toHaveLength(2)
    expect(driver.received[1]!.content[0]!.text).toBe('dig deeper')
  })

  it('rejects a followup on an unknown session with cancelled', async () => {
    const { engine, world } = makeEngine({})

    const result = await engine.followup('no-such-analysis', 'hello?')

    expect(result.outcome).toBe('failed')
    expect(result.errorCode).toBe('cancelled')
    expect(world.created).toHaveLength(0)
  })

  it('maps a mid-followup agent death to cancelled and unregisters', async () => {
    const world = fakeWorld()
    const { engine } = makeEngine({ world })
    const first = await engine.request(input())

    world.created[0]!.driver.followupThrows = new Error('disposed underneath')
    const result = await engine.followup(first.analysisSessionId!, 'still there?')

    expect(result.outcome).toBe('failed')
    expect(result.errorCode).toBe('cancelled')
    expect(engine.activeCount).toBe(0)
    expect(world.created[0]!.disposeCalls).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// Cancel (UI stop).
// ---------------------------------------------------------------------------

describe('cancel', () => {
  it('disposes the analysis session and later followups report cancelled', async () => {
    const world = fakeWorld()
    const { engine, logEntries } = makeEngine({ world })
    const first = await engine.request(input())

    await engine.cancel(first.analysisSessionId!)

    const record = world.created[0]!
    expect(record.disposeCalls).toBe(1)
    expect(record.driver.cancelCauses).toEqual([{ kind: 'user' }])
    expect(engine.activeCount).toBe(0)
    expect(
      logEntries.some((e) => e.op === 'cancel' && e.found === true && e.analysisSessionId),
    ).toBe(true)

    const after = await engine.followup(first.analysisSessionId!, 'anyone?')
    expect(after.errorCode).toBe('cancelled')
  })

  it('is idempotent: an unknown id resolves as a logged no-op', async () => {
    const { engine, logEntries } = makeEngine({})

    await expect(engine.cancel('ghost')).resolves.toBeUndefined()

    expect(logEntries).toEqual([{ op: 'cancel', analysisSessionId: 'ghost', found: false }])
  })
})

// ---------------------------------------------------------------------------
// Concurrency cap.
// ---------------------------------------------------------------------------

describe('concurrency cap', () => {
  it('rejects requests beyond maxActiveSessions with too_many_active', async () => {
    const world = fakeWorld()
    const { engine } = makeEngine({ world, maxActiveSessions: 2 })

    const a = await engine.request(input({ title: 'a' }))
    const b = await engine.request(input({ title: 'b' }))
    expect(a.outcome).toBe('completed')
    expect(b.outcome).toBe('completed')

    const c = await engine.request(input({ title: 'c' }))
    expect(c.outcome).toBe('failed')
    expect(c.errorCode).toBe('too_many_active')
    expect(world.created).toHaveLength(2)

    // Cancelling one frees a slot.
    await engine.cancel(a.analysisSessionId!)
    const d = await engine.request(input({ title: 'd' }))
    expect(d.outcome).toBe('completed')
  })

  it('counts a pending create against the cap (no overshoot race)', async () => {
    vi.useFakeTimers()
    const world = fakeWorld({ createNever: true })
    const { engine } = makeEngine({ world, maxActiveSessions: 1, analysisTimeoutMs: 1000 })

    const pending = engine.request(input({ title: 'slow create' }))
    const second = await engine.request(input({ title: 'racer' }))
    expect(second.errorCode).toBe('too_many_active')

    await vi.advanceTimersByTimeAsync(1000)
    expect((await pending).outcome).toBe('timeout')
  })

  it('rejects a concurrent followup on a busy session with too_many_active', async () => {
    vi.useFakeTimers()
    let hang = false
    const world = fakeWorld({ respond: () => (hang ? null : 'ok') })
    const { engine } = makeEngine({ world })
    const first = await engine.request(input())

    hang = true
    const inFlight = engine.followup(first.analysisSessionId!, 'slow one')
    await Promise.resolve()
    const racer = await engine.followup(first.analysisSessionId!, 'racer')
    expect(racer.errorCode).toBe('too_many_active')

    await vi.advanceTimersByTimeAsync(DEFAULT_ANALYSIS_TIMEOUT_MS)
    expect((await inFlight).outcome).toBe('timeout')
  })
})

// ---------------------------------------------------------------------------
// Log hygiene (S8): analyzed content never reaches the log.
// ---------------------------------------------------------------------------

describe('log hygiene', () => {
  it('never logs summaryText, questions, or model replies — full-text scan', async () => {
    const SUMMARY_MARKER = 'SECRET_TRANSCRIPT_BODY_a6f2'
    const QUESTION_MARKER = 'SECRET_QUESTION_b7e1'
    const REPLY_MARKER = 'SECRET_REPLY_c8d0'
    const world = fakeWorld({ respond: () => `analysis mentioning ${REPLY_MARKER}` })
    const { engine, logEntries } = makeEngine({ world })

    const first = await engine.request(
      input({ title: 'observed session', summaryText: `body: ${SUMMARY_MARKER}` }),
    )
    await engine.followup(first.analysisSessionId!, `why ${QUESTION_MARKER}?`)
    await engine.cancel(first.analysisSessionId!)

    // One entry per contract op, all four ops present.
    const ops = new Set(logEntries.map((e) => e.op))
    expect(ops).toEqual(new Set(['create', 'followup', 'cancel', 'result']))

    const everything = JSON.stringify(logEntries)
    expect(everything).not.toContain(SUMMARY_MARKER)
    expect(everything).not.toContain(QUESTION_MARKER)
    expect(everything).not.toContain(REPLY_MARKER)

    // But the allowed metadata IS there.
    expect(everything).toContain('observed session')
    expect(everything).toContain(first.analysisSessionId!)
    expect(logEntries.some((e) => e.op === 'result' && e.tokensHint === 120)).toBe(true)
  })
})
