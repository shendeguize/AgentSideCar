/**
 * Unit tests for the inject-panel pure logic (src/client/inject/logic.ts).
 * Pure functions only — no React, no DOM, node environment (rendering is
 * validated by the integration wave against a real dsh web).
 *
 * The test deliberately imports from BOTH halves to pin the mirrors: the
 * host gateway's MAX_MESSAGE_BYTES (byte-cap agreement) and the data
 * layer's real ApiError class (structural {@link ApiErrorLike} agreement).
 */

import { describe, expect, it } from 'vitest'
import { MAX_MESSAGE_BYTES as HOST_MAX_MESSAGE_BYTES } from '../src/inject-gateway.ts'
import { ApiError } from '../src/client/api.ts'
import { en, zh } from '../src/client/locales/index.ts'
import {
  byteUsage,
  classifyExecuteResponse,
  classifyPanelKey,
  classifyPrepareResponse,
  deriveEditorGate,
  isDeliveredResult,
  ERROR_COPY,
  errorCopy,
  initialPanelState,
  isApiErrorLike,
  MAX_MESSAGE_BYTES,
  MESSAGE_INVALID_COPY,
  messageBytes,
  messageInvalidCopy,
  MODE_COPY,
  noticeCopy,
  reducePanel,
  RESULT_COPY,
  resultActions,
  showsProcessListWarning,
  tokenCountdown,
  validateMessage,
} from '../src/client/inject/logic.ts'
import type {
  InjectPhase,
  InjectPlanView,
  MessageValidation,
  PanelState,
  PrepareSuccess,
} from '../src/client/inject/logic.ts'

const NOW = 1_700_000_000_000

const plan: InjectPlanView = {
  target: { agent: 'claude', sessionId: 'sess-1' },
  mode: 'queue',
  targetStatus: {
    agent: 'claude',
    sessionId: 'sess-1',
    status: 'waiting',
    title: '标题',
  },
  messagePreview: { bytes: 5, head: 'hello' },
}

const prepared: PrepareSuccess = {
  requestId: 'req-1',
  confirmToken: 'tok-1',
  plan,
  expiresAt: NOW + 60_000,
}

/** Walk a fresh machine into the confirm phase. */
function confirmState(): PanelState {
  let state = initialPanelState()
  state = reducePanel(state, { type: 'PREPARE_START', message: 'hello', mode: 'queue' })
  state = reducePanel(state, { type: 'PREPARE_OK', response: prepared })
  return state
}

/** Walk a fresh machine into the executing phase. */
function executingState(): PanelState {
  return reducePanel(confirmState(), { type: 'EXECUTE_START' })
}

/** Walk a fresh machine into a terminal result. */
function resultState(outcome: 'delivered' | 'failed' | 'unknown'): PanelState {
  return reducePanel(executingState(), {
    type: 'EXECUTE_RESULT',
    result: { outcome, ...(outcome === 'failed' ? { errorCode: 'executor_error' } : {}) },
  })
}

// ---------------------------------------------------------------------------

describe('messageBytes (UTF-8, TextEncoder)', () => {
  it('counts ASCII one byte per char', () => {
    expect(messageBytes('abc')).toBe(3)
  })

  it('counts CJK three bytes per char', () => {
    expect(messageBytes('中')).toBe(3)
    expect(messageBytes('中文')).toBe(6)
  })

  it('counts astral-plane emoji four bytes', () => {
    expect(messageBytes('😀')).toBe(4)
  })

  it('counts mixed content per encoded byte, not per code unit', () => {
    const mixed = 'a中😀'
    expect(mixed.length).toBe(4) // 2 UTF-16 units for the emoji
    expect(messageBytes(mixed)).toBe(1 + 3 + 4)
  })

  it('agrees with the host Buffer.byteLength verdict', () => {
    const sample = 'hello 世界 🚀 café\n\t终'
    expect(messageBytes(sample)).toBe(Buffer.byteLength(sample, 'utf8'))
  })

  it('pins the byte cap to the host gateway constant (16 KiB)', () => {
    expect(MAX_MESSAGE_BYTES).toBe(HOST_MAX_MESSAGE_BYTES)
    expect(MAX_MESSAGE_BYTES).toBe(16 * 1024)
  })
})

