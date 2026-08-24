/**
 * Pure logic for the inject panel (design §5.1 view 3, §5.3 confirmation
 * boundary, §8 S5-S7 wording). No React, no I/O, no imports from the data
 * layer — everything arrives and leaves as plain values, so this module is
 * unit-testable in a bare node environment (same posture as board/logic.ts).
 *
 * Decoupling contract (T4.5 ↔ S5 integration): the wire-facing types below
 * (PrepareSuccess / InjectResultView / InjectPlanView / ApiErrorLike) are
 * this module's OWN mirrors of the host action contract (src/routes.ts +
 * src/inject-gateway.ts) and of the data layer's normalized error
 * (client/api.ts ApiError, matched structurally so real instances satisfy
 * {@link ApiErrorLike} without an import). The integration layer feeds the
 * panel through the `onPrepare`/`onExecute` props; this module classifies
 * whatever comes back.
 *
 * Two-phase state machine (§4.f.5):
 *
 *   idle ──PREPARE_START──▶ preparing ──PREPARE_OK──▶ confirm
 *    ▲                          │                        │
 *    │◀──PREPARE_REJECTED/ERROR─┘        TICK(expired) / CANCEL
 *    │◀──────────────────────────────────────────────────┤
 *    │                                        EXECUTE_START
 *    │                                                   ▼
 *    │◀──RESET (delivered/failed only)── result ◀── executing
 *
 * `outcome: 'unknown'` is terminal: the reducer refuses RESET out of an
 * unknown result, so there is NO machine path that could re-drive an
 * execute after an unknown delivery (S6).
 *
 * @module
 */

import type { SidecarLocaleKey } from '../locales/index.ts'

// ---------------------------------------------------------------------------
// Wire-facing mirrors (host source of truth noted per type).
// ---------------------------------------------------------------------------

/** Injection mode, aligned with dsh `session.prompt` semantics (host: inject-gateway.ts). */
export type InjectMode = 'queue' | 'steer'

/** Terminal outcome vocabulary (host: inject-gateway.ts). */
export type InjectOutcome = 'delivered' | 'failed' | 'unknown'

/** Injection target reference as sent in the prepare envelope. */
export interface InjectTargetRef {
  agent: string
  sessionId: string
}

/** Live target snapshot captured by the server-side prepare re-check. */
export interface TargetStatusView {
  agent: string
  sessionId: string
  status: string
  title?: string
  project?: string
}

/** Confirmation plan echoed by `inject.prepare` (host: inject-gateway.ts). */
export interface InjectPlanView {
  target: InjectTargetRef
  mode: InjectMode
  targetStatus: TargetStatusView
  messagePreview: { bytes: number; head: string }
}

/** Success body of `POST action {type:'inject.prepare'}` (host: routes.ts). */
export interface PrepareSuccess {
  requestId: string
  confirmToken: string
  plan: InjectPlanView
  /** Epoch ms after which the confirmToken is dead. */
  expiresAt: number
}

/** Body of `POST action {type:'inject.execute'}` answers (host: routes.ts). */
export interface InjectResultView {
  outcome: InjectOutcome
  errorCode?: string
  detail?: string
  /** True when the gateway replayed a cached first result (idempotency). */
  replayed?: boolean
}

/**
 * Structural mirror of the data layer's ApiError (client/api.ts): real
 * instances satisfy this shape, so the panel can classify them without
 * importing the data layer.
 */
export interface ApiErrorLike {
  kind: 'timeout' | 'aborted' | 'network' | 'http' | 'parse'
  /** Server `{reason}` code for kind 'http', a stable local code otherwise. */
  reason: string
  status: number | null
}

/** What the panel hands the integration's `onPrepare` callback. */
export interface PanelPrepareRequest {
  target: InjectTargetRef
  mode: InjectMode
  message: string
}

/** What the panel hands the integration's `onExecute` callback. */
export interface PanelExecuteRequest {
  requestId: string
  confirmToken: string
  message: string
}

// ---------------------------------------------------------------------------
// Byte counting and message validation (mirrors the host gate so the UI
// refuses locally exactly what the server would refuse remotely).
// ---------------------------------------------------------------------------

/** Message byte cap; must mirror inject-gateway.ts (pinned by test). */
export const MAX_MESSAGE_BYTES = 16 * 1024

const utf8 = new TextEncoder()

/**
 * UTF-8 byte size of a message. TextEncoder agrees with the host's
 * `Buffer.byteLength(message, 'utf8')` for every well-formed string, so the
 * 16 KiB verdict is identical on both sides.
 */
export function messageBytes(message: string): number {
  return utf8.encode(message).length
}

