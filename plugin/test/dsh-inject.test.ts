/**
 * Unit tests for the dsh in-process injection executor (design §4.d path
 * one). A fake {@link AgentsServiceFace} records get/resume calls and every
 * message handed to followup/steer, so the tests can assert the F11
 * content-block shape, the loaded/resume dispatch, the normalized error
 * vocabulary, and the body-free logging contract.
 */

import { createHash } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'

import type { InjectExecutionRequest } from '../src/inject-gateway.ts'
import {
  createDshInjectExecutor,
  DEFAULT_PLUGIN_NAME,
  type AgentsServiceFace,
  type ColdPresetResolver,
  type DshAgentFace,
  type DshAgentHandleFace,
  type DshInjectLogLevel,
  type DshUserMessageFace,
  type HostDefaultModelResolver,
} from '../src/dsh-inject.ts'

// ---------------------------------------------------------------------------
// Fakes.
// ---------------------------------------------------------------------------

interface RecordedCall {
  method: 'followup' | 'steer'
  message: DshUserMessageFace
}

function fakeAgent(behavior?: { throwWith?: Error }): {
  agent: DshAgentFace
  calls: RecordedCall[]
} {
  const calls: RecordedCall[] = []
  const record = (method: 'followup' | 'steer') => (message: DshUserMessageFace) => {
    if (behavior?.throwWith) throw behavior.throwWith
    calls.push({ method, message })
  }
  return {
    agent: { followup: record('followup'), steer: record('steer') },
    calls,
  }
}

function fakeAgents(opts: {
  available?: boolean
  loaded?: Record<string, DshAgentFace>
  resume?: (
    options: Parameters<AgentsServiceFace['resume']>[0],
  ) => Promise<{
    agent: DshAgentFace
    dispose?: () => Promise<void>
  }>
}): {
  agents: AgentsServiceFace
  getCalls: string[]
  resumeCalls: Array<Parameters<AgentsServiceFace['resume']>[0]>
} {
  const getCalls: string[] = []
  const resumeCalls: Array<Parameters<AgentsServiceFace['resume']>[0]> = []
  const loaded = opts.loaded ?? {}
  const agents: AgentsServiceFace = {
    isAvailable: () => opts.available ?? true,
    get(sessionId) {
      getCalls.push(sessionId)
      return loaded[sessionId]
    },
    resume(options) {
      resumeCalls.push(options)
      if (opts.resume === undefined) {
        return Promise.reject(new Error('unexpected resume'))
      }
      return opts.resume(options).then((handle) => {
        loaded[options.resumeSessionId] = handle.agent
        return {
          agent: handle.agent,
          dispose: async () => {
            await (handle.dispose?.() ?? Promise.resolve())
            if (loaded[options.resumeSessionId] === handle.agent) {
              delete loaded[options.resumeSessionId]
            }
          },
        }
      })
    },
  }
  return { agents, getCalls, resumeCalls }
}

interface LogEntry {
  level: DshInjectLogLevel
  msg: string
  meta?: Record<string, unknown>
}

function collectLog(): { entries: LogEntry[]; log: (l: DshInjectLogLevel, m: string, meta?: Record<string, unknown>) => void } {
  const entries: LogEntry[] = []
  return {
    entries,
    log: (level, msg, meta) => entries.push({ level, msg, ...(meta !== undefined ? { meta } : {}) }),
  }
}

function request(overrides?: Partial<InjectExecutionRequest>): InjectExecutionRequest {
  return {
    target: { agent: 'dsh', sessionId: 'session-7' },
    mode: 'queue',
    message: 'hello from the sidecar board',
    requestId: 'req-0001',
    ...overrides,
  }
}

const HOST_ROUTE = {
  provider: 'host-provider-route',
  model: 'host-model-route',
} as const

const resolveHostDefaultModel: HostDefaultModelResolver = () => ({
  ...HOST_ROUTE,
})

const noOpSetupGuard = () => ({ commit() {} })
const resolveColdPreset: ColdPresetResolver = async () => ({
  state: 'absent',
  setup: noOpSetupGuard,
})

// ---------------------------------------------------------------------------
// Loaded-session dispatch and message shape (F11).
// ---------------------------------------------------------------------------