describe('validateMessage boundaries', () => {
  it('accepts a message of exactly 16 KiB', () => {
    const verdict = validateMessage('a'.repeat(MAX_MESSAGE_BYTES))
    expect(verdict).toEqual({ ok: true, bytes: MAX_MESSAGE_BYTES })
  })

  it('rejects one byte over the cap', () => {
    const verdict = validateMessage('a'.repeat(MAX_MESSAGE_BYTES + 1))
    expect(verdict).toEqual({ ok: false, code: 'too_large', bytes: MAX_MESSAGE_BYTES + 1 })
  })

  it('judges multibyte content by bytes, not by string length', () => {
    // 5462 CJK chars = 16386 bytes: over the cap although length << cap.
    const over = validateMessage('中'.repeat(5462))
    expect(over).toEqual({ ok: false, code: 'too_large', bytes: 16_386 })
    // 5461 CJK chars = 16383 bytes: fits.
    expect(validateMessage('中'.repeat(5461))).toEqual({ ok: true, bytes: 16_383 })
  })

  it('rejects the empty message', () => {
    expect(validateMessage('')).toEqual({ ok: false, code: 'empty', bytes: 0 })
  })

  it('rejects a NUL byte anywhere', () => {
    expect(validateMessage('a\u0000b')).toEqual({ ok: false, code: 'nul', bytes: 3 })
  })

  it('reports NUL before size (host check order)', () => {
    const verdict = validateMessage('a'.repeat(MAX_MESSAGE_BYTES + 10) + '\u0000')
    expect(verdict.ok).toBe(false)
    if (!verdict.ok) expect(verdict.code).toBe('nul')
  })
})

describe('byteUsage', () => {
  it('reports the fill ratio against the limit', () => {
    const usage = byteUsage(MAX_MESSAGE_BYTES / 2)
    expect(usage.ratio).toBe(0.5)
    expect(usage.over).toBe(false)
    expect(usage.limit).toBe(MAX_MESSAGE_BYTES)
  })

  it('clamps the ratio at 1 and flags over', () => {
    const usage = byteUsage(MAX_MESSAGE_BYTES * 3)
    expect(usage.ratio).toBe(1)
    expect(usage.over).toBe(true)
  })

  it('is exactly full (not over) at the cap', () => {
    const usage = byteUsage(MAX_MESSAGE_BYTES)
    expect(usage.ratio).toBe(1)
    expect(usage.over).toBe(false)
  })
})

// ---------------------------------------------------------------------------

describe('state machine: prepare phase', () => {
  it('starts idle with no notice', () => {
    expect(initialPanelState()).toEqual({ phase: 'idle', notice: null })
  })

  it('PREPARE_START moves idle → preparing, carrying message and mode', () => {
    const state = reducePanel(initialPanelState(), {
      type: 'PREPARE_START', message: 'hi', mode: 'steer',
    })
    expect(state).toEqual({ phase: 'preparing', message: 'hi', mode: 'steer' })
  })

  it('PREPARE_START is a no-op outside idle (stale double-click)', () => {
    const state = confirmState()
    expect(reducePanel(state, { type: 'PREPARE_START', message: 'x', mode: 'queue' }))
      .toBe(state)
  })

  it('PREPARE_OK moves preparing → confirm with token, plan and deadline', () => {
    const state = confirmState()
    expect(state).toEqual({
      phase: 'confirm',
      message: 'hello',
      mode: 'queue',
      requestId: 'req-1',
      confirmToken: 'tok-1',
      plan,
      expiresAt: NOW + 60_000,
    })
  })

  it('PREPARE_OK is a no-op when not preparing (stale resolution)', () => {
    const idle = initialPanelState()
    expect(reducePanel(idle, { type: 'PREPARE_OK', response: prepared })).toBe(idle)
  })

  it('PREPARE_REJECTED falls back to idle with the vocabulary notice', () => {
    const preparing = reducePanel(initialPanelState(), {
      type: 'PREPARE_START', message: 'hi', mode: 'queue',
    })
    const state = reducePanel(preparing, {
      type: 'PREPARE_REJECTED', code: 'target_dead', detail: 'ended',
    })
    expect(state).toEqual({
      phase: 'idle',
      notice: { kind: 'prepare_rejected', code: 'target_dead', detail: 'ended' },
    })
  })

  it('PREPARE_ERROR falls back to idle with the transport notice', () => {
    const preparing = reducePanel(initialPanelState(), {
      type: 'PREPARE_START', message: 'hi', mode: 'queue',
    })
    expect(reducePanel(preparing, { type: 'PREPARE_ERROR', code: 'request_timeout' }))
      .toEqual({ phase: 'idle', notice: { kind: 'prepare_error', code: 'request_timeout' } })
  })
})