export type MessageInvalidCode = 'empty' | 'nul' | 'too_large'

export type MessageValidation =
  | { ok: true; bytes: number }
  | { ok: false; code: MessageInvalidCode; bytes: number }

/** Local pre-validation, same checks and order as the host gateway. */
export function validateMessage(message: string): MessageValidation {
  const bytes = messageBytes(message)
  if (bytes === 0) return { ok: false, code: 'empty', bytes }
  if (message.includes('\u0000')) return { ok: false, code: 'nul', bytes }
  if (bytes > MAX_MESSAGE_BYTES) return { ok: false, code: 'too_large', bytes }
  return { ok: true, bytes }
}

/** Render model for the live byte counter / limit progress bar. */
export interface ByteUsage {
  bytes: number
  limit: number
  /** Fill fraction for the progress bar, clamped to [0, 1]. */
  ratio: number
  over: boolean
}

/** Derive the byte counter view from a byte count. */
export function byteUsage(bytes: number): ByteUsage {
  return {
    bytes,
    limit: MAX_MESSAGE_BYTES,
    ratio: Math.min(1, Math.max(0, bytes / MAX_MESSAGE_BYTES)),
    over: bytes > MAX_MESSAGE_BYTES,
  }
}

// ---------------------------------------------------------------------------
// Target visibility warning (design §4.d / S7).
// ---------------------------------------------------------------------------

/**
 * Whether the process-list visibility warning applies. Only cursor-cli
 * qualifies: its upstream contract puts the prompt on the native
 * subprocess argv. claude / codex / dsh do not warn — after S4a the
 * sidecar's own argv (send --message-stdin) and the dsh in-process path
 * both keep the body off the process list.
 */
export function showsProcessListWarning(agent: string): boolean {
  return agent.trim().toLowerCase() === 'cursor-cli'
}

// ---------------------------------------------------------------------------
// Token countdown (60s TTL is server-owned; the UI only reads expiresAt).
// ---------------------------------------------------------------------------

export interface TokenCountdown {
  remainingMs: number
  /** Whole seconds for display, rounded up so "1s" never shows as "0s". */
  seconds: number
  expired: boolean
}

/** Countdown view for a server-issued expiresAt (epoch ms). */
export function tokenCountdown(expiresAt: number, nowMs: number): TokenCountdown {
  const remainingMs = Math.max(0, expiresAt - nowMs)
  return {
    remainingMs,
    seconds: Math.ceil(remainingMs / 1000),
    expired: nowMs >= expiresAt,
  }
}

// ---------------------------------------------------------------------------
// Two-phase state machine.
// ---------------------------------------------------------------------------

/** Non-blocking notice shown on the editor after a failed/expired attempt. */
export type PanelNotice =
  | { kind: 'token_expired' }
  /** Server vocabulary rejection (prepare answered with an error status). */
  | { kind: 'prepare_rejected'; code: string; detail?: string }
  /** Transport-level prepare failure (timeout/network/…); safe to retry. */
  | { kind: 'prepare_error'; code: string }

export type PanelState =
  | { phase: 'idle'; notice: PanelNotice | null }
  | { phase: 'preparing'; message: string; mode: InjectMode }
  | {
      phase: 'confirm'
      message: string
      mode: InjectMode
      requestId: string
      confirmToken: string
      plan: InjectPlanView
      expiresAt: number
    }
  | {
      phase: 'executing'
      message: string
      mode: InjectMode
      requestId: string
      confirmToken: string
      plan: InjectPlanView
    }
  | { phase: 'result'; result: InjectResultView; plan: InjectPlanView | null }

export type InjectPhase = PanelState['phase']

export type PanelEvent =
  | { type: 'PREPARE_START'; message: string; mode: InjectMode }
  | { type: 'PREPARE_OK'; response: PrepareSuccess }
  | { type: 'PREPARE_REJECTED'; code: string; detail?: string }
  | { type: 'PREPARE_ERROR'; code: string }
  | { type: 'TICK'; nowMs: number }
  | { type: 'CANCEL' }
  | { type: 'EXECUTE_START' }
  | { type: 'EXECUTE_RESULT'; result: InjectResultView }
  | { type: 'RESET' }

/** Fresh machine state (factory, so callers can never share a mutable seed). */
export function initialPanelState(): PanelState {
  return { phase: 'idle', notice: null }
}

/**
 * Pure transition function. Events that do not apply to the current phase
 * return the state unchanged (same reference), which both makes stale
 * async callbacks harmless and gives React a free bail-out.
 */