describe('loaded session', () => {
  it('queue mode calls followup exactly once and reports delivered', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({ loaded: { 'session-7': agent } })
    const resolver = vi.fn(resolveHostDefaultModel)
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel: resolver,
      resolveColdPreset,
    })

    expect(executor.kind).toBe('dsh')
    await expect(executor.preflight!(request().target)).resolves.toEqual({ ok: true })
    const result = await executor.execute(request({ mode: 'queue' }))

    expect(result).toEqual({ outcome: 'delivered' })
    expect(calls).toHaveLength(1)
    expect(calls[0]!.method).toBe('followup')
    expect(resumeCalls).toEqual([])
    expect(resolver).not.toHaveBeenCalled()
  })

  it('steer mode calls steer, not followup', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({ loaded: { 'session-7': agent } })
    const resolver = vi.fn(resolveHostDefaultModel)
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel: resolver,
      resolveColdPreset,
    })

    await expect(executor.preflight!(request().target)).resolves.toEqual({ ok: true })
    const result = await executor.execute(request({ mode: 'steer' }))

    expect(result).toEqual({ outcome: 'delivered' })
    expect(calls).toHaveLength(1)
    expect(calls[0]!.method).toBe('steer')
    expect(resumeCalls).toEqual([])
    expect(resolver).not.toHaveBeenCalled()
  })

  it('constructs the message as a single text content block with plugin source', async () => {
    const { agent, calls } = fakeAgent()
    const { agents } = fakeAgents({ loaded: { 'session-7': agent } })
    const executor = createDshInjectExecutor({ agents })

    await executor.execute(request({ message: '两行\n消息', requestId: 'req-f11' }))

    const message = calls[0]!.message
    expect(message.role).toBe('user')
    // F11: content is an ARRAY of blocks, exactly one text block here.
    expect(Array.isArray(message.content)).toBe(true)
    expect(message.content).toEqual([{ type: 'text', text: '两行\n消息' }])
    expect(message.source).toEqual({ kind: 'plugin', plugin: DEFAULT_PLUGIN_NAME })
    // The branded-id slot is a plain unique string tied to the requestId.
    expect(message.id).toBe('agent-sidecar-req-f11')
  })

  it('honors a custom pluginName in source attribution and message id', async () => {
    const { agent, calls } = fakeAgent()
    const { agents } = fakeAgents({ loaded: { 'session-7': agent } })
    const executor = createDshInjectExecutor({ agents, pluginName: 'my-fork' })

    await executor.execute(request({ requestId: 'req-2' }))

    expect(calls[0]!.message.source).toEqual({ kind: 'plugin', plugin: 'my-fork' })
    expect(calls[0]!.message.id).toBe('my-fork-req-2')
  })
})

// ---------------------------------------------------------------------------
// Unloaded session: resume-then-inject.
// ---------------------------------------------------------------------------