describe('state machine: confirm phase and token countdown', () => {
  it('TICK before the deadline keeps the exact same state reference', () => {
    const state = confirmState()
    expect(reducePanel(state, { type: 'TICK', nowMs: NOW + 59_999 })).toBe(state)
  })

  it('TICK at the deadline expires back to idle with the token_expired notice', () => {
    const state = reducePanel(confirmState(), { type: 'TICK', nowMs: NOW + 60_000 })
    expect(state).toEqual({ phase: 'idle', notice: { kind: 'token_expired' } })
  })

  it('TICK after the deadline expires too', () => {
    const state = reducePanel(confirmState(), { type: 'TICK', nowMs: NOW + 90_000 })
    expect(state).toEqual({ phase: 'idle', notice: { kind: 'token_expired' } })
  })

  it('TICK never interrupts an execute already in flight', () => {
    const state = executingState()
    expect(reducePanel(state, { type: 'TICK', nowMs: NOW + 120_000 })).toBe(state)
  })

  it('CANCEL returns to a clean idle', () => {
    expect(reducePanel(confirmState(), { type: 'CANCEL' }))
      .toEqual({ phase: 'idle', notice: null })
  })

  it('CANCEL is a no-op outside confirm', () => {
    const state = executingState()
    expect(reducePanel(state, { type: 'CANCEL' })).toBe(state)
  })

  it('EXECUTE_START moves confirm → executing keeping the binding', () => {
    const state = executingState()
    expect(state).toEqual({
      phase: 'executing',
      message: 'hello',
      mode: 'queue',
      requestId: 'req-1',
      confirmToken: 'tok-1',
      plan,
    })
  })

  it('EXECUTE_START is a no-op outside confirm (no double dispatch)', () => {
    const idle = initialPanelState()
    expect(reducePanel(idle, { type: 'EXECUTE_START' })).toBe(idle)
    const executing = executingState()
    expect(reducePanel(executing, { type: 'EXECUTE_START' })).toBe(executing)
  })

  it('tokenCountdown rounds seconds up and flags expiry at the boundary', () => {
    expect(tokenCountdown(NOW + 60_000, NOW))
      .toEqual({ remainingMs: 60_000, seconds: 60, expired: false })
    expect(tokenCountdown(NOW + 1, NOW))
      .toEqual({ remainingMs: 1, seconds: 1, expired: false })
    expect(tokenCountdown(NOW, NOW))
      .toEqual({ remainingMs: 0, seconds: 0, expired: true })
    expect(tokenCountdown(NOW - 5_000, NOW))
      .toEqual({ remainingMs: 0, seconds: 0, expired: true })
  })
})

describe('state machine: results and the unknown terminal (S6)', () => {
  it('EXECUTE_RESULT lands each of the three outcomes in the result phase', () => {
    for (const outcome of ['delivered', 'failed', 'unknown'] as const) {
      const state = resultState(outcome)
      expect(state.phase).toBe('result')
      if (state.phase === 'result') expect(state.result.outcome).toBe(outcome)
    }
  })

  it('EXECUTE_RESULT is a no-op when nothing is executing (stale resolution)', () => {
    const idle = initialPanelState()
    expect(reducePanel(idle, {
      type: 'EXECUTE_RESULT', result: { outcome: 'delivered' },
    })).toBe(idle)
  })

  it('RESET reopens the editor after failed (re-prepare path)', () => {
    expect(reducePanel(resultState('failed'), { type: 'RESET' }))
      .toEqual({ phase: 'idle', notice: null })
  })

  it('RESET reopens the editor after delivered (next message)', () => {
    expect(reducePanel(resultState('delivered'), { type: 'RESET' }))
      .toEqual({ phase: 'idle', notice: null })
  })

  it('RESET is REFUSED after unknown: no machine path can retry', () => {
    const terminal = resultState('unknown')
    expect(reducePanel(terminal, { type: 'RESET' })).toBe(terminal)
    // …and no other event escapes it either (belt and braces).
    expect(reducePanel(terminal, { type: 'PREPARE_START', message: 'x', mode: 'queue' }))
      .toBe(terminal)
    expect(reducePanel(terminal, { type: 'EXECUTE_START' })).toBe(terminal)
    expect(reducePanel(terminal, { type: 'CANCEL' })).toBe(terminal)
  })

  it('resultActions offers re-prepare only for failed, verify-hint only for unknown', () => {
    expect(resultActions('failed'))
      .toEqual({ canReprepare: true, showCheckSessionHint: false })
    expect(resultActions('delivered'))
      .toEqual({ canReprepare: false, showCheckSessionHint: false })
    expect(resultActions('unknown'))
      .toEqual({ canReprepare: false, showCheckSessionHint: true })
  })

  it('isDeliveredResult gates the UX-05 observation loop on delivered only', () => {
    expect(isDeliveredResult({ outcome: 'delivered' })).toBe(true)
    expect(isDeliveredResult({ outcome: 'delivered', replayed: true })).toBe(true)
    expect(isDeliveredResult({ outcome: 'failed' })).toBe(false)
    expect(isDeliveredResult({ outcome: 'unknown' })).toBe(false)
    expect(isDeliveredResult(new ApiError('http', 'target_dead', 409))).toBe(false)
  })
})