export function reducePanel(state: PanelState, event: PanelEvent): PanelState {
  switch (event.type) {
    case 'PREPARE_START':
      if (state.phase !== 'idle') return state
      return { phase: 'preparing', message: event.message, mode: event.mode }

    case 'PREPARE_OK':
      if (state.phase !== 'preparing') return state
      return {
        phase: 'confirm',
        message: state.message,
        mode: state.mode,
        requestId: event.response.requestId,
        confirmToken: event.response.confirmToken,
        plan: event.response.plan,
        expiresAt: event.response.expiresAt,
      }

    case 'PREPARE_REJECTED':
      if (state.phase !== 'preparing') return state
      return {
        phase: 'idle',
        notice: {
          kind: 'prepare_rejected',
          code: event.code,
          ...(event.detail !== undefined ? { detail: event.detail } : {}),
        },
      }

    case 'PREPARE_ERROR':
      if (state.phase !== 'preparing') return state
      return { phase: 'idle', notice: { kind: 'prepare_error', code: event.code } }

    case 'TICK':
      if (state.phase !== 'confirm') return state
      if (event.nowMs < state.expiresAt) return state
      // Token TTL elapsed while waiting for the click: back to the editor
      // with a "prepare again" notice (§4.f.5, 60s server TTL).
      return { phase: 'idle', notice: { kind: 'token_expired' } }

    case 'CANCEL':
      if (state.phase !== 'confirm') return state
      return { phase: 'idle', notice: null }

    case 'EXECUTE_START':
      if (state.phase !== 'confirm') return state
      return {
        phase: 'executing',
        message: state.message,
        mode: state.mode,
        requestId: state.requestId,
        confirmToken: state.confirmToken,
        plan: state.plan,
      }

    case 'EXECUTE_RESULT':
      if (state.phase !== 'executing') return state
      return { phase: 'result', result: event.result, plan: state.plan }

    case 'RESET':
      if (state.phase !== 'result') return state
      // 'unknown' is terminal (S6): no machine path leads back to the
      // editor, so no UI built on this reducer can offer a retry.
      if (state.result.outcome === 'unknown') return state
      return { phase: 'idle', notice: null }
  }
}

// ---------------------------------------------------------------------------
// Response classification (PrepareSuccess/InjectResult vs normalized error).
// ---------------------------------------------------------------------------

const API_ERROR_KINDS: ReadonlySet<string> = new Set([
  'timeout',
  'aborted',
  'network',
  'http',
  'parse',
])

/** Structural guard matching client/api.ts ApiError instances. */
export function isApiErrorLike(value: unknown): value is ApiErrorLike {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  return (
    typeof v['kind'] === 'string' &&
    API_ERROR_KINDS.has(v['kind']) &&
    typeof v['reason'] === 'string' &&
    !('outcome' in v)
  )
}

/**
 * Classify what `onPrepare` resolved with. An HTTP-kind error is a server
 * vocabulary rejection (the reason carries the errorCode routes mapped to
 * the status); any other kind is a transport failure — harmless for
 * prepare, which is side-effect-free beyond a token that expires on its
 * own, so the editor may simply offer another attempt.
 */
export function classifyPrepareResponse(
  value: PrepareSuccess | ApiErrorLike,
): PanelEvent {
  if (isApiErrorLike(value)) {
    return value.kind === 'http'
      ? { type: 'PREPARE_REJECTED', code: value.reason }
      : { type: 'PREPARE_ERROR', code: value.reason }
  }
  return { type: 'PREPARE_OK', response: value }
}

/**
 * Classify what `onExecute` resolved with. An HTTP-kind error is the
 * routes-mapped failed outcome. Everything else (timeout/network/parse/
 * aborted) happened AFTER the execute may already have been dispatched, so
 * the honest verdict is `outcome: 'unknown'` — terminal, no retry (S6);
 * the user is pointed at the target session to verify.
 */
export function classifyExecuteResponse(
  value: InjectResultView | ApiErrorLike,
): PanelEvent {
  if (isApiErrorLike(value)) {
    if (value.kind === 'http') {
      return {
        type: 'EXECUTE_RESULT',
        result: { outcome: 'failed', errorCode: value.reason },
      }
    }
    return {
      type: 'EXECUTE_RESULT',
      result: { outcome: 'unknown', errorCode: value.reason },
    }
  }
  return { type: 'EXECUTE_RESULT', result: value }
}

// ---------------------------------------------------------------------------
// Enable/disable gate for the editor (design: 置灰规则).
// ---------------------------------------------------------------------------

export type EditorBlock = 'inject_off' | 'no_target' | 'busy' | 'invalid_message'

export interface EditorGate {
  canPrepare: boolean
  block: EditorBlock | null
}

