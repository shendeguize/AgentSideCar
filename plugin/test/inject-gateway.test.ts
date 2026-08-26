/**
 * Unit tests for the InjectGateway (design §4.d / §4.f.5 / §8).
 * Fake executors, fake clock, fake requestId source; no cordis/dsh, no I/O.
 */

import { describe, expect, it } from 'vitest'
import { createHash } from 'node:crypto'
import type { SessionRow } from '../src/bridge.ts'
import {
  deriveInjectEligibility,
  MAX_SESSION_EXTRA_BYTES,
  MAX_SESSION_EXTRA_DEPTH,
  MAX_SESSION_EXTRA_ITEMS,
} from '../src/inject-eligibility.ts'
import {
  InjectGateway,
  MAX_MESSAGE_BYTES,
  MAX_PENDING_TOKENS,
  RESULT_CACHE_TTL_MS,
  TOKEN_TTL_MS,
  type InjectExecutionRequest,
  type InjectExecutor,
  type InjectLogEntry,
  type InjectPreflightResult,
  type InjectResult,
  type InjectTarget,
  type TargetStatus,
} from '../src/inject-gateway.ts'

const T0 = 1_700_000_000_000

const TARGET_DSH: InjectTarget = { agent: 'dsh', sessionId: 'sess-dsh-1' }
const TARGET_CLAUDE: InjectTarget = { agent: 'claude', sessionId: 'sess-claude-1' }
const TARGET_KIMI: InjectTarget = { agent: 'kimi', sessionId: 'sess-kimi-1' }

function sessionRow(overrides: Partial<SessionRow> = {}): SessionRow {
  return {
    agent: 'claude',
    session_id: 'session-1',
    project: '/local/project',
    transcript: '/local/project/session.jsonl',
    updated_at: 1,
    title: 'session',
    status: 'waiting',
    extra: {},
    parent_id: null,
    ...overrides,
  }
}

interface FakeExecutor {
  executor: InjectExecutor
  preflightCalls: InjectTarget[]
  calls: InjectExecutionRequest[]
  setPreflight(result: InjectPreflightResult): void
  setResult(result: InjectResult): void
  setError(message: string): void
}