describe('unloaded session', () => {
  it('preflights and resumes with the complete host-default route before inbox acceptance', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, getCalls, resumeCalls } = fakeAgents({
      resume: () => Promise.resolve({ agent }),
    })
    const resolver = vi.fn(resolveHostDefaultModel)
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel: resolver,
      resolveColdPreset,
    })

    await expect(executor.preflight!(request().target)).resolves.toEqual({ ok: true })
    const result = await executor.execute(request({ mode: 'queue' }))

    expect(result).toEqual({ outcome: 'delivered' })
    expect(getCalls.length).toBeGreaterThanOrEqual(4)
    expect(getCalls.every((sessionId) => sessionId === 'session-7')).toBe(true)
    expect(resolver).toHaveBeenCalledTimes(2)
    expect(resumeCalls).toHaveLength(1)
    expect(resumeCalls[0]).toMatchObject({
      resumeSessionId: 'session-7',
      agentOptions: {
        provider: HOST_ROUTE.provider,
        model: HOST_ROUTE.model,
      },
    })
    expect(resumeCalls[0]!.signal).toBeInstanceOf(AbortSignal)
    expect(resumeCalls[0]!.signal?.aborted).toBe(false)
    expect(resumeCalls[0]!.setup).toBe(noOpSetupGuard)
    const setupResult = resumeCalls[0]!.setup!({})
    expect(setupResult).not.toBeInstanceOf(Promise)
    if (setupResult !== undefined && 'commit' in setupResult) {
      setupResult.commit()
    }
    expect(calls).toHaveLength(1)
    expect(calls[0]!.method).toBe('followup')
  })

  it('never resumes a loaded session (resume is not idempotent)', async () => {
    const { agent } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({
      loaded: { 'session-7': agent },
      resume: () => Promise.reject(new Error('cannot prepare session while it is live')),
    })
    const resolver = vi.fn(resolveHostDefaultModel)
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel: resolver,
    })

    const result = await executor.execute(request())

    expect(result.outcome).toBe('delivered')
    expect(resumeCalls).toEqual([])
    expect(resolver).not.toHaveBeenCalled()
  })

  it('maps a resume rejection without a live winner to sanitized executor_error', async () => {
    const { agents } = fakeAgents({
      resume: () => Promise.reject(new Error('stored session "session-7" failed validation')),
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
    })

    const result = await executor.execute(request())

    expect(result).toEqual({ outcome: 'failed', errorCode: 'executor_error' })
  })

  it('bounds the resume wait and maps it to failed/timeout', async () => {
    const { agents } = fakeAgents({
      resume: () => new Promise(() => {}), // never settles
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
      resumeTimeoutMs: 20,
    })

    const result = await executor.execute(request())

    expect(result.outcome).toBe('failed')
    expect(result.errorCode).toBe('timeout')
  })

  it.each([
    ['absent', undefined],
    ['throwing', () => { throw new Error('selection failed') }],
    ['partial', () => ({ provider: HOST_ROUTE.provider })],
    ['blank', () => ({ provider: '   ', model: '\t' })],
  ] as const)(
    'rejects a cold %s host-default resolver as dsh_model_unconfigured',
    async (_case, rawResolver) => {
      const { agent, calls } = fakeAgent()
      const { agents, resumeCalls } = fakeAgents({
        resume: () => Promise.resolve({ agent }),
      })
      const executor = createDshInjectExecutor({
        agents,
        ...(rawResolver === undefined
          ? {}
          : {
              resolveHostDefaultModel:
                rawResolver as unknown as HostDefaultModelResolver,
            }),
        resolveColdPreset,
      })

      await expect(executor.preflight!(request().target)).resolves.toEqual({
        ok: false,
        errorCode: 'dsh_model_unconfigured',
      })
      await expect(executor.execute(request())).resolves.toEqual({
        outcome: 'failed',
        errorCode: 'dsh_model_unconfigured',
      })
      expect(resumeCalls).toHaveLength(0)
      expect(calls).toHaveLength(0)
    },
  )

  it('rejects a missing preset inspection port as executor_error', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({
      resume: () => Promise.resolve({ agent }),
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
    })

    await expect(executor.preflight!(request().target)).resolves.toEqual({
      ok: false,
      errorCode: 'executor_error',
    })
    expect(resumeCalls).toHaveLength(0)
    expect(calls).toHaveLength(0)
  })

  it('maps an authoritative missing proof before model lookup or resume', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({
      resume: () => Promise.resolve({ agent }),
    })
    const resolver = vi.fn(resolveHostDefaultModel)
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel: resolver,
      resolveColdPreset: async () => ({ state: 'missing' }),
    })

    await expect(executor.preflight!(request().target)).resolves.toEqual({
      ok: false,
      errorCode: 'target_not_found',
    })
    await expect(executor.execute(request())).resolves.toEqual({
      outcome: 'failed',
      errorCode: 'target_not_found',
    })
    expect(resolver).not.toHaveBeenCalled()
    expect(resumeCalls).toHaveLength(0)
    expect(calls).toHaveLength(0)
  })

  it('reclassifies a resume deletion race as target_not_found without splice', async () => {
    const { calls } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({
      resume: async (): Promise<never> => {
        throw new Error('/SECRET/session-7 was deleted')
      },
    })
    const preset = vi
      .fn<ColdPresetResolver>()
      .mockResolvedValueOnce({
        state: 'absent',
        setup: noOpSetupGuard,
      })
      .mockResolvedValueOnce({ state: 'missing' })
    const { entries, log } = collectLog()
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset: preset,
      log,
    })

    await expect(executor.execute(request())).resolves.toEqual({
      outcome: 'failed',
      errorCode: 'target_not_found',
    })
    expect(preset).toHaveBeenCalledTimes(2)
    expect(resumeCalls).toHaveLength(1)
    expect(calls).toHaveLength(0)
    const serialized = JSON.stringify(entries)
    expect(serialized).not.toContain('session-7')
    expect(serialized).not.toContain('/SECRET')
    expect(serialized).not.toContain(HOST_ROUTE.provider)
    expect(serialized).not.toContain(HOST_ROUTE.model)
  })

  it('fails closed when the host-default route disappears after prepare', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({
      resume: () => Promise.resolve({ agent }),
    })
    const resolver = vi
      .fn<HostDefaultModelResolver>()
      .mockReturnValueOnce({ ...HOST_ROUTE })
      .mockReturnValue(null)
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel: resolver,
      resolveColdPreset,
    })

    await expect(executor.preflight!(request().target)).resolves.toEqual({ ok: true })
    await expect(executor.execute(request())).resolves.toEqual({
      outcome: 'failed',
      errorCode: 'dsh_model_unconfigured',
    })
    expect(resumeCalls).toHaveLength(0)
    expect(calls).toHaveLength(0)
  })

  it('rechecks effective preset after prepare and refuses a newly-present preset', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({
      resume: () => Promise.resolve({ agent }),
    })
    const preset = vi
      .fn<ColdPresetResolver>()
      .mockResolvedValueOnce({
        state: 'absent',
        setup: noOpSetupGuard,
      })
      .mockResolvedValue({ state: 'present' })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset: preset,
    })

    await expect(executor.preflight!(request().target)).resolves.toEqual({
      ok: true,
    })
    await expect(executor.execute(request())).resolves.toEqual({
      outcome: 'failed',
      errorCode: 'dsh_preset_unsupported',
    })
    expect(preset).toHaveBeenCalledTimes(2)
    expect(resumeCalls).toHaveLength(0)
    expect(calls).toHaveLength(0)
  })

  it('uses a newly-live Agent after prepare without resolving a route again', async () => {
    const { agent, calls } = fakeAgent()
    let live = false
    const resume = vi.fn<AgentsServiceFace['resume']>()
    const agents: AgentsServiceFace = {
      isAvailable: () => true,
      get: () => (live ? agent : undefined),
      resume,
    }
    const resolver = vi.fn(resolveHostDefaultModel)
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel: resolver,
      resolveColdPreset,
    })

    await expect(executor.preflight!(request().target)).resolves.toEqual({ ok: true })
    live = true
    resolver.mockReturnValue(null)
    await expect(executor.execute(request())).resolves.toEqual({
      outcome: 'delivered',
    })
    expect(resolver).toHaveBeenCalledTimes(1)
    expect(resume).not.toHaveBeenCalled()
    expect(calls).toHaveLength(1)
  })

  it('aborts a timed-out preset inspection before resume or splice', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({
      resume: () => Promise.resolve({ agent }),
    })
    let inspectedSignal: AbortSignal | undefined
    const pendingInspection: ColdPresetResolver = async (_sessionId, signal) => {
      inspectedSignal = signal
      return await new Promise<Awaited<ReturnType<ColdPresetResolver>>>(
        (_resolve, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason), {
          once: true,
        })
        },
      )
    }
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset: pendingInspection,
      resumeTimeoutMs: 10,
    })

    await expect(executor.preflight!(request().target)).resolves.toEqual({
      ok: false,
      errorCode: 'executor_error',
    })
    expect(inspectedSignal?.aborted).toBe(true)
    expect(resumeCalls).toHaveLength(0)
    expect(calls).toHaveLength(0)
  })

  it('retains a late fulfilled slot after request timeout and reuses it', async () => {
    const { agent, calls } = fakeAgent()
    const loaded: Record<string, DshAgentFace> = {}
    const dispose = vi.fn(async () => {})
    let settleResume!: (handle: DshAgentHandleFace) => void
    const pending = new Promise<DshAgentHandleFace>((resolve) => {
      settleResume = resolve
    })
    const { agents, resumeCalls } = fakeAgents({
      loaded,
      resume: () => pending,
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
      resumeTimeoutMs: 100,
      requestTimeoutMs: () => 10,
    })

    await expect(executor.execute(request())).resolves.toEqual({
      outcome: 'failed',
      errorCode: 'timeout',
    })
    expect(resumeCalls).toHaveLength(1)
    expect(resumeCalls[0]!.signal?.aborted).toBe(false)
    expect(calls).toHaveLength(0)

    loaded['session-7'] = agent
    settleResume({ agent, dispose })
    await expect(
      executor.execute(
        request({ requestId: 'req-reuse', message: 'reused message' }),
      ),
    ).resolves.toEqual({ outcome: 'delivered' })
    expect(resumeCalls).toHaveLength(1)
    expect(dispose).not.toHaveBeenCalled()
    expect(calls).toHaveLength(1)

    await executor.dispose()
    expect(dispose).toHaveBeenCalledTimes(1)
  })

  it('does not borrow a fulfilled slot whose agent owner already destroyed it', async () => {
    const old = fakeAgent()
    const replacement = fakeAgent()
    const loaded: Record<string, DshAgentFace> = {}
    let ownerDisposed = false
    const disposeOld = vi.fn(async () => {
      if (ownerDisposed) return
      ownerDisposed = true
      delete loaded['session-7']
    })
    let attempt = 0
    const { agents, resumeCalls } = fakeAgents({
      loaded,
      resume: async () => {
        attempt += 1
        const agent = attempt === 1 ? old.agent : replacement.agent
        loaded['session-7'] = agent
        return {
          agent,
          dispose: attempt === 1 ? disposeOld : async () => {},
        }
      },
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
    })

    await expect(
      executor.execute(request({ requestId: 'req-old', message: 'old message' })),
    ).resolves.toEqual({ outcome: 'delivered' })
    await disposeOld()
    await expect(
      executor.execute(request({ requestId: 'req-new', message: 'new message' })),
    ).resolves.toEqual({ outcome: 'delivered' })

    expect(resumeCalls).toHaveLength(2)
    expect(old.calls).toHaveLength(1)
    expect(replacement.calls).toHaveLength(1)
  })

  it('keeps a retiring tombstone until old-agent disposal detaches', async () => {
    const old = fakeAgent()
    const replacement = fakeAgent()
    const loaded: Record<string, DshAgentFace> = {}
    const oldGeneration = {}
    const newGeneration = {}
    let generation: object | null = oldGeneration
    let finishDispose!: () => void
    const disposal = new Promise<void>((resolve) => {
      finishDispose = resolve
    })
    let attempt = 0
    const { agents, resumeCalls } = fakeAgents({
      loaded,
      resume: async () => {
        attempt += 1
        return attempt === 1
          ? { agent: old.agent, dispose: () => disposal }
          : { agent: replacement.agent, dispose: async () => {} }
      },
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
      currentColdServiceGeneration: () => generation,
      requestTimeoutMs: () => 100,
    })
    await expect(
      executor.execute(request({ requestId: 'req-old', message: 'old message' })),
    ).resolves.toEqual({ outcome: 'delivered' })

    generation = newGeneration
    const retiring = executor.invalidateColdServiceGeneration(oldGeneration)
    let secondSettled = false
    const second = executor
      .execute(request({ requestId: 'req-new', message: 'new message' }))
      .then((result) => {
        secondSettled = true
        return result
      })
    await new Promise<void>((resolve) => setTimeout(resolve, 20))
    expect(loaded['session-7']).toBe(old.agent)
    expect(secondSettled).toBe(false)
    expect(resumeCalls).toHaveLength(1)
    expect(old.calls).toHaveLength(1)
    expect(replacement.calls).toHaveLength(0)

    finishDispose()
    await retiring
    await expect(second).resolves.toEqual({ outcome: 'delivered' })
    expect(resumeCalls).toHaveLength(2)
    expect(old.calls).toHaveLength(1)
    expect(replacement.calls).toHaveLength(1)
  })

  it('times out a waiter without bypassing a hanging retiring tombstone', async () => {
    const old = fakeAgent()
    const replacement = fakeAgent()
    const loaded: Record<string, DshAgentFace> = {}
    const oldGeneration = {}
    const newGeneration = {}
    let generation: object | null = oldGeneration
    let finishDispose!: () => void
    const disposal = new Promise<void>((resolve) => {
      finishDispose = resolve
    })
    let attempt = 0
    const { agents, resumeCalls } = fakeAgents({
      loaded,
      resume: async () => {
        attempt += 1
        return attempt === 1
          ? { agent: old.agent, dispose: () => disposal }
          : { agent: replacement.agent, dispose: async () => {} }
      },
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
      currentColdServiceGeneration: () => generation,
      requestTimeoutMs: (req) => (req.requestId === 'req-short' ? 10 : 100),
    })
    await executor.execute(
      request({ requestId: 'req-old', message: 'old message' }),
    )
    generation = newGeneration
    const retiring = executor.invalidateColdServiceGeneration(oldGeneration)

    await expect(
      executor.execute(
        request({ requestId: 'req-short', message: 'must not splice' }),
      ),
    ).resolves.toEqual({ outcome: 'failed', errorCode: 'timeout' })
    expect(loaded['session-7']).toBe(old.agent)
    expect(resumeCalls).toHaveLength(1)
    expect(old.calls).toHaveLength(1)
    expect(replacement.calls).toHaveLength(0)

    finishDispose()
    await retiring
  })

  it('removes a failed retirement tombstone with stable executor_error', async () => {
    const old = fakeAgent()
    const replacement = fakeAgent()
    const loaded: Record<string, DshAgentFace> = {}
    const oldGeneration = {}
    const newGeneration = {}
    let generation: object | null = oldGeneration
    let attempt = 0
    const { agents, resumeCalls } = fakeAgents({
      loaded,
      resume: async () => {
        attempt += 1
        if (attempt === 1) {
          return {
            agent: old.agent,
            dispose: async () => {
              delete loaded['session-7']
              throw new Error('SECRET dispose failure')
            },
          }
        }
        return { agent: replacement.agent, dispose: async () => {} }
      },
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
      currentColdServiceGeneration: () => generation,
    })
    await executor.execute(
      request({ requestId: 'req-old', message: 'old message' }),
    )
    generation = newGeneration
    const retiring = executor.invalidateColdServiceGeneration(oldGeneration)

    await expect(
      executor.execute(
        request({ requestId: 'req-failed-retire', message: 'no splice' }),
      ),
    ).resolves.toEqual({ outcome: 'failed', errorCode: 'executor_error' })
    await retiring
    expect(old.calls).toHaveLength(1)
    expect(replacement.calls).toHaveLength(0)

    await expect(
      executor.execute(
        request({ requestId: 'req-retry', message: 'new generation retry' }),
      ),
    ).resolves.toEqual({ outcome: 'delivered' })
    expect(resumeCalls).toHaveLength(2)
    expect(replacement.calls).toHaveLength(1)
  })

  it('clears the deadline when resume resolves before it wins', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({
      resume: () => Promise.resolve({ agent }),
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
      resumeTimeoutMs: 15,
    })

    await expect(executor.execute(request())).resolves.toEqual({
      outcome: 'delivered',
    })
    await new Promise<void>((resolve) => setTimeout(resolve, 25))
    expect(resumeCalls[0]!.signal?.aborted).toBe(false)
    expect(calls).toHaveLength(1)
  })

  it('shares one same-route resume and splices each waiter message once', async () => {
    const { agent, calls } = fakeAgent()
    let settleWinner!: (handle: DshAgentHandleFace) => void
    const winner = new Promise<DshAgentHandleFace>((resolve) => {
      settleWinner = resolve
    })
    const { agents, resumeCalls } = fakeAgents({
      resume: () => winner,
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
      resumeTimeoutMs: 100,
    })

    const first = executor.execute(
      request({ requestId: 'req-a', message: 'message-a' }),
    )
    const second = executor.execute(
      request({ requestId: 'req-b', message: 'message-b' }),
    )
    await vi.waitFor(() => expect(resumeCalls).toHaveLength(1))
    settleWinner({ agent, dispose: async () => {} })

    await expect(Promise.all([first, second])).resolves.toEqual([
      { outcome: 'delivered' },
      { outcome: 'delivered' },
    ])
    expect(resumeCalls).toHaveLength(1)
    expect(calls).toHaveLength(2)
    expect(
      calls.map((call) =>
        (call.message.content[0] as { readonly text?: string }).text,
      ).sort(),
    ).toEqual(['message-a', 'message-b'])
  })

  it('keeps short and long waiters independent on the first-route slot winner', async () => {
    const { agent, calls } = fakeAgent()
    const dispose = vi.fn(async () => {})
    const loaded: Record<string, DshAgentFace> = {}
    const routeA = { provider: 'provider-a', model: 'model-a' }
    const routeB = { provider: 'provider-b', model: 'model-b' }
    const routes = [routeA, routeB]
    const resolver = vi.fn<HostDefaultModelResolver>(() => routes.shift() ?? null)
    let settleWinner!: (handle: DshAgentHandleFace) => void
    const winner = new Promise<DshAgentHandleFace>((resolve) => {
      settleWinner = resolve
    })
    const { agents, resumeCalls } = fakeAgents({
      loaded,
      resume: () => winner,
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel: resolver,
      resolveColdPreset,
      resumeTimeoutMs: 100,
      requestTimeoutMs: (req) =>
        req.requestId === 'req-short' ? 10 : 100,
    })

    const short = executor.execute(
      request({ requestId: 'req-short', message: 'short message' }),
    )
    await vi.waitFor(() => expect(resumeCalls).toHaveLength(1))
    // Publication may precede AgentRegistry.resume fulfillment. The resuming
    // slot must still win over get() for the long waiter.
    loaded['session-7'] = agent
    const long = executor.execute(
      request({ requestId: 'req-long', message: 'long message' }),
    )

    await expect(short).resolves.toEqual({
      outcome: 'failed',
      errorCode: 'timeout',
    })
    expect(dispose).not.toHaveBeenCalled()
    settleWinner({ agent, dispose })
    await expect(long).resolves.toEqual({ outcome: 'delivered' })

    expect(resumeCalls).toHaveLength(1)
    expect(resumeCalls[0]!.agentOptions).toEqual(routeA)
    expect(resumeCalls[0]!.signal?.aborted).toBe(false)
    expect(resolver).toHaveBeenCalledTimes(1)
    expect(calls).toHaveLength(1)
    expect(
      (calls[0]!.message.content[0] as { readonly text?: string }).text,
    ).toBe('long message')
    expect(dispose).not.toHaveBeenCalled()
  })

  it('aborts and owns cleanup when unloading a pending slot', async () => {
    const { agent, calls } = fakeAgent()
    const disposeHandle = vi.fn(async () => {})
    let settleResume!: (handle: DshAgentHandleFace) => void
    const pending = new Promise<DshAgentHandleFace>((resolve) => {
      settleResume = resolve
    })
    const { agents, resumeCalls } = fakeAgents({
      resume: () => pending,
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
      resumeTimeoutMs: 100,
    })
    const execution = executor.execute(request())
    await vi.waitFor(() => expect(resumeCalls).toHaveLength(1))

    const unloading = executor.dispose()
    expect(resumeCalls[0]!.signal?.aborted).toBe(true)
    settleResume({ agent, dispose: disposeHandle })

    await unloading
    await expect(execution).resolves.toEqual({
      outcome: 'failed',
      errorCode: 'executor_error',
    })
    expect(disposeHandle).toHaveBeenCalledTimes(1)
    expect(calls).toHaveLength(0)
  })

  it('adopts an external winner after resume rejection without parsing error text', async () => {
    const { agent, calls } = fakeAgent()
    const loaded: Record<string, DshAgentFace> = {}
    const { agents, resumeCalls } = fakeAgents({
      loaded,
      resume: async () => {
        loaded['session-7'] = agent
        throw new Error('SECRET collision wording must not matter')
      },
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
    })

    await expect(executor.execute(request())).resolves.toEqual({
      outcome: 'delivered',
    })
    expect(resumeCalls).toHaveLength(1)
    expect(calls).toHaveLength(1)
  })

  it('never adopts a winner that appears only after the request deadline', async () => {
    const { agent, calls } = fakeAgent()
    const loaded: Record<string, DshAgentFace> = {}
    let rejectResume!: (error: Error) => void
    const pending = new Promise<never>((_resolve, reject) => {
      rejectResume = reject
    })
    const { agents } = fakeAgents({
      loaded,
      resume: () => pending,
    })
    const executor = createDshInjectExecutor({
      agents,
      resolveHostDefaultModel,
      resolveColdPreset,
      resumeTimeoutMs: 10,
    })

    await expect(executor.execute(request())).resolves.toEqual({
      outcome: 'failed',
      errorCode: 'timeout',
    })
    loaded['session-7'] = agent
    rejectResume(new Error('late collision'))
    await new Promise<void>((resolve) => queueMicrotask(resolve))
    expect(calls).toHaveLength(0)
  })
})

