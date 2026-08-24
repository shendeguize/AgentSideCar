/**
 * Unit tests for the InjectGateway (design §4.d / §4.f.5 / §8).
 * Fake executors, fake clock, fake requestId source; no cordis/dsh, no I/O.
 */

import { describe, expect, it } from 'vitest'
import { createHash } from 'node:crypto'
import {
  InjectGateway,
  MAX_MESSAGE_BYTES,
  MAX_PENDING_TOKENS,
  RESULT_CACHE_TTL_MS,
  TOKEN_TTL_MS,
  type InjectExecutionRequest,
  type InjectExecutor,
  type InjectLogEntry,
  type InjectResult,
  type InjectTarget,
  type TargetStatus,
} from '../src/inject-gateway.ts'

const T0 = 1_700_000_000_000

const TARGET_DSH: InjectTarget = { agent: 'dsh', sessionId: 'sess-dsh-1' }
const TARGET_CLAUDE: InjectTarget = { agent: 'claude', sessionId: 'sess-claude-1' }

interface FakeExecutor {
  executor: InjectExecutor
  calls: InjectExecutionRequest[]
  setResult(result: InjectResult): void
  setError(message: string): void
}

function fakeExecutor(kind: 'dsh' | 'send-cli'): FakeExecutor {
  const calls: InjectExecutionRequest[] = []
  let result: InjectResult = { outcome: 'delivered' }
  let error: string | null = null
  return {
    calls,
    setResult(next) {
      result = next
      error = null
    },
    setError(message) {
      error = message
    },
    executor: {
      kind,
      async execute(req) {
        calls.push(req)
        if (error !== null) throw new Error(error)
        return { ...result }
      },
    },
  }
}

type VerifyFn = (target: InjectTarget) => Promise<TargetStatus | null>

interface Harness {
  gateway: InjectGateway
  logs: InjectLogEntry[]
  dsh: FakeExecutor
  sendCli: FakeExecutor
  verifyCalls: InjectTarget[]
  advance(ms: number): void
  setAllowWrite(value: boolean): void
  setVerify(fn: VerifyFn): void
}

function makeHarness(): Harness {
  let clock = T0
  let allowWrite = true
  let seq = 0
  let verify: VerifyFn = async (target) => ({
    agent: target.agent,
    sessionId: target.sessionId,
    status: 'waiting',
  })
  const logs: InjectLogEntry[] = []
  const verifyCalls: InjectTarget[] = []
  const dsh = fakeExecutor('dsh')
  const sendCli = fakeExecutor('send-cli')
  const gateway = new InjectGateway({
    executors: { dsh: dsh.executor, sendCli: sendCli.executor },
    verifyTarget(target) {
      verifyCalls.push(target)
      return verify(target)
    },
    allowWrite: () => allowWrite,
    log: (entry) => logs.push(entry),
    now: () => clock,
    randomId: () => `req-${++seq}`,
  })
  return {
    gateway,
    logs,
    dsh,
    sendCli,
    verifyCalls,
    advance: (ms) => {
      clock += ms
    },
    setAllowWrite: (value) => {
      allowWrite = value
    },
    setVerify: (fn) => {
      verify = fn
    },
  }
}

/** prepare() that must succeed; narrows the union for the caller. */
async function prepared(
  h: Harness,
  target: InjectTarget,
  message: string,
  mode: 'queue' | 'steer' = 'queue',
): Promise<{ requestId: string; confirmToken: string }> {
  const prep = await h.gateway.prepare({ target, mode, message })
  if (!prep.ok) throw new Error(`prepare rejected: ${prep.errorCode}`)
  return { requestId: prep.requestId, confirmToken: prep.confirmToken }
}

function sha12(message: string): string {
  return createHash('sha256').update(message, 'utf8').digest('hex').slice(0, 12)
}

// ---------------------------------------------------------------------------