// ---------------------------------------------------------------------------

describe('classifyPanelKey (UX-10 keyboard affordances)', () => {
  const base = {
    key: 'Enter',
    metaKey: false,
    ctrlKey: false,
    phase: 'idle' as InjectPhase,
    canPrepare: true,
  }
  const PHASES: readonly InjectPhase[] = ['idle', 'preparing', 'confirm', 'executing', 'result']

  it('Escape closes in every phase', () => {
    for (const phase of PHASES) {
      expect(classifyPanelKey({ ...base, key: 'Escape', phase })).toBe('close')
    }
  })

  it('Cmd/Ctrl+Enter prepares only from the editor with a valid message', () => {
    expect(classifyPanelKey({ ...base, metaKey: true })).toBe('prepare')
    expect(classifyPanelKey({ ...base, ctrlKey: true })).toBe('prepare')
    expect(classifyPanelKey({ ...base, metaKey: true, canPrepare: false })).toBeNull()
  })

  it('binds NOTHING outside the editor: confirming stays an explicit click', () => {
    for (const phase of PHASES) {
      if (phase === 'idle') continue
      expect(classifyPanelKey({ ...base, metaKey: true, phase })).toBeNull()
      expect(classifyPanelKey({ ...base, ctrlKey: true, phase })).toBeNull()
    }
  })

  it('plain Enter never submits', () => {
    expect(classifyPanelKey(base)).toBeNull()
  })
})

// ---------------------------------------------------------------------------

describe('response classification', () => {
  it('recognizes real ApiError instances structurally', () => {
    expect(isApiErrorLike(new ApiError('http', 'target_dead', 409))).toBe(true)
    expect(isApiErrorLike(new ApiError('timeout', 'request_timeout'))).toBe(true)
  })

  it('does not mistake results or junk for errors', () => {
    expect(isApiErrorLike({ outcome: 'delivered' })).toBe(false)
    expect(isApiErrorLike({ kind: 'http' })).toBe(false) // no reason
    expect(isApiErrorLike({ kind: 'weird', reason: 'x' })).toBe(false)
    expect(isApiErrorLike(null)).toBe(false)
    expect(isApiErrorLike('boom')).toBe(false)
  })

  it('classifies a prepare success as PREPARE_OK', () => {
    expect(classifyPrepareResponse(prepared))
      .toEqual({ type: 'PREPARE_OK', response: prepared })
  })

  it('classifies an http prepare error as a vocabulary rejection', () => {
    expect(classifyPrepareResponse(new ApiError('http', 'too_many_pending', 429)))
      .toEqual({ type: 'PREPARE_REJECTED', code: 'too_many_pending' })
  })

  it('classifies transport prepare errors as retryable PREPARE_ERROR', () => {
    expect(classifyPrepareResponse(new ApiError('timeout', 'request_timeout')))
      .toEqual({ type: 'PREPARE_ERROR', code: 'request_timeout' })
    expect(classifyPrepareResponse(new ApiError('network', 'network_error')))
      .toEqual({ type: 'PREPARE_ERROR', code: 'network_error' })
  })

  it('passes execute results through untouched (incl. replayed)', () => {
    const result = { outcome: 'delivered' as const, replayed: true }
    expect(classifyExecuteResponse(result))
      .toEqual({ type: 'EXECUTE_RESULT', result })
  })

  it('maps an http execute error to a failed outcome with the code', () => {
    expect(classifyExecuteResponse(new ApiError('http', 'token_expired', 401)))
      .toEqual({
        type: 'EXECUTE_RESULT',
        result: { outcome: 'failed', errorCode: 'token_expired' },
      })
  })

  it('maps execute transport loss to the terminal unknown (may have delivered)', () => {
    for (const err of [
      new ApiError('timeout', 'request_timeout'),
      new ApiError('network', 'network_error'),
      new ApiError('parse', 'invalid_json', 200),
    ]) {
      expect(classifyExecuteResponse(err)).toEqual({
        type: 'EXECUTE_RESULT',
        result: { outcome: 'unknown', errorCode: err.reason },
      })
    }
  })

  it('an execute transport loss therefore reaches the no-retry terminal', () => {
    const event = classifyExecuteResponse(new ApiError('timeout', 'request_timeout'))
    const state = reducePanel(executingState(), event)
    expect(state.phase).toBe('result')
    expect(reducePanel(state, { type: 'RESET' })).toBe(state) // no retry path
  })
})

