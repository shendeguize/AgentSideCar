/**
 * Unit tests for the automatic check of an unknown injection outcome
 * (client/inject/verify.ts + the glue probe): what counts as evidence that
 * the message landed, the bounded retry loop, the difference between "the
 * transcript says no" and "no transcript answered", and the honesty of the
 * copy the panel shows for each verdict. Node environment, injected probe
 * and sleep throughout — nothing here touches a transport or a clock.
 */

import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/client/api.ts'
import {
  createVerifyProbe,
  VERIFY_PAGE_LIMIT,
  type FetchTimelinePageFn,
} from '../src/client/inject-glue.ts'
import {
  injectedHead,
  matchesInjectedMessage,
  shouldVerifyDelivery,
  verifyCopyKey,
  verifyInjection,
  VERIFY_ATTEMPTS,
  VERIFY_SKEW_MS,
  type VerifyEntry,
} from '../src/client/inject/verify.ts'
import type { InjectOutcome, InjectPlanView, PanelState } from '../src/client/inject/logic.ts'
import { en } from '../src/client/locales/en.ts'
import { zh } from '../src/client/locales/zh.ts'

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

const NOW = 1_700_000_000_000

function entry(overrides: Partial<VerifyEntry> = {}): VerifyEntry {
  return { kind: 'user', text: 'restart the build please', ts: NOW, ...overrides }
}

function plan(head: string): InjectPlanView {
  return {
    target: { agent: 'claude', sessionId: 'sess-1' },
    mode: 'queue',
    targetStatus: { agent: 'claude', sessionId: 'sess-1', status: 'idle' },
    messagePreview: { bytes: head.length, head },
  }
}

function resultState(outcome: InjectOutcome, head = 'restart the build please'): PanelState {
  return { phase: 'result', result: { outcome }, plan: plan(head) }
}

/** A probe that answers the given pages in order, then repeats the last. */
function scriptedProbe(pages: readonly (readonly VerifyEntry[] | null)[]): {
  probe: () => Promise<readonly VerifyEntry[] | null>
  calls: () => number
} {
  let call = 0
  return {
    probe: () => {
      const page = pages[Math.min(call, pages.length - 1)] ?? null
      call += 1
      return Promise.resolve(page)
    },
    calls: () => call,
  }
}

/** Sleep seam that resolves instantly and records the requested waits. */
function fakeSleep(): { sleep: (ms: number) => Promise<void>; waits: number[] } {
  const waits: number[] = []
  return {
    sleep: (ms) => {
      waits.push(ms)
      return Promise.resolve()
    },
    waits,
  }
}

// ---------------------------------------------------------------------------
// What counts as evidence.
// ---------------------------------------------------------------------------