function fakeExecutor(
  kind: 'dsh' | 'send-cli',
  withPreflight = false,
): FakeExecutor {
  const preflightCalls: InjectTarget[] = []
  const calls: InjectExecutionRequest[] = []
  let preflightResult: InjectPreflightResult = { ok: true }
  let result: InjectResult = { outcome: 'delivered' }
  let error: string | null = null
  const executor: InjectExecutor = {
    kind,
    async execute(req) {
      calls.push(req)
      if (error !== null) throw new Error(error)
      return { ...result }
    },
  }
  if (withPreflight) {
    executor.preflight = async (target) => {
      preflightCalls.push(target)
      return { ...preflightResult }
    }
  }
  return {
    executor,
    preflightCalls,
    calls,
    setPreflight(next) {
      preflightResult = next
    },
    setResult(next) {
      result = next
      error = null
    },
    setError(message) {
      error = message
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
    inject_eligibility: { allowed: true, reason: 'eligible' },
  })
  const logs: InjectLogEntry[] = []
  const verifyCalls: InjectTarget[] = []
  const dsh = fakeExecutor('dsh', true)
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

describe('deriveInjectEligibility', () => {
  it.each(['cursor-ide', 'copilot', 'unknown'])(
    'rejects unsupported agent %s',
    (agent) => {
      expect(deriveInjectEligibility(sessionRow({ agent }))).toEqual({
        allowed: false,
        reason: 'unsupported_agent',
      })
    },
  )

  it.each(['claude', 'codex', 'cursor-cli', 'kimi'])(
    'allows local top-level waiting/idle external agent %s',
    (agent) => {
      for (const status of ['waiting', 'idle']) {
        expect(deriveInjectEligibility(sessionRow({ agent, status }))).toEqual({
          allowed: true,
          reason: 'eligible',
        })
      }
    },
  )

  it.each([
    ['working', { status: 'working' }, 'working_session'],
    ['dead', { status: 'dead' }, 'dead_session'],
    ['unknown status', { status: 'paused' }, 'invalid_session'],
    ['child', { parent_id: 'parent-secret' }, 'child_session'],
    ['sidechain', { extra: { sidechain: true } }, 'child_session'],
    ['remote flag', { extra: { remote: true } }, 'remote_session'],
    ['remote source', { extra: { source: 'remote' } }, 'remote_session'],
    ['remote host', { extra: { host: 'private-edge' } }, 'remote_session'],
  ] as const)('rejects external %s with a stable reason', (_name, overrides, reason) => {
    expect(deriveInjectEligibility(sessionRow(overrides))).toEqual({
      allowed: false,
      reason,
    })
  })

  it.each([
    ['working', { status: 'working' }, 'working_session'],
    ['dead', { status: 'dead' }, 'dead_session'],
    ['child', { parent_id: 'parent-secret' }, 'child_session'],
    ['sidechain', { extra: { sidechain: true } }, 'child_session'],
    ['remote', { extra: { source: 'remote' } }, 'remote_session'],
    ['invalid', { status: 'paused' }, 'invalid_session'],
  ] as const)('fails Kimi %s closed', (_name, overrides, reason) => {
    expect(deriveInjectEligibility(sessionRow({ agent: 'kimi', ...overrides }))).toEqual({
      allowed: false,
      reason,
    })
  })

  it('keeps the existing local default for absent or non-matching remote markers', () => {
    for (const extra of [{}, { remote: false }, { remote: 'unknown', source: 'cli' }]) {
      expect(deriveInjectEligibility(sessionRow({ extra }))).toEqual({
        allowed: true,
        reason: 'eligible',
      })
    }
  })

  it.each([
    ['top-level host', { host: 'edge-a' }],
    ['top-level remote', { remote: true }],
    ['top-level source', { source: 'remote' }],
    ['top-level alias', { remote_alias: 'edge-a' }],
    ['top-level remote host', { remote_host: 'edge-a' }],
    ['extra alias', { extra: { remote_alias: 'edge-a' } }],
    ['extra remote host', { extra: { remote_host: 'edge-a' } }],
  ] as const)('rejects authoritative %s provenance', (_name, overrides) => {
    expect(deriveInjectEligibility(sessionRow(overrides))).toEqual({
      allowed: false,
      reason: 'remote_session',
    })
  })

  it('keeps explicit remote provenance authoritative over local state/topology', () => {
    expect(
      deriveInjectEligibility(
        sessionRow({
          status: 'working',
          parent_id: 'parent',
          host: 'edge-a',
        }),
      ),
    ).toEqual({ allowed: false, reason: 'remote_session' })
  })

  it('does not infer remote from an absent host or host=local', () => {
    expect(deriveInjectEligibility(sessionRow())).toEqual({
      allowed: true,
      reason: 'eligible',
    })
    expect(deriveInjectEligibility(sessionRow({ host: 'local' }))).toEqual({
      allowed: true,
      reason: 'eligible',
    })
  })

  it.each([
    ['non-string status', { status: 7 }],
    ['array extra', { extra: [] }],
    ['non-string parent', { parent_id: { id: 'parent' } }],
    ['malformed top host', { host: null }],
    ['malformed remote alias', { remote_alias: 7 }],
  ] as const)('fails closed for malformed %s', (_name, overrides) => {
    expect(
      deriveInjectEligibility({
        ...sessionRow(),
        ...overrides,
      } as unknown as SessionRow),
    ).toEqual({ allowed: false, reason: 'invalid_session' })
  })

  it('rejects accessor and custom-prototype extra without invoking getters', () => {
    let getterReads = 0
    const accessorExtra: Record<string, unknown> = {}
    Object.defineProperty(accessorExtra, 'remote', {
      enumerable: true,
      get() {
        getterReads += 1
        return true
      },
    })
    const inheritedExtra = Object.assign(Object.create({ remote: false }), { safe: true })

    expect(deriveInjectEligibility(sessionRow({ extra: accessorExtra }))).toEqual({
      allowed: false,
      reason: 'invalid_session',
    })
    expect(deriveInjectEligibility(sessionRow({ extra: inheritedExtra }))).toEqual({
      allowed: false,
      reason: 'invalid_session',
    })
    expect(getterReads).toBe(0)
  })

  it('enforces sidecar depth, item, and aggregate bounds for extra', () => {
    const nested = (depth: number): Record<string, unknown> => {
      const root: Record<string, unknown> = {}
      let cursor = root
      for (let level = 1; level < depth; level += 1) {
        const child: Record<string, unknown> = {}
        cursor['next'] = child
        cursor = child
      }
      return root
    }
    const itemObject = (items: number): Record<string, unknown> =>
      Object.fromEntries(Array.from({ length: items }, (_, index) => [`k${index}`, null]))

    expect(
      deriveInjectEligibility(sessionRow({ extra: nested(MAX_SESSION_EXTRA_DEPTH) })),
    ).toEqual({ allowed: true, reason: 'eligible' })
    expect(
      deriveInjectEligibility(sessionRow({ extra: nested(MAX_SESSION_EXTRA_DEPTH + 1) })),
    ).toEqual({ allowed: false, reason: 'invalid_session' })
    expect(
      deriveInjectEligibility(
        sessionRow({ extra: itemObject(MAX_SESSION_EXTRA_ITEMS - 1) }),
      ),
    ).toEqual({ allowed: true, reason: 'eligible' })
    expect(
      deriveInjectEligibility(sessionRow({ extra: itemObject(MAX_SESSION_EXTRA_ITEMS) })),
    ).toEqual({ allowed: false, reason: 'invalid_session' })
    expect(
      deriveInjectEligibility(
        sessionRow({ extra: { value: 'x'.repeat(MAX_SESSION_EXTRA_BYTES - 32) } }),
      ),
    ).toEqual({ allowed: true, reason: 'eligible' })
    expect(
      deriveInjectEligibility(
        sessionRow({ extra: { value: 'x'.repeat(MAX_SESSION_EXTRA_BYTES) } }),
      ),
    ).toEqual({ allowed: false, reason: 'invalid_session' })
  })

  it.each(['working', 'waiting', 'idle'])(
    'allows local dsh %s, including child/sidechain topology',
    (status) => {
      expect(
        deriveInjectEligibility(
          sessionRow({
            agent: 'dsh',
            status,
            parent_id: 'dsh-parent',
            extra: { sidechain: true },
          }),
        ),
      ).toEqual({ allowed: true, reason: 'eligible' })
    },
  )

  it.each([
    ['dead', { status: 'dead' }, 'dead_session'],
    ['remote', { extra: { source: 'remote' } }, 'remote_session'],
    ['invalid status', { status: 'paused' }, 'invalid_session'],
  ] as const)('rejects dsh %s', (_name, overrides, reason) => {
    expect(
      deriveInjectEligibility(sessionRow({ agent: 'dsh', ...overrides })),
    ).toEqual({ allowed: false, reason })
  })

  it('returns only static verdict vocabulary, never sensitive marker values', () => {
    const secret = 'PRIVATE-HOST-parent-token'
    const verdict = deriveInjectEligibility(
      sessionRow({ extra: { host: secret }, parent_id: secret }),
    )
    expect(verdict).toEqual({ allowed: false, reason: 'remote_session' })
    expect(JSON.stringify(verdict)).not.toContain(secret)
  })
})

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
      inject_eligibility: { allowed: true, reason: 'eligible' },
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

  it('rejects unknown and dead targets with stable eligibility reasons', async () => {
    const h = makeHarness()
    h.setVerify(async () => null)
    const missing = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message: 'hi' })
    expect(missing).toMatchObject({ ok: false, errorCode: 'target_not_found' })

    h.setVerify(async (t) => ({
      agent: t.agent,
      sessionId: t.sessionId,
      status: 'dead',
      inject_eligibility: { allowed: false, reason: 'dead_session' },
    }))
    const dead = await h.gateway.prepare({ target: TARGET_CLAUDE, mode: 'queue', message: 'hi' })
    expect(dead).toMatchObject({ ok: false, errorCode: 'dead_session' })
  })

  it('fails closed on an eligibility rejection before preflight or token issuance', async () => {
    const h = makeHarness()
    h.setVerify(async (target) => ({
      agent: target.agent,
      sessionId: target.sessionId,
      status: 'working',
      inject_eligibility: { allowed: false, reason: 'working_session' },
    }))

    const rejected = await h.gateway.prepare({
      target: TARGET_CLAUDE,
      mode: 'queue',
      message: 'hi',
    })
    expect(rejected).toEqual({ ok: false, errorCode: 'working_session' })
    expect(h.dsh.preflightCalls).toHaveLength(0)
    expect(h.logs.at(-1)).toMatchObject({
      phase: 'prepare',
      requestId: null,
      errorCode: 'working_session',
    })
  })

  it('issues no token and dispatches nothing for a working Kimi target', async () => {
    const h = makeHarness()
    h.setVerify(async (target) => ({
      agent: target.agent,
      sessionId: target.sessionId,
      status: 'working',
      inject_eligibility: { allowed: false, reason: 'working_session' },
    }))

    expect(await h.gateway.prepare({
      target: TARGET_KIMI,
      mode: 'queue',
      message: 'do not dispatch',
    })).toEqual({ ok: false, errorCode: 'working_session' })
    expect(h.logs.at(-1)).toMatchObject({
      phase: 'prepare',
      requestId: null,
      target: TARGET_KIMI,
      errorCode: 'working_session',
    })
    expect(h.sendCli.calls).toHaveLength(0)
    expect(h.dsh.calls).toHaveLength(0)
  })

  it('runs dsh preflight before issuance and rejects modelless cold targets without a token', async () => {
    const h = makeHarness()
    h.dsh.setPreflight({
      ok: false,
      errorCode: 'dsh_model_unconfigured',
    })

    const rejected = await h.gateway.prepare({
      target: TARGET_DSH,
      mode: 'queue',
      message: 'hi',
    })
    expect(rejected).toEqual({
      ok: false,
      errorCode: 'dsh_model_unconfigured',
    })
    expect(h.dsh.preflightCalls).toEqual([TARGET_DSH])
    expect(h.dsh.calls).toHaveLength(0)
    expect(h.logs.at(-1)).toMatchObject({
      phase: 'prepare',
      requestId: null,
      target: null,
      mode: null,
      errorCode: 'dsh_model_unconfigured',
    })

    // Rejection consumed neither an id nor token capacity.
    const accepted = await h.gateway.prepare({
      target: TARGET_CLAUDE,
      mode: 'queue',
      message: 'still available',
    })
    expect(accepted).toMatchObject({ ok: true, requestId: 'req-1' })
  })

  it('leaves send-cli agent prepare behavior unchanged without a preflight hook', async () => {
    const h = makeHarness()
    expect(h.sendCli.executor.preflight).toBeUndefined()

    for (const agent of ['claude', 'codex', 'cursor-cli', 'kimi']) {
      const prep = await h.gateway.prepare({
        target: { agent, sessionId: `sess-${agent}` },
        mode: 'queue',
        message: `for ${agent}`,
      })
      expect(prep.ok).toBe(true)
    }
    expect(h.dsh.preflightCalls).toHaveLength(0)
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

  it('revalidates the same eligibility verdict at execute and blocks stale targets', async () => {
    const h = makeHarness()
    const { requestId, confirmToken } = await prepared(h, TARGET_CLAUDE, 'hi')
    h.setVerify(async (target) => ({
      agent: target.agent,
      sessionId: target.sessionId,
      status: 'waiting',
      inject_eligibility: { allowed: false, reason: 'remote_session' },
    }))

    const result = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(result).toEqual({ outcome: 'failed', errorCode: 'remote_session' })
    expect(h.verifyCalls).toEqual([TARGET_CLAUDE, TARGET_CLAUDE])
    expect(h.sendCli.calls).toHaveLength(0)
  })
})

