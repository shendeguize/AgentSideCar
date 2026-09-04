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
  /** Version in the daemon's own tree; absent when it cannot be read. */
  sourceVersion?: string
  /** The daemon's tree no longer matches the code it loaded. */
  sourceChanged?: boolean
  http: { enabled: boolean; host?: string; port?: number }
}

/**
 * Why the host's last hosting attempt failed (host: supervisor.ts).
 * `reason` stays a string here so an unrecognized cause degrades to the
 * plain offline wording instead of breaking the page.
 */
export interface DaemonFailure {
  reason: string
  exitCode: number | null
  detail: string | null
}

/** Compact most-recent-event summary per session (host: session-store.ts). */
export interface SessionEventSummary {
  ts: string
  kind: string
  text: string
}

/** Stable host verdict vocabulary for one injection target. */
export type InjectIneligibilityReason =
  | 'unsupported_agent'
  | 'working_session'
  | 'dead_session'
  | 'child_session'
  | 'remote_session'
  | 'invalid_session'
  | 'target_dead'

export type InjectEligibility =
  | Readonly<{ allowed: true; reason: 'eligible' }>
  | Readonly<{ allowed: false; reason: InjectIneligibilityReason }>

export type InjectBlockReason = 'inject_disabled' | InjectIneligibilityReason

const INJECT_INELIGIBILITY_REASONS: ReadonlySet<string> = new Set([
  'unsupported_agent',
  'working_session',
  'dead_session',
  'child_session',
  'remote_session',
  'invalid_session',
  'target_dead',
])

/** Fail-closed legacy/malformed-host fallback; never infer eligibility locally. */
export const INVALID_INJECT_ELIGIBILITY: InjectEligibility = Object.freeze({
  allowed: false,
  reason: 'invalid_session',
})