describe('prepare', () => {
  it('issues requestId + 128-bit confirmToken with 60s TTL and a body-free plan', async () => {
    const h = makeHarness()
    const message = 'inject me please'
    const prep = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'steer', message })
    expect(prep.ok).toBe(true)
    if (!prep.ok) return
    expect(prep.requestId).toBe('req-1')
    expect(prep.confirmToken).toMatch(/^[0-9a-f]{32}$/) // 16 bytes hex = 128 bits
    expect(prep.expiresAt).toBe(T0 + TOKEN_TTL_MS)
    expect(prep.plan.target).toEqual(TARGET_CLAUDE)
    expect(prep.plan.mode).toBe('steer')
    expect(prep.plan.targetStatus).toEqual({
      agent: 'claude',
      sessionId: 'sess-claude-1',
      status: 'waiting',
    })
    expect(prep.plan.messagePreview).toEqual({
      bytes: Buffer.byteLength(message, 'utf8'),
      head: message, // shorter than the head cap → full prefix
    })
  })

  it('rejects with inject_disabled when allowWrite() is false, before verifyTarget', async () => {
    const h = makeHarness()
    h.setAllowWrite(false)
    const prep = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message: 'hi' })
    expect(prep).toMatchObject({ ok: false, errorCode: 'inject_disabled' })
    expect(h.verifyCalls).toHaveLength(0)
  })

  it('rejects empty and NUL-bearing messages before verifyTarget', async () => {
    const h = makeHarness()
    const empty = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message: '' })
    expect(empty).toMatchObject({ ok: false, errorCode: 'invalid_message' })
    const nul = await h.gateway.prepare({
      target: TARGET_CLAUDE,
      mode: 'queue',
      message: 'ab\u0000cd',
    })
    expect(nul).toMatchObject({ ok: false, errorCode: 'invalid_message' })
    expect(h.verifyCalls).toHaveLength(0)
  })

  it('applies the 16 KiB cap byte-accurately (limit-1, limit, limit+1)', async () => {
    const h = makeHarness()
    const under = await h.gateway.prepare({
      target: TARGET_CLAUDE,
      mode: 'queue',
      message: 'a'.repeat(MAX_MESSAGE_BYTES - 1),
    })
    expect(under.ok).toBe(true)
    const exact = await h.gateway.prepare({
      target: TARGET_CLAUDE,
      mode: 'queue',
      message: 'a'.repeat(MAX_MESSAGE_BYTES),
    })
    expect(exact.ok).toBe(true)
    const over = await h.gateway.prepare({
      target: TARGET_CLAUDE,
      mode: 'queue',
      message: 'a'.repeat(MAX_MESSAGE_BYTES + 1),
    })
    expect(over).toMatchObject({ ok: false, errorCode: 'invalid_message' })
  })

  it('counts bytes, not chars: multi-byte UTF-8 crosses the limit early', async () => {
    const h = makeHarness()
    // '汉' is 3 UTF-8 bytes: 5462 chars = 16386 bytes > 16384, well under in chars.
    const message = '汉'.repeat(5462)
    expect(message.length).toBeLessThan(MAX_MESSAGE_BYTES)
    const prep = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message })
    expect(prep).toMatchObject({ ok: false, errorCode: 'invalid_message' })
  })

  it('rejects unknown targets (target_not_found) and dead targets (target_dead)', async () => {
    const h = makeHarness()
    h.setVerify(async () => null)
    const missing = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message: 'hi' })
    expect(missing).toMatchObject({ ok: false, errorCode: 'target_not_found' })

    h.setVerify(async (t) => ({ agent: t.agent, sessionId: t.sessionId, status: 'dead' }))
    const dead = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message: 'hi' })
    expect(dead).toMatchObject({ ok: false, errorCode: 'target_dead' })
  })

  it('caps in-flight tokens; consumption and expiry both free capacity', async () => {
    const h = makeHarness()
    const first = await prepared(h, TARGET_CLAUDE, 'm0')
    for (let i = 1; i < MAX_PENDING_TOKENS; i++) {
      await prepared(h, TARGET_CLAUDE, `m${i}`)
    }
    const overflow = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message: 'x' })
    expect(overflow).toMatchObject({ ok: false, errorCode: 'too_many_pending' })

    // Consuming one token frees exactly one slot.
    await h.gateway.execute({ ...first, message: 'm0' })
    const refill = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message: 'y' })
    expect(refill.ok).toBe(true)

    // TTL expiry frees the rest.
    h.advance(TOKEN_TTL_MS)
    const fresh = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message: 'z' })
    expect(fresh.ok).toBe(true)
  })
})