// ---------------------------------------------------------------------------
// Synchronous injection failure.
// ---------------------------------------------------------------------------

describe('injection call failure', () => {
  it('maps a synchronous followup throw to failed/executor_error', async () => {
    const { agent } = fakeAgent({ throwWith: new Error('agent "session-7" is disposed') })
    const { agents } = fakeAgents({ loaded: { 'session-7': agent } })
    const executor = createDshInjectExecutor({ agents })

    const result = await executor.execute(request({ mode: 'queue' }))

    expect(result).toEqual({
      outcome: 'failed',
      errorCode: 'executor_error',
      detail: 'dsh injection call failed: agent "session-7" is disposed',
    })
  })

  it('maps a synchronous steer throw to failed/executor_error', async () => {
    const { agent } = fakeAgent({ throwWith: new Error('boom') })
    const { agents } = fakeAgents({ loaded: { 'session-7': agent } })
    const executor = createDshInjectExecutor({ agents })

    const result = await executor.execute(request({ mode: 'steer' }))

    expect(result).toEqual({
      outcome: 'failed',
      errorCode: 'executor_error',
      detail: 'dsh injection call failed: boom',
    })
  })

  it('sanitizes a non-Error splice throw', async () => {
    const { agents } = fakeAgents({
      loaded: {
        'session-7': {
          followup() {
            // oxlint-disable-next-line no-throw-literal
            throw 'raw failure'
          },
          steer() {},
        },
      },
    })
    const executor = createDshInjectExecutor({ agents })

    const result = await executor.execute(request())

    expect(result).toEqual({
      outcome: 'failed',
      errorCode: 'executor_error',
      detail: 'dsh injection call failed: raw failure',
    })
  })

  it('redacts absolute paths and bounds splice failure detail', async () => {
    const { agent } = fakeAgent({
      throwWith: new Error(`/SECRET/private/${'x'.repeat(800)}`),
    })
    const { agents } = fakeAgents({ loaded: { 'session-7': agent } })
    const executor = createDshInjectExecutor({ agents })

    const result = await executor.execute(request())

    expect(result).toMatchObject({
      outcome: 'failed',
      errorCode: 'executor_error',
      detail: 'dsh injection call failed: <path>',
    })
    expect(result.detail).not.toContain('/SECRET/private')
    expect(result.detail!.length).toBeLessThanOrEqual(512)
  })
})