// ---------------------------------------------------------------------------

describe('editor gate (置灰规则)', () => {
  const okValidation: MessageValidation = { ok: true, bytes: 5 }
  const badValidation: MessageValidation = { ok: false, code: 'empty', bytes: 0 }

  it('capability off blocks everything, whatever else holds', () => {
    expect(deriveEditorGate({
      injectEnabled: false, hasTarget: false, phase: 'executing', validation: badValidation,
    })).toEqual({ canPrepare: false, block: 'inject_off' })
  })

  it('missing target blocks next', () => {
    expect(deriveEditorGate({
      injectEnabled: true, hasTarget: false, phase: 'idle', validation: okValidation,
    })).toEqual({ canPrepare: false, block: 'no_target' })
  })

  it('any in-flight phase blocks as busy', () => {
    for (const phase of ['preparing', 'confirm', 'executing', 'result'] as const) {
      expect(deriveEditorGate({
        injectEnabled: true, hasTarget: true, phase, validation: okValidation,
      })).toEqual({ canPrepare: false, block: 'busy' })
    }
  })

  it('an invalid message blocks last', () => {
    expect(deriveEditorGate({
      injectEnabled: true, hasTarget: true, phase: 'idle', validation: badValidation,
    })).toEqual({ canPrepare: false, block: 'invalid_message' })
  })

  it('everything green enables prepare', () => {
    expect(deriveEditorGate({
      injectEnabled: true, hasTarget: true, phase: 'idle', validation: okValidation,
    })).toEqual({ canPrepare: true, block: null })
  })
})

describe('cursor-cli process-list warning gate', () => {
  it('warns for cursor-cli only', () => {
    expect(showsProcessListWarning('cursor-cli')).toBe(true)
    expect(showsProcessListWarning(' Cursor-CLI ')).toBe(true) // wire-noise tolerant
  })

  it('never warns for the stdin/in-process paths and the rest', () => {
    for (const agent of ['claude', 'codex', 'dsh', 'cursor-ide', 'cursor', 'kimi', 'copilot', '']) {
      expect(showsProcessListWarning(agent), agent).toBe(false)
    }
  })
})

// ---------------------------------------------------------------------------