describe('execute token checks (three rejections + anti-swap)', () => {
  it('token_missing: requestId the gateway never issued', async () => {
    const h = makeHarness()
    const result = await h.gateway.execute({
      requestId: 'req-never',
      confirmToken: 'f'.repeat(32),
      message: 'hi',
    })
    expect(result).toEqual({ outcome: 'failed', errorCode: 'token_missing' })
    expect(h.dsh.calls).toHaveLength(0)
    expect(h.sendCli.calls).toHaveLength(0)
  })

  it('token_missing: empty confirmToken, and the issued token survives', async () => {
    const h = makeHarness()
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'hi')
    const missing = await h.gateway.execute({ requestId, confirmToken: '', message: 'hi' })
    expect(missing).toEqual({ outcome: 'failed', errorCode: 'token_missing' })
    // A malformed request is not an attempt: the real token still works.
    const ok = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(ok.outcome).toBe('delivered')
  })

  it('token_expired: past the 60s TTL the executor never fires', async () => {
    const h = makeHarness()
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'hi')
    h.advance(TOKEN_TTL_MS)
    const result = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(result).toEqual({ outcome: 'failed', errorCode: 'token_expired' })
    expect(h.sendCli.calls).toHaveLength(0)
  })

  it('still accepts right below the TTL boundary', async () => {
    const h = makeHarness()
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'hi')
    h.advance(TOKEN_TTL_MS - 1)
    const result = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(result.outcome).toBe('delivered')
  })

  it('token_reused: a failed attempt consumes the token; the retry is refused', async () => {
    const h = makeHarness()
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'original message')
    const swapped = await h.gateway.execute({ requestId, confirmToken, message: 'swapped message' })
    expect(swapped).toEqual({ outcome: 'failed', errorCode: 'token_mismatch' })
    // Consume-on-attempt: even the correct message is now refused.
    const retry = await h.gateway.execute({ requestId, confirmToken, message: 'original message' })
    expect(retry).toEqual({ outcome: 'failed', errorCode: 'token_reused' })
    expect(h.sendCli.calls).toHaveLength(0)
  })

  it('token_reused: after the result cache expires, the requestId can never re-fire', async () => {
    const h = makeHarness()
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'hi')
    const first = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(first.outcome).toBe('delivered')
    h.advance(RESULT_CACHE_TTL_MS)
    const late = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(late).toEqual({ outcome: 'failed', errorCode: 'token_reused' })
    expect(h.sendCli.calls).toHaveLength(1)
  })

  it('token_mismatch: a wrong confirmToken voids the real one too', async () => {
    const h = makeHarness()
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'hi')
    const wrong = await h.gateway.execute({
      requestId,
      confirmToken: '0'.repeat(32),
      message: 'hi',
    })
    expect(wrong).toEqual({ outcome: 'failed', errorCode: 'token_mismatch' })
    const retry = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(retry).toEqual({ outcome: 'failed', errorCode: 'token_reused' })
    expect(h.sendCli.calls).toHaveLength(0)
  })

  it('token_mismatch: message swapped after prepare is refused (anti-swap)', async () => {
    const h = makeHarness()
    const { requestId, confirmToken } = await prepared(h, TARGET_DSH, 'the confirmed body')
    const result = await h.gateway.execute({
      requestId,
      confirmToken,
      message: 'a different body',
    })
    expect(result).toEqual({ outcome: 'failed', errorCode: 'token_mismatch' })
    expect(h.dsh.calls).toHaveLength(0)
  })
})