/** One board row (host: session-store.ts). */
export interface SessionView {
  agent: string
  session_id: string
  status: string
  title: string
  project: string
  updated_at: number
  /** Epoch SECONDS of session birth; absent when the adapter cannot tell. */
  created_at?: number
  /** Absolute transcript path; absent for adapters with no file on disk. */
  transcript?: string
  model?: string
  model_provider?: string
  last_event: SessionEventSummary | null
  /** True when a dsh seq discontinuity was observed since the last snapshot. */
  gap: boolean
  /**
   * Target verdict from current hosts. Optional at the wire boundary only so
   * an older host fails closed with visible compatibility guidance.
   */
  injectEligibility?: InjectEligibility
  /** Snake-case spelling emitted by host versions using daemon wire naming. */
  inject_eligibility?: InjectEligibility
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Validate an untrusted host verdict. Only the exact positive tuple
 * `{allowed:true, reason:"eligible"}` opens the gate.
 */
export function normalizeInjectEligibility(value: unknown): InjectEligibility {
  if (!isRecord(value)) return INVALID_INJECT_ELIGIBILITY
  if (value['allowed'] === true && value['reason'] === 'eligible') {
    return { allowed: true, reason: 'eligible' }
  }
  const reason = value['reason']
  if (
    value['allowed'] === false &&
    typeof reason === 'string' &&
    INJECT_INELIGIBILITY_REASONS.has(reason)
  ) {
    return { allowed: false, reason: reason as InjectIneligibilityReason }
  }
  return INVALID_INJECT_ELIGIBILITY
}

/**
 * Read the current verdict while retaining fail-closed compatibility with
 * pre-verdict hosts. Camel-case wins when explicitly present; malformed
 * values never fall through to another field or to a local allow-list.
 */
export function sessionInjectEligibility(
  session: SessionView | null | undefined,
  expected?: Readonly<{ agent?: string; sessionId?: string }>,
): InjectEligibility {
  if (session === null || session === undefined) return INVALID_INJECT_ELIGIBILITY
  if (
    (expected?.agent !== undefined && session.agent !== expected.agent) ||
    (expected?.sessionId !== undefined && session.session_id !== expected.sessionId)
  ) {
    return INVALID_INJECT_ELIGIBILITY
  }
  const record = session as SessionView & Record<string, unknown>
  const hasCamel = Object.prototype.hasOwnProperty.call(record, 'injectEligibility')
  return normalizeInjectEligibility(
    hasCamel ? record['injectEligibility'] : record['inject_eligibility'],
  )
}

/** Merge the global capability gate with the target verdict, global first. */
export function injectBlockReason(
  injectEnabled: boolean,
  eligibility: unknown,
): InjectBlockReason | null {
  if (!injectEnabled) return 'inject_disabled'
  const normalized = normalizeInjectEligibility(eligibility)
  return normalized.allowed ? null : normalized.reason
}

/** One archived board row (host: session-store.ts `ArchivedSessionView`). */
export interface ArchivedSessionView extends SessionView {
  /** Epoch SECONDS of the archive decision (same clock as `updated_at`). */
  archived_at: number
  archive_reason: string
}

/** Archive policy advertised by the daemon (host: session-store.ts). */
export interface ArchivePolicy {
  auto: boolean
  autoAfterSeconds: number
  defaultIdleSeconds: number
}

/** Full board state (host: session-store.ts). */
export interface BoardState {
  sessions: SessionView[]
  /** Absent on hosts that predate archiving; read as an empty list. */
  archived?: ArchivedSessionView[]
  archivePolicy?: ArchivePolicy | null
  streamHealth: StreamHealth
  lastReconcileAt: number | null
}

/** Body of `GET state` and of every SSE `state` event (host: routes.ts). */
export interface StateSnapshot {
  daemon: {
    state: SupervisorState
    lastPing: PingInfo | null
    /** Cause of a FAILED daemon; absent on older hosts, null when healthy. */
    failure?: DaemonFailure | null
  }
  board: BoardState
  capabilities: {
    inject: boolean
    /** Whether `dsh.dispose` can succeed now; absent on older hosts. */
    dispose?: boolean
  }
}

/** Stable, content-free host result for one timeline source consultation. */
export type TimelineSourceOutcome =
  | 'succeeded'
  | 'unavailable'
  | 'not_found'
  | 'replay_unsupported'
  | 'source_failed'

/** Current host source names; values never contain upstream error detail. */
export interface TimelineSourceOutcomes {
  liveSession: TimelineSourceOutcome
  sessionQuery: TimelineSourceOutcome
  sidecarReplay: TimelineSourceOutcome
  buffer: TimelineSourceOutcome
}

export interface TimelineSourceSummary {
  available: number
  unavailable: number
  failed: number
}

/**
 * How far back the rendered timeline can be trusted to reach.
 *
 * `volatile_only` says every durable source came up empty and the events on
 * screen are whatever the in-memory ring still holds — a bounded tail, not a
 * history. It is orthogonal to `TimelineHealth.kind`: no source has to break
 * for it to happen (a session whose transcript shape has no replay reaches it
 * with everything working), so it is reported beside the failure state rather
 * than folded into it.
 */
export type TimelineHistoryScope = 'durable' | 'volatile_only' | 'unknown'

/**
 * Browser-safe timeline availability. `unverified` is the fail-closed state
 * for malformed or newer unknown host vocabulary; raw wire values are never
 * retained, so paths, ids, and upstream errors cannot reach presentation.
 */
export type TimelineHealth =
  | Readonly<{
      kind: 'healthy'
      legacy: boolean
      summary: TimelineSourceSummary | null
      historyScope: TimelineHistoryScope
    }>
  | Readonly<{
      kind: 'partial' | 'failed'
      legacy: false
      summary: TimelineSourceSummary
      historyScope: TimelineHistoryScope
    }>
  | Readonly<{
      kind: 'unverified'
      legacy: false
      summary: null
      historyScope: 'unknown'
    }>

const TIMELINE_SOURCE_KEYS = [
  'liveSession',
  'sessionQuery',
  'sidecarReplay',
  'buffer',
] as const

const TIMELINE_SOURCE_OUTCOMES: ReadonlySet<string> = new Set([
  'succeeded',
  'unavailable',
  'not_found',
  'replay_unsupported',
  'source_failed',
])

/**
 * Mirror of the host's classification (fusion.ts `TIMELINE_FAILURE_OUTCOMES`)
 * and must move with it. `replay_unsupported` is not a failure: it says the
 * session's transcript shape has no history source, which belongs in the
 * summary's "unavailable" column next to a source that was never wired.
 */
const TIMELINE_FAILURE_OUTCOMES: ReadonlySet<TimelineSourceOutcome> = new Set([
  'source_failed',
])

/** Outcomes that contributed nothing but broke nothing. */
const TIMELINE_INERT_OUTCOMES: ReadonlySet<TimelineSourceOutcome> = new Set([
  'unavailable',
  'not_found',
  'replay_unsupported',
])

/**
 * Sources whose success means the page reaches as far back as the session
 * itself. The buffer is excluded: it is a bounded ring the host fills from
 * the live stream, so it can only ever vouch for a recent tail.
 */
const TIMELINE_DURABLE_SOURCE_KEYS = ['liveSession', 'sessionQuery', 'sidecarReplay'] as const

export const LEGACY_TIMELINE_HEALTH: TimelineHealth = Object.freeze({
  kind: 'healthy',
  legacy: true,
  summary: null,
  historyScope: 'unknown',
})

const UNVERIFIED_TIMELINE_HEALTH: TimelineHealth = Object.freeze({
  kind: 'unverified',
  legacy: false,
  summary: null,
  historyScope: 'unknown',
})

function timelineSourceOutcomes(value: unknown): TimelineSourceOutcomes | null {
  if (!isRecord(value)) return null
  const outcomes = {} as TimelineSourceOutcomes
  for (const key of TIMELINE_SOURCE_KEYS) {
    const outcome = value[key]
    if (typeof outcome !== 'string' || !TIMELINE_SOURCE_OUTCOMES.has(outcome)) return null
    outcomes[key] = outcome as TimelineSourceOutcome
  }
  return outcomes
}

/**
 * Whether the oldest event on the page is a real beginning or merely where
 * the ring happens to start. Only a page carried exclusively by the buffer is
 * `volatile_only`; an empty page has no history to misrepresent, so it needs
 * no caveat and the failure and empty states own its messaging.
 */
function timelineHistoryScope(outcomes: TimelineSourceOutcomes): TimelineHistoryScope {
  const durable = TIMELINE_DURABLE_SOURCE_KEYS.some((key) => outcomes[key] === 'succeeded')
  return !durable && outcomes.buffer === 'succeeded' ? 'volatile_only' : 'durable'
}

function timelineSourceSummary(outcomes: TimelineSourceOutcomes): TimelineSourceSummary {
  const summary = { available: 0, unavailable: 0, failed: 0 }
  for (const outcome of Object.values(outcomes)) {
    if (outcome === 'succeeded') summary.available += 1
    else if (TIMELINE_FAILURE_OUTCOMES.has(outcome)) summary.failed += 1
    else summary.unavailable += 1
  }
  return summary
}

/**
 * Validate timeline health fields on an untrusted page object.
 *
 * A page from an older host has none of the three fields and remains a
 * healthy legacy page. Any partial tuple, unknown enum, or inconsistent
 * aggregate fails closed to `unverified` without exposing the bad value.
 */
export function normalizeTimelineHealth(page: unknown): TimelineHealth {
  if (!isRecord(page)) return UNVERIFIED_TIMELINE_HEALTH
  const hasOutcomes = Object.prototype.hasOwnProperty.call(page, 'sourceOutcomes')
  const hasDegraded = Object.prototype.hasOwnProperty.call(page, 'degraded')
  const hasReason = Object.prototype.hasOwnProperty.call(page, 'reason')
  if (!hasOutcomes && !hasDegraded && !hasReason) return LEGACY_TIMELINE_HEALTH
  if (!hasOutcomes || !hasDegraded || !hasReason) return UNVERIFIED_TIMELINE_HEALTH

  const outcomes = timelineSourceOutcomes(page['sourceOutcomes'])
  const entries = page['entries']
  const degraded = page['degraded']
  const reason = page['reason']
  if (outcomes === null || !Array.isArray(entries) || typeof degraded !== 'boolean') {
    return UNVERIFIED_TIMELINE_HEALTH
  }

  const values = Object.values(outcomes)
  const failures = values.filter((outcome) => TIMELINE_FAILURE_OUTCOMES.has(outcome))
  const usable = values.filter((outcome) => !TIMELINE_INERT_OUTCOMES.has(outcome))
  const allSourcesFailed =
    entries.length === 0 &&
    usable.length > 0 &&
    usable.every((outcome) => TIMELINE_FAILURE_OUTCOMES.has(outcome))
  const summary = timelineSourceSummary(outcomes)
  const historyScope = timelineHistoryScope(outcomes)

  if (!degraded && reason === null && failures.length === 0) {
    return { kind: 'healthy', legacy: false, summary, historyScope }
  }
  if (degraded && reason === 'all_sources_failed' && allSourcesFailed) {
    return { kind: 'failed', legacy: false, summary, historyScope }
  }
  if (degraded && reason === 'partial_source_failure' && failures.length > 0 && !allSourcesFailed) {
    return { kind: 'partial', legacy: false, summary, historyScope }
  }
  return UNVERIFIED_TIMELINE_HEALTH
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

/** One archive target (host: routes.ts `ArchiveTarget`). */
export interface ArchiveTarget {
  agent: string
  sessionId: string
}

/** `POST action` body previewing a batch archive (host: routes.ts). */
export interface ArchivePreviewActionBody {
  type: 'archive.preview'
  idleSeconds: number
  statuses?: string[]
}

/** `POST action` body archiving a previewed subset (host: routes.ts). */
export interface ArchiveApplyActionBody {
  type: 'archive.apply'
  targets: ArchiveTarget[]
  /** Single-use token minted by the matching `archive.preview`. */
  token: string
}

/** `POST action` body releasing archived sessions (host: routes.ts). */
export interface ArchiveUnarchiveActionBody {
  type: 'archive.unarchive'
  targets?: ArchiveTarget[]
  all?: true
}

/** `POST action` body reading the archive registry (host: routes.ts). */
export interface ArchiveListActionBody {
  type: 'archive.list'
}

/** `POST action` body ending one live dsh session (host: routes.ts). */
export interface DshDisposeActionBody {
  type: 'dsh.dispose'
  sessionId: string
}

/**
 * Outcome of one dispose attempt. Content-free by construction: host error
 * text can name paths and prompts, so only the class of answer crosses the
 * boundary (host: dsh-dispose.ts `DisposeOutcome`).
 */
export type DisposeOutcome =
  | 'disposed'
  | 'not_found'
  | 'unsupported'
  | 'timeout'
  | 'failed'

/** `dsh.dispose` result as it reaches the browser. */
export interface DisposeResult {
  outcome: DisposeOutcome
}

/** Daemon `archive_preview` result as it reaches the browser. */
export interface ArchivePreviewResult {
  idle_seconds: number
  statuses: string[]
  candidates: SessionView[]
  count: number
  token: string
}

/** Daemon `archive_apply` result as it reaches the browser. */
export interface ArchiveApplyResult {
  count: number
  requested: number
}

/** Daemon `unarchive` result as it reaches the browser. */
export interface UnarchiveResult {
  count: number
}

/** Analysis target selector (host: routes.ts `AnalysisTargetRequest`). */
export type AnalysisTargetKind = 'session' | 'project' | 'cross-agent'

/** `POST action` body starting one analysis (host: routes.ts, M3). */
export interface AnalysisRequestActionBody {
  type: 'analysis.request'
  targetKind: AnalysisTargetKind
  /** Session id / project path; required for session and project kinds. */
  targetId?: string
  question?: string
}

/** `POST action` body asking a follow-up in a live analysis session. */
export interface AnalysisFollowupActionBody {
  type: 'analysis.followup'
  analysisSessionId: string
  question: string
}

/** `POST action` body releasing an analysis session (idempotent). */
export interface AnalysisCancelActionBody {
  type: 'analysis.cancel'
  analysisSessionId: string
}

/**
 * The action dispatcher envelope (host: routes.ts `handleAction`):
 * a `{type, ...}` union with the phase fields at the top level. M2 inject
 * phases + daemon management + the M3 `analysis.*` trio.
 */
export type ActionEnvelope =
  | PrepareActionBody
  | ExecuteActionBody
  | DaemonRetryActionBody
  | AnalysisRequestActionBody
  | AnalysisFollowupActionBody
  | AnalysisCancelActionBody
  | ArchivePreviewActionBody
  | ArchiveApplyActionBody
  | ArchiveUnarchiveActionBody
  | ArchiveListActionBody
  | DshDisposeActionBody

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

/**
 * Preview which sessions a batch archive would hide. The returned token is
 * single-use and must be handed back verbatim to {@link archiveApply}, so
 * the daemon can verify the confirmed set against what the user reviewed.
 */
export async function archivePreview(
  idleSeconds: number,
  statuses?: readonly string[],
  opts: RequestOptions = {},
): Promise<ArchivePreviewResult> {
  const body: ArchivePreviewActionBody = { type: 'archive.preview', idleSeconds }
  if (statuses !== undefined) body.statuses = [...statuses]
  return (await postAction(body, opts)) as ArchivePreviewResult
}

/** Archive a subset of one preview's candidates. */
export async function archiveApply(
  targets: readonly ArchiveTarget[],
  token: string,
  opts: RequestOptions = {},
): Promise<ArchiveApplyResult> {
  return (await postAction(
    { type: 'archive.apply', targets: [...targets], token },
    opts,
  )) as ArchiveApplyResult
}

/**
 * End one live dsh session. Unlike archiving this is irreversible and is
 * therefore never issued in bulk without an explicit opt-in, and never at
 * all for non-dsh agents (they have no supervised session to end).
 *
 * A settled attempt always resolves — including `failed` and `timeout`.
 * Only transport/gate refusals reject with {@link ApiError}, notably 501
 * `dispose_unavailable` on a host without the capability.
 */
export async function dshDispose(
  sessionId: string,
  opts: RequestOptions = {},
): Promise<DisposeResult> {
  return (await postAction(
    { type: 'dsh.dispose', sessionId },
    opts,
  )) as DisposeResult
}

/** Return archived sessions to the board. */
export async function archiveUnarchive(
  targets: readonly ArchiveTarget[] | 'all',
  opts: RequestOptions = {},
): Promise<UnarchiveResult> {
  const body: ArchiveUnarchiveActionBody =
    targets === 'all'
      ? { type: 'archive.unarchive', all: true }
      : { type: 'archive.unarchive', targets: [...targets] }
  return (await postAction(body, opts)) as UnarchiveResult
}