describe('copy mapping', () => {
  it('maps every gateway vocabulary code to a dedicated key', () => {
    const vocab = [
      'inject_disabled', 'invalid_message', 'target_not_found', 'target_dead',
      'too_many_pending', 'token_missing', 'token_expired', 'token_reused',
      'token_mismatch', 'unsupported_agent', 'executor_error',
    ]
    for (const code of vocab) {
      const copy = errorCopy(code)
      expect(copy.key, code).not.toBe('inject.errGeneric')
      expect(copy.key.startsWith('inject.err'), code).toBe(true)
    }
  })

  it('maps the data-layer transport reasons to dedicated keys', () => {
    expect(errorCopy('request_timeout').key).toBe('inject.errTimeout')
    expect(errorCopy('request_aborted').key).toBe('inject.errAborted')
    expect(errorCopy('network_error').key).toBe('inject.errNetwork')
    expect(errorCopy('invalid_json').key).toBe('inject.errParse')
  })

  it('falls back to the generic template, carrying the raw code', () => {
    expect(errorCopy('http_502'))
      .toEqual({ key: 'inject.errGeneric', params: { code: 'http_502' } })
  })

  it('renders the outcome headlines from RESULT_COPY', () => {
    expect(RESULT_COPY.delivered).toBe('inject.resultDelivered')
    expect(RESULT_COPY.failed).toBe('inject.resultFailed')
    expect(RESULT_COPY.unknown).toBe('inject.resultUnknown')
  })

  it('the unknown copy says: may be delivered, do not retry, check the session', () => {
    expect(zh[RESULT_COPY.unknown]).toContain('可能已投递')
    expect(zh[RESULT_COPY.unknown]).toContain('请勿重试')
    expect(zh[RESULT_COPY.unknown]).toContain('目标会话核对')
    expect(en[RESULT_COPY.unknown]).toContain('Do NOT retry')
  })

  it('too_large invalid copy carries bytes and the limit', () => {
    expect(messageInvalidCopy({ ok: false, code: 'too_large', bytes: 20_000 }))
      .toEqual({
        key: 'inject.msgTooLarge',
        params: { bytes: 20_000, limit: MAX_MESSAGE_BYTES },
      })
    expect(messageInvalidCopy({ ok: false, code: 'empty', bytes: 0 }))
      .toEqual({ key: 'inject.msgEmpty' })
    expect(messageInvalidCopy({ ok: false, code: 'nul', bytes: 3 }))
      .toEqual({ key: 'inject.msgNul' })
  })

  it('notices map to their copy (expiry fixed key, rejections via errorCopy)', () => {
    expect(noticeCopy({ kind: 'token_expired' })).toEqual({ key: 'inject.tokenExpired' })
    expect(noticeCopy({ kind: 'prepare_rejected', code: 'target_dead' }))
      .toEqual({ key: 'inject.errTargetDead' })
    expect(noticeCopy({ kind: 'prepare_error', code: 'network_error' }))
      .toEqual({ key: 'inject.errNetwork' })
  })
})

describe('locale table coverage (inject.* domain, zh/en aligned)', () => {
  const injectKeysZh = Object.keys(zh).filter(key => key.startsWith('inject.'))
  const injectKeysEn = Object.keys(en).filter(key => key.startsWith('inject.'))

  it('the inject domain is populated and identical across locales', () => {
    expect(injectKeysZh.length).toBeGreaterThan(0)
    expect(injectKeysEn.sort()).toEqual(injectKeysZh.sort())
  })

  it('every key the logic maps reference exists non-empty in zh AND en', () => {
    const referenced = new Set<string>([
      ...Object.values(ERROR_COPY),
      ...Object.values(RESULT_COPY),
      ...Object.values(MESSAGE_INVALID_COPY),
      ...Object.values(MODE_COPY).flatMap(copy => [copy.label, copy.hint]),
      'inject.tokenExpired',
      'inject.errGeneric',
    ])
    for (const key of referenced) {
      expect(zh[key as keyof typeof zh], `zh missing ${key}`).toBeTypeOf('string')
      expect(zh[key as keyof typeof zh].length, `zh empty ${key}`).toBeGreaterThan(0)
      expect(en[key as keyof typeof en], `en missing ${key}`).toBeTypeOf('string')
      expect(en[key as keyof typeof en].length, `en empty ${key}`).toBeGreaterThan(0)
    }
  })

  it('template keys keep their placeholders in both locales', () => {
    for (const dict of [zh, en]) {
      expect(dict['inject.byteCount']).toContain('{bytes}')
      expect(dict['inject.byteCount']).toContain('{limit}')
      expect(dict['inject.msgTooLarge']).toContain('{bytes}')
      expect(dict['inject.msgTooLarge']).toContain('{limit}')
      expect(dict['inject.countdown']).toContain('{seconds}')
      expect(dict['inject.planStatus']).toContain('{status}')
      expect(dict['inject.planPreviewLabel']).toContain('{bytes}')
      expect(dict['inject.errGeneric']).toContain('{code}')
    }
  })
})