describe('dispatch', () => {
  it('routes agent=dsh to the dsh executor with the full execution request', async () => {
    const h = makeHarness()
    const { requestId, confirmToken } = await prepared(h, TARGET_DSH, 'hello dsh', 'steer')
    const result = await h.gateway.execute({ requestId, confirmToken, message: 'hello dsh' })
    expect(result.outcome).toBe('delivered')
    expect(h.sendCli.calls).toHaveLength(0)
    expect(h.dsh.calls).toEqual([
      { target: TARGET_DSH, mode: 'steer', message: 'hello dsh', requestId: 'req-1' },
    ])
  })

  it('routes claude/codex/cursor-cli to the send-cli executor', async () => {
    const h = makeHarness()
    for (const agent of ['claude', 'codex', 'cursor-cli']) {
      const target: InjectTarget = { agent, sessionId: `sess-${agent}` }
      const { requestId, confirmToken } = await prepared(h, target, `for ${agent}`)
      const result = await h.gateway.execute({ requestId, confirmToken, message: `for ${agent}` })
      expect(result.outcome).toBe('delivered')
    }
    expect(h.sendCli.calls.map((c) => c.target.agent)).toEqual([
      'claude',
      'codex',
      'cursor-cli',
    ])
    expect(h.dsh.calls).toHaveLength(0)
  })

  it('rejects non-injectable agents at PREPARE: no token, no capacity, no executor (F-6)', async () => {
    const h = makeHarness()
    // verifyTarget answers for any target here, so the rejection below can
    // only come from the whitelist check, not from target_not_found.
    for (const agent of ['copilot', 'kimi', 'cursor-ide']) {
      const prep = await h.gateway.prepare({
        target: { agent, sessionId: `sess-${agent}` },
        mode: 'queue',
        message: 'hi',
      })
      expect(prep).toMatchObject({ ok: false, errorCode: 'unsupported_agent' })
    }
    expect(h.dsh.calls).toHaveLength(0)
    expect(h.sendCli.calls).toHaveLength(0)
    // No requestId was ever issued (all three prepares logged as rejected)…
    expect(h.logs.every((entry) => entry.requestId === null)).toBe(true)
    // …and no in-flight capacity was consumed: a full whitelist round still fits.
    for (let i = 0; i < MAX_PENDING_TOKENS; i++) {
      await prepared(h, TARGET_CLAUDE, `m${i}`)
    }
  })

  it('keeps target_not_found ahead of the whitelist for unknown targets', async () => {
    const h = makeHarness()
    h.setVerify(async () => null)
    const prep = await h.gateway.prepare({
      target: { agent: 'copilot', sessionId: 'sess-x' },
      mode: 'queue',
      message: 'hi',
    })
    expect(prep).toMatchObject({ ok: false, errorCode: 'target_not_found' })
  })
})

describe('idempotency', () => {
  it('delivered: a second execute replays the cached result without re-firing', async () => {
    const h = makeHarness()
    h.sendCli.setResult({ outcome: 'delivered', detail: 'send ok' })
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'hi')
    const first = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(first).toEqual({ outcome: 'delivered', detail: 'send ok' })
    const second = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(second).toEqual({ outcome: 'delivered', detail: 'send ok', replayed: true })
    expect(h.sendCli.calls).toHaveLength(1)
  })

  it('unknown is terminal: the replay serves the cache and never re-fires', async () => {
    const h = makeHarness()
    h.sendCli.setResult({ outcome: 'unknown', errorCode: 'send_timeout' })
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'hi')
    const first = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(first).toEqual({ outcome: 'unknown', errorCode: 'send_timeout' })
    const second = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(second).toEqual({ outcome: 'unknown', errorCode: 'send_timeout', replayed: true })
    expect(h.sendCli.calls).toHaveLength(1)
  })

  it('an executor throw maps to executor_error and is cached like any result', async () => {
    const h = makeHarness()
    h.dsh.setError('boom')
    const { requestId, confirmToken } = await prepared(h, TARGET_DSH, 'hi')
    const first = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(first).toEqual({ outcome: 'failed', errorCode: 'executor_error', detail: 'boom' })
    const second = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(second).toEqual({
      outcome: 'failed',
      errorCode: 'executor_error',
      detail: 'boom',
      replayed: true,
    })
    expect(h.dsh.calls).toHaveLength(1)
  })

  it('a replay with a swapped message is refused (anti-swap on the cache path)', async () => {
    const h = makeHarness()
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'hi')
    const first = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(first.outcome).toBe('delivered')
    const swapped = await h.gateway.execute({ requestId, confirmToken, message: 'other' })
    expect(swapped).toEqual({ outcome: 'failed', errorCode: 'token_mismatch' })
    expect(h.sendCli.calls).toHaveLength(1)
  })

  it('fails closed when the cache outlives the pending record (F-5 window)', async () => {
    // The "cache alive, pending pruned" window IS reachable: the cache
    // expiry stamp is taken after the executor awaited, so an executor
    // running past the token's nominal expiry stretches the cache past the
    // pending prune time (issuedAt + TOKEN_TTL_MS + RESULT_CACHE_TTL_MS).
    const h = makeHarness()
    const inner = h.sendCli.executor.execute.bind(h.sendCli.executor)
    // Send-cli worst case: ~35s of wall clock inside one execute.
    h.sendCli.executor.execute = async (req) => {
      const result = await inner(req)
      h.advance(35_000)
      return result
    }

    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'hi') // issued at T0
    h.advance(TOKEN_TTL_MS - 1_000) // dispatch starts just inside the TTL
    const first = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(first.outcome).toBe('delivered')
    // Clock now T0+94s; cache lives until T0+394s, pending until T0+360s.

    // Inside the window, even the original caller with the correct token is
    // refused: the binding facts are gone, so the replay is unverifiable —
    // never serve a cached result on a bare requestId.
    h.advance(271_000) // T0+365s: pending pruned, cache still alive
    const bare = await h.gateway.execute({ requestId, confirmToken: 'f'.repeat(32), message: 'hi' })
    expect(bare).toEqual({ outcome: 'failed', errorCode: 'token_missing' })
    const honest = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(honest).toEqual({ outcome: 'failed', errorCode: 'token_missing' })
    expect(h.sendCli.calls).toHaveLength(1) // and nothing ever re-fired
  })
})