/**
 * Why (if at all) the prepare action is unavailable, in priority order:
 * capability off > no target > a phase already in flight > invalid message.
 */
export function deriveEditorGate(input: {
  injectEnabled: boolean
  hasTarget: boolean
  phase: InjectPhase
  validation: MessageValidation
}): EditorGate {
  if (!input.injectEnabled) return { canPrepare: false, block: 'inject_off' }
  if (!input.hasTarget) return { canPrepare: false, block: 'no_target' }
  if (input.phase !== 'idle') return { canPrepare: false, block: 'busy' }
  if (!input.validation.ok) return { canPrepare: false, block: 'invalid_message' }
  return { canPrepare: true, block: null }
}

// ---------------------------------------------------------------------------
// Result actions (S6: unknown is terminal).
// ---------------------------------------------------------------------------

export interface ResultActions {
  /** True only for 'failed': a fresh prepare (fresh confirmation) is offered. */
  canReprepare: boolean
  /** True only for 'unknown': point the user at the session to verify. */
  showCheckSessionHint: boolean
}

/** Which follow-up affordances a terminal outcome earns. */
export function resultActions(outcome: InjectOutcome): ResultActions {
  return {
    canReprepare: outcome === 'failed',
    showCheckSessionHint: outcome === 'unknown',
  }
}

// ---------------------------------------------------------------------------
// Copy mapping (keys live in the T2.3 locale table, inject.* domain).
// ---------------------------------------------------------------------------

/** A locale key plus its `{name}` template params, ready for `t()`. */
export interface CopyRef {
  key: SidecarLocaleKey
  params?: Record<string, unknown>
}

/** Mode radio copy (label + semantics hint) per injection mode. */
export const MODE_COPY = {
  queue: { label: 'inject.modeQueue', hint: 'inject.modeQueueHint' },
  steer: { label: 'inject.modeSteer', hint: 'inject.modeSteerHint' },
} as const satisfies Record<InjectMode, { label: SidecarLocaleKey; hint: SidecarLocaleKey }>

/** Local validation verdict → copy key. */
export const MESSAGE_INVALID_COPY = {
  empty: 'inject.msgEmpty',
  nul: 'inject.msgNul',
  too_large: 'inject.msgTooLarge',
} as const satisfies Record<MessageInvalidCode, SidecarLocaleKey>

/** Validation verdict → renderable copy (too_large carries bytes/limit). */
export function messageInvalidCopy(
  validation: Extract<MessageValidation, { ok: false }>,
): CopyRef {
  if (validation.code === 'too_large') {
    return {
      key: MESSAGE_INVALID_COPY.too_large,
      params: { bytes: validation.bytes, limit: MAX_MESSAGE_BYTES },
    }
  }
  return { key: MESSAGE_INVALID_COPY[validation.code] }
}

/** Terminal outcome → headline copy key. */
export const RESULT_COPY = {
  delivered: 'inject.resultDelivered',
  failed: 'inject.resultFailed',
  unknown: 'inject.resultUnknown',
} as const satisfies Record<InjectOutcome, SidecarLocaleKey>

/**
 * Error vocabulary → copy key: the gateway codes (inject-gateway.ts), plus
 * the data layer's transport reasons (api.ts). Unlisted codes fall back to
 * the generic template via {@link errorCopy}.
 */
export const ERROR_COPY: Readonly<Record<string, SidecarLocaleKey>> = {
  inject_disabled: 'inject.errInjectDisabled',
  invalid_message: 'inject.errInvalidMessage',
  target_not_found: 'inject.errTargetNotFound',
  target_dead: 'inject.errTargetDead',
  too_many_pending: 'inject.errTooManyPending',
  token_missing: 'inject.errTokenMissing',
  token_expired: 'inject.errTokenExpired',
  token_reused: 'inject.errTokenReused',
  token_mismatch: 'inject.errTokenMismatch',
  unsupported_agent: 'inject.errUnsupportedAgent',
  executor_error: 'inject.errExecutorError',
  request_timeout: 'inject.errTimeout',
  request_aborted: 'inject.errAborted',
  network_error: 'inject.errNetwork',
  invalid_json: 'inject.errParse',
}

/** Error code → renderable copy; unknown codes get the generic template. */
export function errorCopy(code: string): CopyRef {
  const key = ERROR_COPY[code]
  return key === undefined ? { key: 'inject.errGeneric', params: { code } } : { key }
}

/** Editor notice → renderable copy. */
export function noticeCopy(notice: PanelNotice): CopyRef {
  if (notice.kind === 'token_expired') return { key: 'inject.tokenExpired' }
  return errorCopy(notice.code)
}
