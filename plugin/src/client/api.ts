/**
 * Browser-half data layer: same-origin fetch wrappers for the plugin's
 * self-registered route namespace (design §5.3; server: src/routes.ts).
 *
 * Wire types below are hand-mirrored from the host half (routes.ts /
 * session-store.ts / supervisor.ts / bridge.ts response shapes): the
 * client TS program cannot import host modules because they live in the
 * node-typed host program. The API_PREFIX mirror is pinned against the
 * routes.ts constant by test/client-data.test.ts.
 *
 * Calls are same-origin relative paths with no auth headers (ADR-8 trust
 * posture). Browser primitives — fetch, AbortController, timers — are
 * injectable so everything runs under plain node in tests; defaults
 * resolve from globalThis at call time. Pure data layer: no React, no
 * slots SDK; the UI half imports its types from here.
 *
 * @module
 */

/** Route namespace; must mirror routes.ts `API_PREFIX` (pinned by test). */
export const API_PREFIX = '/plugins/agent-sidecar/api'

/** Default overall request deadline (design §5.3: 15s fetch timeout). */
export const DEFAULT_TIMEOUT_MS = 15_000

// ---------------------------------------------------------------------------
// Wire types (host source of truth noted per type).
// ---------------------------------------------------------------------------

/** Daemon supervisor state machine states (host: supervisor.ts). */
export type SupervisorState =
  | 'probe'
  | 'adopted'
  | 'defer'
  | 'reprobe'
  | 'hosting'
  | 'hosted'
  | 'backoff'
  | 'failed'

/** Health of the host↔daemon subscribe stream (host: bridge.ts). */
export type StreamHealth = 'ok' | 'degraded' | 'unknown'

/** Daemon self-description from the Unix-socket ping (host: supervisor.ts). */
export interface PingInfo {
  pid: number
  version: string
  http: { enabled: boolean; host?: string; port?: number }
}

/** Compact most-recent-event summary per session (host: session-store.ts). */
export interface SessionEventSummary {
  ts: string
  kind: string
  text: string
}

/** One board row (host: session-store.ts). */
export interface SessionView {
  agent: string
  session_id: string
  status: string
  title: string
  project: string
  updated_at: number
  last_event: SessionEventSummary | null
  /** True when a dsh seq discontinuity was observed since the last snapshot. */
  gap: boolean
}

/** Full board state (host: session-store.ts). */
export interface BoardState {
  sessions: SessionView[]
  streamHealth: StreamHealth
  lastReconcileAt: number | null
}

/** Body of `GET state` and of every SSE `state` event (host: routes.ts). */
export interface StateSnapshot {
  daemon: { state: SupervisorState; lastPing: PingInfo | null }
  board: BoardState
  capabilities: { inject: boolean }
}

/** Body of `GET session/<id>` (host: routes.ts, M1 shape). */
export interface SessionDetail {
  session: SessionView
  /** Always null in M1; the event timeline lands in M3. */
  timeline: null
  timelineNote: string
}

/** `POST action` body for injection phase one (host: routes.ts `handlePrepare`). */
export interface PrepareActionBody {
  type: 'inject.prepare'
  target: { agent: string; sessionId: string }
  mode: 'queue' | 'steer'
  message: string
}

/** `POST action` body for injection phase two (host: routes.ts `handleExecute`). */
export interface ExecuteActionBody {
  type: 'inject.execute'
  requestId: string
  confirmToken: string
  message: string
}

/** `POST action` body for daemon management (host: routes.ts `handleAction`). */
export interface DaemonRetryActionBody {
  type: 'daemon.retry'
}

/**
 * The M2 action dispatcher envelope (host: routes.ts `handleAction`):
 * a `{type, ...}` union with the phase fields at the top level.
 */
export type ActionEnvelope = PrepareActionBody | ExecuteActionBody | DaemonRetryActionBody

// ---------------------------------------------------------------------------
// Injectable browser primitives (structural, so node tests can fake them).
// ---------------------------------------------------------------------------

/** Opaque timer handle so DOM numbers and fake-timer handles interoperate. */
export type TimerHandle = unknown

export interface AbortSignalLike {
  readonly aborted: boolean
  addEventListener(type: 'abort', listener: () => void): void
  removeEventListener(type: 'abort', listener: () => void): void
}

export interface AbortControllerLike {
  readonly signal: AbortSignalLike
  abort(): void
}

export interface ResponseLike {
  ok: boolean
  status: number
  json(): Promise<unknown>
}

export interface RequestInitLike {
  method: string
  headers?: Record<string, string>
  body?: string
  signal?: AbortSignalLike
}

export type FetchLike = (url: string, init: RequestInitLike) => Promise<ResponseLike>

/** Injectable primitives; every field defaults to the globalThis flavor. */
export interface ApiDeps {
  fetch?: FetchLike
  createAbortController?: () => AbortControllerLike
  setTimeout?: (fn: () => void, ms: number) => TimerHandle
  clearTimeout?: (handle: TimerHandle) => void
}

/** Per-call options for the request helpers. */
export interface RequestOptions extends ApiDeps {
  /** Overall deadline; default {@link DEFAULT_TIMEOUT_MS}. */
  timeoutMs?: number
  /** Caller-side cancellation; aborting it aborts the underlying fetch. */
  signal?: AbortSignalLike
}

// ---------------------------------------------------------------------------
// Normalized errors.
// ---------------------------------------------------------------------------