// ---------------------------------------------------------------------------
// Logging: body-free, digest-bearing (S8).
// ---------------------------------------------------------------------------

describe('logging', () => {
  const SECRET = 'TOP-SECRET-PLAINTEXT-marker-91c4'

  async function runAll(): Promise<LogEntry[]> {
    const merged: LogEntry[] = []

    // delivered (loaded), resumed, resume-failed, timeout, sync-throw.
    {
      const { agent } = fakeAgent()
      const { entries, log } = collectLog()
      await createDshInjectExecutor({
        agents: fakeAgents({ loaded: { s: agent } }).agents,
        log,
      }).execute(request({ target: { agent: 'dsh', sessionId: 's' }, message: SECRET }))
      merged.push(...entries)
    }
    {
      const { agent } = fakeAgent()
      const { entries, log } = collectLog()
      await createDshInjectExecutor({
        agents: fakeAgents({ resume: () => Promise.resolve({ agent }) }).agents,
        resolveHostDefaultModel,
        resolveColdPreset,
        log,
      }).execute(request({ message: SECRET }))
      merged.push(...entries)
    }
    {
      const { entries, log } = collectLog()
      await createDshInjectExecutor({
        agents: fakeAgents({ resume: () => Promise.reject(new Error('nope')) }).agents,
        resolveHostDefaultModel,
        resolveColdPreset,
        log,
      }).execute(request({ message: SECRET }))
      merged.push(...entries)
    }
    {
      const { entries, log } = collectLog()
      await createDshInjectExecutor({
        agents: fakeAgents({ resume: () => new Promise(() => {}) }).agents,
        resolveHostDefaultModel,
        resolveColdPreset,
        log,
        resumeTimeoutMs: 10,
      }).execute(request({ message: SECRET }))
      merged.push(...entries)
    }
    {
      const { agent } = fakeAgent({ throwWith: new Error('sync boom') })
      const { entries, log } = collectLog()
      await createDshInjectExecutor({
        agents: fakeAgents({ loaded: { 'session-7': agent } }).agents,
        log,
      }).execute(request({ message: SECRET }))
      merged.push(...entries)
    }
    return merged
  }

  it('never logs the message body on any path', async () => {
    const entries = await runAll()
    expect(entries.length).toBeGreaterThan(0)
    const serialized = JSON.stringify(entries)
    expect(serialized).not.toContain(SECRET)
  })

  it('never logs provider/model values and records only host-default provenance', async () => {
    const { agent } = fakeAgent()
    const { entries, log } = collectLog()
    const provider = 'SECRET-PROVIDER-VALUE-8d1c'
    const model = 'SECRET-MODEL-VALUE-2a7f'
    await createDshInjectExecutor({
      agents: fakeAgents({
        resume: () => Promise.resolve({ agent }),
      }).agents,
      resolveHostDefaultModel: () => ({ provider, model }),
      resolveColdPreset,
      log,
    }).execute(request())

    const serialized = JSON.stringify(entries)
    expect(serialized).not.toContain(provider)
    expect(serialized).not.toContain(model)
    expect(serialized).toContain('"routingSource":"host-default"')
    expect(serialized).toContain('"modelRouteAvailable":true')
  })

  it('logs byte size and the sha256 12-char prefix instead', async () => {
    const entries = await runAll()
    const expectedSha12 = createHash('sha256').update(SECRET, 'utf8').digest('hex').slice(0, 12)
    for (const entry of entries) {
      expect(entry.meta?.['messageBytes']).toBe(Buffer.byteLength(SECRET, 'utf8'))
      expect(entry.meta?.['messageSha12']).toBe(expectedSha12)
    }
  })

  it('marks whether the delivery needed a resume', async () => {
    const { agent } = fakeAgent()
    const { entries, log } = collectLog()
    await createDshInjectExecutor({
      agents: fakeAgents({ resume: () => Promise.resolve({ agent }) }).agents,
      resolveHostDefaultModel,
      resolveColdPreset,
      log,
    }).execute(request())

    const delivered = entries.find((e) => e.msg === 'dsh injection delivered')
    expect(delivered?.meta?.['resumed']).toBe(true)
  })
})
