/**
 * Unit tests for the dsh in-process injection executor (design §4.d path
 * one). A fake {@link AgentsServiceFace} records get/resume calls and every
 * message handed to followup/steer, so the tests can assert the F11
 * content-block shape, the loaded/resume dispatch, the normalized error
 * vocabulary, and the body-free logging contract.
 */

import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'

import type { InjectExecutionRequest } from '../src/inject-gateway.ts'
import {
  createDshInjectExecutor,
  DEFAULT_PLUGIN_NAME,
  type AgentsServiceFace,
  type DshAgentFace,
  type DshInjectLogLevel,
  type DshUserMessageFace,
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
  loaded?: Record<string, DshAgentFace>
  resume?: (sessionId: string) => Promise<{ agent: DshAgentFace }>
}): { agents: AgentsServiceFace; getCalls: string[]; resumeCalls: string[] } {
  const getCalls: string[] = []
  const resumeCalls: string[] = []
  const agents: AgentsServiceFace = {
    get(sessionId) {
      getCalls.push(sessionId)
      return opts.loaded?.[sessionId]
    },
    resume(options) {
      resumeCalls.push(options.resumeSessionId)
      if (opts.resume === undefined) {
        return Promise.reject(new Error('unexpected resume'))
      }
      return opts.resume(options.resumeSessionId)
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

// ---------------------------------------------------------------------------
// Loaded-session dispatch and message shape (F11).
// ---------------------------------------------------------------------------

describe('loaded session', () => {
  it('queue mode calls followup exactly once and reports delivered', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({ loaded: { 'session-7': agent } })
    const executor = createDshInjectExecutor({ agents })

    expect(executor.kind).toBe('dsh')
    const result = await executor.execute(request({ mode: 'queue' }))

    expect(result).toEqual({ outcome: 'delivered' })
    expect(calls).toHaveLength(1)
    expect(calls[0]!.method).toBe('followup')
    expect(resumeCalls).toEqual([])
  })

  it('steer mode calls steer, not followup', async () => {
    const { agent, calls } = fakeAgent()
    const { agents } = fakeAgents({ loaded: { 'session-7': agent } })
    const executor = createDshInjectExecutor({ agents })

    const result = await executor.execute(request({ mode: 'steer' }))

    expect(result).toEqual({ outcome: 'delivered' })
    expect(calls).toHaveLength(1)
    expect(calls[0]!.method).toBe('steer')
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
  it('resumes with resumeSessionId, then injects into the resumed agent', async () => {
    const { agent, calls } = fakeAgent()
    const { agents, getCalls, resumeCalls } = fakeAgents({
      resume: () => Promise.resolve({ agent }),
    })
    const executor = createDshInjectExecutor({ agents })

    const result = await executor.execute(request({ mode: 'queue' }))

    expect(result).toEqual({ outcome: 'delivered' })
    expect(getCalls).toEqual(['session-7'])
    expect(resumeCalls).toEqual(['session-7'])
    expect(calls).toHaveLength(1)
    expect(calls[0]!.method).toBe('followup')
  })

  it('never resumes a loaded session (resume is not idempotent)', async () => {
    const { agent } = fakeAgent()
    const { agents, resumeCalls } = fakeAgents({
      loaded: { 'session-7': agent },
      resume: () => Promise.reject(new Error('cannot prepare session while it is live')),
    })
    const executor = createDshInjectExecutor({ agents })

    const result = await executor.execute(request())

    expect(result.outcome).toBe('delivered')
    expect(resumeCalls).toEqual([])
  })

  it('maps a resume rejection to failed/session_not_found with dsh detail', async () => {
    const { agents } = fakeAgents({
      resume: () => Promise.reject(new Error('stored session "session-7" failed validation')),
    })
    const executor = createDshInjectExecutor({ agents })

    const result = await executor.execute(request())

    expect(result).toEqual({
      outcome: 'failed',
      errorCode: 'session_not_found',
      detail: 'stored session "session-7" failed validation',
    })
  })

  it('bounds the resume wait and maps it to failed/timeout', async () => {
    const { agents } = fakeAgents({
      resume: () => new Promise(() => {}), // never settles
    })
    const executor = createDshInjectExecutor({ agents, resumeTimeoutMs: 20 })

    const result = await executor.execute(request())

    expect(result.outcome).toBe('failed')
    expect(result.errorCode).toBe('timeout')
    expect(result.detail).toContain('20ms')
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
      detail: 'agent "session-7" is disposed',
    })
  })

  it('maps a synchronous steer throw to failed/executor_error', async () => {
    const { agent } = fakeAgent({ throwWith: new Error('boom') })
    const { agents } = fakeAgents({ loaded: { 'session-7': agent } })
    const executor = createDshInjectExecutor({ agents })

    const result = await executor.execute(request({ mode: 'steer' }))

    expect(result).toEqual({ outcome: 'failed', errorCode: 'executor_error', detail: 'boom' })
  })

  it('maps a non-Error throw through String()', async () => {
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

    expect(result).toEqual({ outcome: 'failed', errorCode: 'executor_error', detail: 'raw failure' })
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
        log,
      }).execute(request({ message: SECRET }))
      merged.push(...entries)
    }
    {
      const { entries, log } = collectLog()
      await createDshInjectExecutor({
        agents: fakeAgents({ resume: () => Promise.reject(new Error('nope')) }).agents,
        log,
      }).execute(request({ message: SECRET }))
      merged.push(...entries)
    }
    {
      const { entries, log } = collectLog()
      await createDshInjectExecutor({
        agents: fakeAgents({ resume: () => new Promise(() => {}) }).agents,
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
      log,
    }).execute(request())

    const delivered = entries.find((e) => e.msg === 'dsh injection delivered')
    expect(delivered?.meta?.['resumed']).toBe(true)
  })
})