describe('matchesInjectedMessage', () => {
  it('accepts a user turn that contains the previewed head', () => {
    const entries = [entry({ text: 'restart the build please, then run the tests' })]
    expect(matchesInjectedMessage(entries, 'restart the build please')).toBe(true)
  })

  it('accepts a transcript that truncated the message shorter than the head', () => {
    expect(matchesInjectedMessage([entry({ text: 'restart the build' })], 'restart the build please'))
      .toBe(true)
  })

  it('matches across re-wrapped whitespace', () => {
    const entries = [entry({ text: 'restart   the\n  build\tplease' })]
    expect(matchesInjectedMessage(entries, 'restart the build please')).toBe(true)
  })

  it.each(['prompt', 'input', 'user_message', 'USER'])(
    'treats %s as a user turn',
    (kind) => {
      expect(matchesInjectedMessage([entry({ kind })], 'restart the build please')).toBe(true)
    },
  )

  it('refuses assistant output quoting the prompt back', () => {
    const entries = [entry({ kind: 'assistant', text: 'sure — restart the build please' })]
    expect(matchesInjectedMessage(entries, 'restart the build please')).toBe(false)
  })

  it('refuses an empty transcript, an empty entry, and a blank head', () => {
    expect(matchesInjectedMessage([], 'restart the build please')).toBe(false)
    expect(matchesInjectedMessage([entry({ text: '   ' })], 'restart the build please')).toBe(false)
    // A blank needle is an unusable probe, never a wildcard that confirms
    // whatever happens to be in the transcript.
    expect(matchesInjectedMessage([entry()], '   ')).toBe(false)
  })

  it('ignores an identical message that predates the injection', () => {
    const entries = [entry({ ts: NOW - 60_000 })]
    expect(matchesInjectedMessage(entries, 'restart the build please', NOW)).toBe(false)
    expect(matchesInjectedMessage(entries, 'restart the build please', NOW - 120_000)).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// The bounded loop.
// ---------------------------------------------------------------------------

describe('verifyInjection', () => {
  it('confirms on the first page without waiting', async () => {
    const { probe, calls } = scriptedProbe([[entry()]])
    const { sleep, waits } = fakeSleep()
    await expect(verifyInjection({
      probe,
      head: 'restart the build please',
      sleep,
    })).resolves.toBe('confirmed')
    expect(calls()).toBe(1)
    expect(waits).toEqual([])
  })

  it('retries a queued injection that reaches the transcript late', async () => {
    const { probe, calls } = scriptedProbe([[], [], [entry()]])
    const { sleep, waits } = fakeSleep()
    await expect(verifyInjection({
      probe,
      head: 'restart the build please',
      sleep,
      delayMs: 1500,
    })).resolves.toBe('confirmed')
    expect(calls()).toBe(3)
    expect(waits).toEqual([1500, 1500])
  })

  it('reports absent after a bounded number of answered attempts', async () => {
    const { probe, calls } = scriptedProbe([[entry({ kind: 'assistant' })]])
    const { sleep } = fakeSleep()
    await expect(verifyInjection({
      probe,
      head: 'restart the build please',
      sleep,
    })).resolves.toBe('absent')
    // Bounded: the loop can never sit on an unreachable session forever.
    expect(calls()).toBe(VERIFY_ATTEMPTS)
  })

  it('reports unavailable when nothing ever answered, however it failed', async () => {
    const rejecting = vi.fn(() => Promise.reject(
      new ApiError('timeout', 'request_timeout', null),
    ))
    const { sleep } = fakeSleep()
    await expect(verifyInjection({
      probe: rejecting,
      head: 'restart the build please',
      sleep,
    })).resolves.toBe('unavailable')
    expect(rejecting).toHaveBeenCalledTimes(VERIFY_ATTEMPTS)

    const { probe } = scriptedProbe([null])
    await expect(verifyInjection({ probe, head: 'restart the build please', sleep }))
      .resolves.toBe('unavailable')
  })

  it('reports absent when one attempt answered even if others could not', async () => {
    const { probe } = scriptedProbe([null, [entry({ kind: 'assistant' })], null])
    const { sleep } = fakeSleep()
    await expect(verifyInjection({ probe, head: 'restart the build please', sleep }))
      .resolves.toBe('absent')
  })

  it('abandons the run on cancellation without probing again', async () => {
    let cancelled = false
    const probe = vi.fn(() => {
      cancelled = true
      return Promise.resolve([] as readonly VerifyEntry[])
    })
    const { sleep } = fakeSleep()
    await expect(verifyInjection({
      probe,
      head: 'restart the build please',
      sleep,
      cancelled: () => cancelled,
    })).resolves.toBe('unavailable')
    expect(probe).toHaveBeenCalledTimes(1)
  })
})

// ---------------------------------------------------------------------------
// When the panel runs it, and what it says.
// ---------------------------------------------------------------------------

describe('panel gating and copy', () => {
  it('checks only a terminal unknown outcome, and only with a probe', () => {
    expect(shouldVerifyDelivery(resultState('unknown'), true)).toBe(true)
    expect(shouldVerifyDelivery(resultState('unknown'), false)).toBe(false)
    expect(shouldVerifyDelivery(resultState('delivered'), true)).toBe(false)
    expect(shouldVerifyDelivery(resultState('failed'), true)).toBe(false)
    expect(shouldVerifyDelivery({ phase: 'idle', notice: null }, true)).toBe(false)
    // No needle, no check: a blank head could only produce a false verdict.
    expect(shouldVerifyDelivery(resultState('unknown', '  '), true)).toBe(false)
    expect(shouldVerifyDelivery(
      { phase: 'result', result: { outcome: 'unknown' }, plan: null },
      true,
    )).toBe(false)
  })

  it('reads the needle from the previewed head only in the result phase', () => {
    expect(injectedHead(resultState('unknown', 'restart'))).toBe('restart')
    expect(injectedHead({ phase: 'idle', notice: null })).toBe('')
  })

  it('maps each phase to copy, and says nothing when off', () => {
    expect(verifyCopyKey({ phase: 'off' })).toBeNull()
    expect(verifyCopyKey({ phase: 'running' })).toBe('inject.verifying')
    expect(verifyCopyKey({ phase: 'done', outcome: 'confirmed' }))
      .toBe('inject.verifyConfirmed')
    expect(verifyCopyKey({ phase: 'done', outcome: 'absent' })).toBe('inject.verifyAbsent')
    expect(verifyCopyKey({ phase: 'done', outcome: 'unavailable' }))
      .toBe('inject.verifyUnavailable')
  })

  it('keeps the no-retry rule in every verdict, including a confirmation', () => {
    expect(en['inject.verifyConfirmed']).toMatch(/do not send it again/i)
    expect(zh['inject.verifyConfirmed']).toContain('不要重复发送')
    // An absent verdict is the one that most invites a blind resend, so it
    // must still ask for a human look first.
    expect(en['inject.verifyAbsent']).toMatch(/confirm in the session/i)
    expect(zh['inject.verifyAbsent']).toContain('先自行到会话中确认')
    // Unavailable must not read as either outcome.
    expect(en['inject.verifyUnavailable']).toMatch(/nothing was learned/i)
    expect(en['inject.verifyUnavailable']).not.toMatch(/\bdelivered\b|never arrived/i)
    expect(zh['inject.verifyUnavailable']).toContain('没有得到结论')
    for (const key of [
      'inject.verifying',
      'inject.verifyConfirmed',
      'inject.verifyAbsent',
      'inject.verifyUnavailable',
      'inject.openTarget',
    ] as const) {
      expect(zh[key]).not.toBe(en[key])
    }
  })

  it('allows the transcript clock to run behind by a bounded amount', () => {
    // Enough slack for file-written timestamps, small enough that a message
    // the operator sent minutes earlier cannot be credited to this attempt.
    expect(VERIFY_SKEW_MS).toBeGreaterThan(0)
    expect(VERIFY_SKEW_MS).toBeLessThanOrEqual(60_000)
  })
})

// ---------------------------------------------------------------------------
// The read-only probe.
// ---------------------------------------------------------------------------

describe('createVerifyProbe', () => {
  it('reads one bounded newest window of the target session', async () => {
    const fetchPage = vi.fn<FetchTimelinePageFn>(() => Promise.resolve({
      entries: [entry()],
    }))
    const probe = createVerifyProbe('sess-1', { fetchPage })
    await expect(probe()).resolves.toEqual([entry()])
    expect(fetchPage).toHaveBeenCalledWith('sess-1', { limit: VERIFY_PAGE_LIMIT })
  })

  it('propagates a transport failure so the loop reads it as no answer', async () => {
    const failure = new ApiError('network', 'network_error', null)
    const probe = createVerifyProbe('sess-1', {
      fetchPage: () => Promise.reject(failure),
    })
    await expect(probe()).rejects.toBe(failure)
    const { sleep } = fakeSleep()
    await expect(verifyInjection({
      probe,
      head: 'restart the build please',
      sleep,
    })).resolves.toBe('unavailable')
  })
})