export type ApiErrorKind = 'timeout' | 'aborted' | 'network' | 'http' | 'parse'

/**
 * Single normalized failure shape for every request path, so the UI has
 * one catch surface. `reason` carries the server `{reason}` envelope for
 * kind 'http' and a stable local code otherwise.
 */
export class ApiError extends Error {
  readonly kind: ApiErrorKind
  readonly reason: string
  /** HTTP status when a response was received, else null. */
  readonly status: number | null

  constructor(
    kind: ApiErrorKind,
    reason: string,
    status: number | null = null,
    cause?: unknown,
  ) {
    super(
      status === null ? `api ${kind}: ${reason}` : `api ${kind}: ${reason} (http ${status})`,
      cause === undefined ? undefined : { cause },
    )
    this.name = 'ApiError'
    this.kind = kind
    this.reason = reason
    this.status = status
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError
}

// ---------------------------------------------------------------------------
// Defaults (resolved lazily so late-stubbed globals and fake timers work).
// ---------------------------------------------------------------------------

const defaultSetTimeout = (fn: () => void, ms: number): TimerHandle =>
  globalThis.setTimeout(fn, ms)

const defaultClearTimeout = (handle: TimerHandle): void => {
  globalThis.clearTimeout(handle as ReturnType<typeof globalThis.setTimeout>)
}

const defaultCreateAbortController = (): AbortControllerLike => new AbortController()

function resolveFetch(opts: ApiDeps): FetchLike {
  if (opts.fetch !== undefined) return opts.fetch
  // DOM fetch is only nominally stricter than FetchLike (its RequestInit
  // wants a branded AbortSignal); every value this module actually passes
  // is a real one when the default controller factory is in play.
  return globalThis.fetch as unknown as FetchLike
}

// ---------------------------------------------------------------------------
// Core request helper.
// ---------------------------------------------------------------------------

interface RequestInitInput {
  method: string
  headers?: Record<string, string>
  body?: string
}

async function request(
  path: string,
  init: RequestInitInput,
  opts: RequestOptions,
): Promise<unknown> {
  const doFetch = resolveFetch(opts)
  const controller = (opts.createAbortController ?? defaultCreateAbortController)()
  const setT = opts.setTimeout ?? defaultSetTimeout
  const clearT = opts.clearTimeout ?? defaultClearTimeout
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS

  let timedOut = false
  let externallyAborted = false
  const timer = setT(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)

  const external = opts.signal
  const onExternalAbort = (): void => {
    externallyAborted = true
    controller.abort()
  }
  if (external !== undefined) {
    if (external.aborted) onExternalAbort()
    else external.addEventListener('abort', onExternalAbort)
  }

  try {
    let res: ResponseLike
    try {
      res = await doFetch(path, { ...init, signal: controller.signal })
    } catch (err) {
      if (timedOut) throw new ApiError('timeout', 'request_timeout', null, err)
      if (externallyAborted) throw new ApiError('aborted', 'request_aborted', null, err)
      throw new ApiError('network', 'network_error', null, err)
    }
    if (!res.ok) {
      // routes.ts answers most failures with a `{reason}` envelope; the M2
      // inject.execute failure answers the result view itself ({outcome:
      // 'failed', errorCode}) with no reason key, so the vocabulary code is
      // read as the fallback. Status-derived reason covers non-JSON bodies.
      let reason = `http_${res.status}`
      try {
        const body = await res.json()
        if (typeof body === 'object' && body !== null) {
          const record = body as Record<string, unknown>
          const value = record['reason']
          const code = record['errorCode']
          if (typeof value === 'string' && value !== '') reason = value
          else if (typeof code === 'string' && code !== '') reason = code
        }
      } catch {
        // Non-JSON error body: the fallback reason stands.
      }
      throw new ApiError('http', reason, res.status)
    }
    try {
      return await res.json()
    } catch (err) {
      if (timedOut) throw new ApiError('timeout', 'request_timeout', null, err)
      throw new ApiError('parse', 'invalid_json', res.status, err)
    }
  } finally {
    clearT(timer)
    if (external !== undefined) external.removeEventListener('abort', onExternalAbort)
  }
}

// ---------------------------------------------------------------------------
// Public surface.
// ---------------------------------------------------------------------------

/** `GET <prefix>/state` — full board snapshot. */
export async function fetchState(opts: RequestOptions = {}): Promise<StateSnapshot> {
  return (await request(`${API_PREFIX}/state`, { method: 'GET' }, opts)) as StateSnapshot
}

/**
 * `GET <prefix>/session/<id>` — single-session detail. Unknown ids reject
 * with an ApiError carrying the server's `session_not_found` reason.
 */
export async function fetchSession(
  sessionId: string,
  opts: RequestOptions = {},
): Promise<SessionDetail> {
  const path = `${API_PREFIX}/session/${encodeURIComponent(sessionId)}`
  return (await request(path, { method: 'GET' }, opts)) as SessionDetail
}

/**
 * `POST <prefix>/action` — transport layer only: the envelope is passed
 * through verbatim, failures are normalized, and there is deliberately NO
 * retry here — requestId idempotency and the `delivery:unknown` no-retry
 * rule (S6) are gateway/UI policy, not transport policy.
 */
export async function postAction(
  body: ActionEnvelope,
  opts: RequestOptions = {},
): Promise<unknown> {
  return request(
    `${API_PREFIX}/action`,
    {
      method: 'POST',
      // The guard requires application/json on POST (415 otherwise).
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    },
    opts,
  )
}