describe('logging', () => {
  it('logs exactly one body-free entry per prepare and per execute', async () => {
    const h = makeHarness()
    const message = 'audit me'
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, message)
    await h.gateway.execute({ requestId, confirmToken, message })
    expect(h.logs).toHaveLength(2)

    const [prepareEntry, executeEntry] = h.logs
    expect(prepareEntry).toMatchObject({
      phase: 'prepare',
      requestId: 'req-1',
      target: TARGET_CLAUDE,
      mode: 'queue',
      ok: true,
      messageBytes: Buffer.byteLength(message, 'utf8'),
      messageSha12: sha12(message),
    })
    expect(executeEntry).toMatchObject({
      phase: 'execute',
      requestId: 'req-1',
      target: TARGET_CLAUDE,
      mode: 'queue',
      ok: true,
      outcome: 'delivered',
      messageBytes: Buffer.byteLength(message, 'utf8'),
      messageSha12: sha12(message),
    })
    expect(Object.isFrozen(prepareEntry)).toBe(true)
  })

  it('rejected prepares/executes are logged with their error codes', async () => {
    const h = makeHarness()
    h.setAllowWrite(false)
    await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message: 'hi' })
    h.setAllowWrite(true)
    await h.gateway.execute({ requestId: 'req-none', confirmToken: 'x'.repeat(32), message: 'hi' })
    expect(h.logs).toHaveLength(2)
    expect(h.logs[0]).toMatchObject({
      phase: 'prepare',
      requestId: null,
      ok: false,
      errorCode: 'inject_disabled',
    })
    expect(h.logs[1]).toMatchObject({
      phase: 'execute',
      requestId: 'req-none',
      target: null,
      mode: null,
      ok: false,
      outcome: 'failed',
      errorCode: 'token_missing',
    })
  })

  it('never contains the message body or its head plaintext anywhere', async () => {
    const h = makeHarness()
    const secret = `TOPSECRET-injection-payload ${'z'.repeat(300)}`
    const head = secret.slice(0, 120)

    // A full lifecycle plus a rejected execute, all with the secret body.
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, secret)
    await h.gateway.execute({ requestId, confirmToken, message: secret })
    await h.gateway.execute({ requestId: 'req-x', confirmToken: '', message: secret })

    expect(h.logs.length).toBeGreaterThanOrEqual(3)
    const dumps = [JSON.stringify(h.logs), JSON.stringify(h.gateway.getRecentLog(100))]
    for (const dump of dumps) {
      expect(dump).not.toContain('TOPSECRET')
      expect(dump).not.toContain(head)
      expect(dump).not.toContain(secret)
      // The digest fields ARE there — the fingerprint without the body.
      expect(dump).toContain(sha12(secret))
      expect(dump).toContain(String(Buffer.byteLength(secret, 'utf8')))
    }
  })

  it('getRecentLog returns newest first and honors the limit', async () => {
    const h = makeHarness()
    const a = await prepared(h, TARGET_CLAUDE, 'first')
    await h.gateway.execute({ ...a, message: 'first' })
    await prepared(h, TARGET_DSH, 'second')

    const all = h.gateway.getRecentLog()
    expect(all.map((e) => [e.phase, e.requestId])).toEqual([
      ['prepare', 'req-2'],
      ['execute', 'req-1'],
      ['prepare', 'req-1'],
    ])
    const limited = h.gateway.getRecentLog(2)
    expect(limited.map((e) => e.requestId)).toEqual(['req-2', 'req-1'])
    expect(limited[0]?.phase).toBe('prepare')
    expect(h.gateway.getRecentLog(0)).toEqual([])
  })
})