describe('dispatch', () => {
  it('routes agent=dsh to the dsh executor with the full execution request', async () => {
    const h = makeHarness()
    h.setVerify(async (target) => ({
      agent: target.agent,
      sessionId: target.sessionId,
      status: 'working',
      inject_eligibility: { allowed: true, reason: 'eligible' },
    }))
    const { requestId, confirmToken } = await prepared(h, TARGET_DSH, 'hello dsh', 'steer')
    const result = await h.gateway.execute({ requestId, confirmToken, message: 'hello dsh' })
    expect(result.outcome).toBe('delivered')
    expect(h.sendCli.calls).toHaveLength(0)
    expect(h.dsh.calls).toEqual([
      { target: TARGET_DSH, mode: 'steer', message: 'hello dsh', requestId: 'req-1' },
    ])
  })

  it('routes external spawn-resume agents, including Kimi, to send-cli', async () => {
    const h = makeHarness()
    for (const agent of ['claude', 'codex', 'cursor-cli', 'kimi']) {
      const target: InjectTarget = { agent, sessionId: `sess-${agent}` }
      const { requestId, confirmToken } = await prepared(h, target, `for ${agent}`)
      const result = await h.gateway.execute({ requestId, confirmToken, message: `for ${agent}` })
      expect(result.outcome).toBe('delivered')
    }
    expect(h.sendCli.calls.map((c) => c.target.agent)).toEqual([
      'claude',
      'codex',
      'cursor-cli',
      'kimi',
    ])
    expect(h.dsh.calls).toHaveLength(0)
  })

  it('rejects non-injectable agents at PREPARE: no token, no capacity, no executor (F-6)', async () => {
    const h = makeHarness()
    // verifyTarget answers for any target here, so the rejection below can
    // only come from the whitelist check, not from target_not_found.
    for (const agent of ['copilot', 'cursor-ide']) {
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

  it('keeps Kimi completed-but-unknown terminal and does not dispatch its replay', async () => {
    const h = makeHarness()
    h.sendCli.setResult({ outcome: 'unknown' })
    const { requestId, confirmToken } = await prepared(h, TARGET_KIMI, 'kimi resume')

    expect(await h.gateway.execute({
      requestId,
      confirmToken,
      message: 'kimi resume',
    })).toEqual({ outcome: 'unknown' })
    expect(await h.gateway.execute({
      requestId,
      confirmToken,
      message: 'kimi resume',
    })).toEqual({ outcome: 'unknown', replayed: true })
    expect(h.sendCli.calls).toEqual([
      {
        target: TARGET_KIMI,
        mode: 'queue',
        message: 'kimi resume',
        requestId,
      },
    ])
    expect(h.dsh.calls).toHaveLength(0)
  })

  it('sanitizes an executor throw, caches it, and redacts failed dsh identities', async () => {
    const h = makeHarness()
    const secret = '/SECRET/session-id/preset/model'
    h.dsh.setError(secret)
    const { requestId, confirmToken } = await prepared(h, TARGET_DSH, 'hi')
    const first = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(first).toEqual({ outcome: 'failed', errorCode: 'executor_error' })
    const second = await h.gateway.execute({ requestId, confirmToken, message: 'hi' })
    expect(second).toEqual({
      outcome: 'failed',
      errorCode: 'executor_error',
      replayed: true,
    })
    expect(h.dsh.calls).toHaveLength(1)
    expect(JSON.stringify([first, second, ...h.logs])).not.toContain(secret)
    expect(h.logs.slice(-2)).toEqual([
      expect.objectContaining({
        phase: 'execute',
        requestId: null,
        target: null,
        mode: null,
      }),
      expect.objectContaining({
        phase: 'execute',
        requestId: null,
        target: null,
        mode: null,
      }),
    ])
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
