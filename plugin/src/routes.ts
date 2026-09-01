/**
 * HTTP route layer for the plugin's self-registered namespace (design §4.f).
 *
 * M1 scope: three read endpoints (`GET state`, `GET session/<id>`,
 * `GET stream` SSE) plus a `POST action` placeholder behind the write gate.
 *
 * M2 scope: when an {@link InjectGatewayApi} is wired into the deps,
 * `POST action` becomes a dispatcher over a bounded (≤64 KiB) JSON body
 * `{ type: 'inject.prepare' | 'inject.execute' | 'daemon.retry', ... }`.
 * The inject types pass the write-action gate and drive the gateway's
 * two-phase confirm; `daemon.retry` is daemon management — independent of
 * the injection capability (design §6: `inject.enabled` gates injection
 * only) — so it passes guard layers 1-4 without the write gate. Without a
 * gateway the M1 placeholder contract (write gate, then 501) is preserved.
 * Message bodies never reach the route log (S8), and `outcome: 'unknown'`
 * is answered 200 as a terminal "do not retry" — this layer never re-fires
 * the gateway on it (S6).
 *
 * M3 scope: when a {@link FusionApi} is wired into the deps, the read
 * surface widens (design §4.e / §5, all GET, guard layers 1-4, no write
 * gate):
 * - `GET session/<id>` upgrades its `timeline: null` placeholder to the
 *   newest fused timeline page plus a `nextCursor` pagination token, and
 *   gains a `unified` row (dsh-live sessions the sidecar has not seen
 *   yet resolve here instead of 404).
 * - `GET session/<id>/timeline?cursor=&limit=` pages backward through
 *   history (fusion pulls dsh live/cold logs, the daemon `replay` op and
 *   the bounded event ring on demand).
 * - `GET lineage/<id>` → fusion.getLineage; ALWAYS 200 with
 *   `{available:false, reason}` when sessionQuery is absent or the trace
 *   fails (degradation is data, not an error; design §4.e.4).
 * - `GET search?q=&project=&limit=` → fusion.searchSessions (`mode`
 *   reports 'full-text' vs the 'filter-only' degradation); a
 *   project-only query filters the unified list without the engine.
 * - `GET projects` → fusion.getProjectGroups.
 * Without a fusion the four new paths answer 501 `fusion_not_wired` and
 * `GET session/<id>` keeps the M1 placeholder contract. The SSE stream
 * contract is untouched: live updates stay full-snapshot frames and the
 * client pairs them with timeline pagination (no per-session filter —
 * recorded as not needed for M3).
 *
 * M3 analysis scope (design §4.e.3 / §7-B): when an {@link AnalysisApi} is
 * wired into the deps, `POST action` also dispatches
 * `{ type: 'analysis.request' | 'analysis.followup' | 'analysis.cancel' }`.
 * These pass guard layers 1-4 and then a WRITE GATE OF THEIR OWN —
 * `deps.analysisEnabled` (live `analysis.enabled`, default false), parallel
 * to and independent of the guard's inject gate — closed (or absent) means
 * 403 `analysis_disabled`. Past the gate, a missing/unavailable analysis
 * surface (agents-less composition) answers 501 `analysis_unavailable`
 * (honest degradation, never a crash). `analysis.request` additionally
 * pre-checks that an analysis model is resolvable — no model anywhere
 * answers 403 `analysis_model_unconfigured` before any session is created
 * (A-1) — then resolves its target through the wiring's fusion-backed
 * input adapter (unknown target
 * → 404 `target_not_found`) and drives the engine; result error codes map
 * to `analysis_disabled` 403, `too_many_active` 429, `timeout` 504,
 * `create_failed` 502 — while `cancelled` stays 200 (a terminal fact about
 * the analysis session, carried in the outcome, not a transport failure).
 * Analyzed content (summaries, questions, model replies) never reaches the
 * route log (S8). Note the M1 no-gateway placeholder still wins: analysis
 * dispatch lives inside the gateway-backed action handler.
 *
 * Timeline cursors are opaque `<seq|'-'>~<epoch-ms>` tokens minted by
 * {@link encodeCursor}; clients must round-trip them verbatim.
 *
 * Contract (verified against the dsh source, not docs): the webServer
 * prefix-route handler is plain `node:http` —
 * `(req: IncomingMessage, res: ServerResponse) => void | Promise<void>`,
 * and it owns the full response lifecycle, so SSE long-holds are legal
 * (`packages/host/webserver/src/index.ts:42-48`). A `prefix` route `p`
 * receives `p` and `p/<anything>` (`:38`, `:262`), and handler rejections
 * are caught by the carrier (logged, answered 400; `:181-194`). Internal
 * dispatch via `new URL(req.url, ...)` follows the better-sidebar
 * precedent (`DSH-better-sidebar/src/index.ts:665-694`).
 *
 * Like guard/store/supervisor, this module imports nothing from
 * cordis/dsh: the plugin entry (index.ts) wires it via
 * `ctx.webServer.register({ kind: 'prefix', path: API_PREFIX, handler })`
 * and puts `dispose()` inside a `ctx.effect` disposer.
 *
 * @module
 */

import type { IncomingMessage, ServerResponse } from 'node:http'
import {
  guardRequest,
  guardWriteAction,
  type GuardOptions,
  type GuardVerdict,
} from './guard.ts'
import type { AnalysisEngine, AnalysisInput, AnalysisResult } from './analysis.ts'
import type { DshDisposeApi } from './dsh-dispose.ts'
import type { FusionQuery, TimelineCursor, TimelinePage } from './fusion.ts'
import type { InjectGateway } from './inject-gateway.ts'
import type { BoardState, SessionStore } from './session-store.ts'
import type { DaemonSupervisor, PingInfo, SupervisorState } from './supervisor.ts'

/** dsh webServer route handler shape (see module doc for the evidence). */
export type WebRouteHandler = (
  req: IncomingMessage,
  res: ServerResponse,
) => void | Promise<void>

/** Route namespace, per the `/plugins/<package>/` convention (design §4.f). */
export const API_PREFIX = '/plugins/agent-sidecar/api'

/**
 * The slice of {@link InjectGateway} the routes drive. A `Pick` keeps the
 * dependency structural (the class has private state), so the real gateway
 * and plain-object test fakes are equally assignable.
 */
export type InjectGatewayApi = Pick<InjectGateway, 'prepare' | 'execute'> &
  Partial<Pick<InjectGateway, 'bindTargetVerifier'>>

/**
 * The slice of {@link FusionQuery} the M3 read endpoints drive. Structural
 * for the same reason as {@link InjectGatewayApi}: the entry wires the real
 * fusion (possibly behind a holder facade) and tests wire plain objects.
 */
export type FusionApi = Pick<
  FusionQuery,
  | 'getUnifiedSessions'
  | 'getSessionTimeline'
  | 'getProjectGroups'
  | 'getLineage'
  | 'searchSessions'
  | 'getCapabilities'
>

/** The slice of {@link AnalysisEngine} the analysis actions drive. */
export type AnalysisEngineApi = Pick<AnalysisEngine, 'request' | 'followup' | 'cancel'>

/** One `(agent, sessionId)` archive target carried by an action envelope. */
export interface ArchiveTarget {
  agent: string
  sessionId: string
}

/**
 * The daemon archive ops the `archive.*` actions drive (host: bridge.ts
 * {@link SidecarSocketClient}). Structural, like the other route deps.
 *
 * Archiving is observation-level and fully reversible — it hides a session
 * from this board without touching the vendor transcript or any process —
 * so, like `daemon.retry`, these actions pass guard layers 1-4 WITHOUT the
 * inject write gate: `inject.enabled` gates agent-state mutation, which
 * archiving is not (design §6).
 */
export interface ArchiveApi {
  preview(idleSeconds: number, statuses?: readonly string[]): Promise<unknown>
  apply(targets: readonly ArchiveTarget[], token: string): Promise<unknown>
  unarchive(targets: readonly ArchiveTarget[] | 'all'): Promise<unknown>
  list(): Promise<unknown>
}

/** Target selector carried by one `analysis.request` action envelope. */
export interface AnalysisTargetRequest {
  targetKind: 'session' | 'project' | 'cross-agent'
  /** Session id / project path; required for session and project kinds. */
  targetId?: string
  /** Optional user question folded into the analysis input by the adapter. */
  question?: string
}

/**
 * M3 analysis wiring handed in by the entry: the engine plus the adapter
 * that assembles a bounded {@link AnalysisInput} from fusion data. The
 * routes stay dumb — target resolution and summary assembly are the
 * wiring's job, request/result vocabulary is the engine's.
 */
export interface AnalysisApi {
  engine: AnalysisEngineApi
  /**
   * Assemble the bounded analysis input for a target. `null` means the
   * target is unknown to fusion (the routes answer 404 `target_not_found`).
   */
  buildInput(req: AnalysisTargetRequest): Promise<AnalysisInput | null>
  /**
   * Whether the underlying `ctx.agents` service is bound. `false` answers
   * 501 `analysis_unavailable` before touching the engine (agents-less
   * composition — honest degradation).
   */
  available(): boolean
  /**
   * Whether an analysis model is resolvable (explicit analysis.provider/
   * model config, or the host's default model selection). `false` answers
   * 403 `analysis_model_unconfigured` on `analysis.request` BEFORE the
   * engine creates a session — honest pre-rejection instead of a modelless
   * agent completing with an empty summary (A-1). Followup/cancel are not
   * gated: an established analysis session carries its model already.
   * Optional: an absent probe skips the pre-check (the create adapter
   * still fails honestly as `create_failed`).
   */
  modelConfigured?(): boolean
}

/** Everything the route layer consumes; all live objects, none owned here. */
export interface RoutesDeps {
  store: SessionStore
  supervisor: DaemonSupervisor
  /** Live `inject.enabled` reader shared with the guard's write gate. */
  guardOptions: GuardOptions
  /**
   * M2 injection gateway. When absent (M1 wiring, injection not assembled)
   * `POST action` keeps the placeholder contract: write gate, then 501.
   */
  injectGateway?: InjectGatewayApi
  /**
   * M3 fusion query surface. When absent the timeline/lineage/search/
   * projects endpoints answer 501 `fusion_not_wired` and `GET session/<id>`
   * keeps the M1 placeholder contract.
   */
  fusion?: FusionApi
  /**
   * Live `analysis.enabled` reader — the analysis write gate, parallel to
   * (and independent of) the guard's inject gate. Absent reads as CLOSED
   * (fail-closed): every `analysis.*` action answers 403 `analysis_disabled`.
   */
  analysisEnabled?: () => boolean
  /**
   * M3 analysis surface. When absent (analysis not assembled), `analysis.*`
   * actions that pass the gate answer 501 `analysis_unavailable`.
   */
  analysis?: AnalysisApi
  /**
   * dsh session dispose. When absent — or bound to a host whose sessions
   * service has no dispose member — `dsh.dispose` answers 501
   * `dispose_unavailable` and `GET state` reports the capability as false.
   */
  dispose?: DshDisposeApi
  /**
   * Daemon archive ops. When absent, `archive.*` actions answer 501
   * `archive_unavailable`.
   */
  archive?: ArchiveApi
  log(level: 'info' | 'warn' | 'error', msg: string, meta?: object): void
}

export interface RoutesOptions {
  /** Concurrent SSE connection cap; extra connects get 503. Default 8. */
  maxSseClients?: number
  /** SSE comment-frame heartbeat cadence. Default 15000. */
  sseHeartbeatMs?: number
  /**
   * Per-connection bound on frames queued behind a slow socket; exceeding
   * it destroys that connection (the client reconnects and resnapshots).
   * Default 256.
   */
  sseBufferLimit?: number
}

/** Body of `GET state` and of every SSE `state` event (full snapshot). */
export interface StateSnapshot {
  daemon: { state: SupervisorState; lastPing: PingInfo | null }
  board: BoardState
  capabilities: {
    inject: boolean
    /**
     * Whether `dsh.dispose` can succeed right now: a bound sessions
     * service that exposes dispose, behind the same write gate. False
     * hides the control instead of offering a doomed action.
     */
    dispose: boolean
  }
}

/** What `createRoutes` hands back to the plugin entry. */
export interface Routes {
  /** Mount as the `prefix` route handler for {@link API_PREFIX}. */
  handle: (req: IncomingMessage, res: ServerResponse) => Promise<void>
  /** Close all SSE connections, unsubscribe, clear timers. Idempotent. */
  dispose(): void
}

const DEFAULT_MAX_SSE_CLIENTS = 8
const DEFAULT_SSE_HEARTBEAT_MS = 15_000
const DEFAULT_SSE_BUFFER_LIMIT = 256

const HEARTBEAT_FRAME = ': hb\n\n'

/** Bound on the `POST action` JSON body (message cap is 16 KiB + envelope). */
export const MAX_ACTION_BODY_BYTES = 64 * 1024

/** `inject.prepare` rejection code → HTTP status (task spec mapping). */
const PREPARE_ERROR_STATUS: Readonly<Record<string, number>> = {
  inject_disabled: 403,
  invalid_message: 422,
  target_not_found: 404,
  target_changed: 409,
  target_dead: 409,
  working_session: 409,
  dead_session: 409,
  child_session: 422,
  remote_session: 422,
  invalid_session: 422,
  dsh_model_unconfigured: 409,
  dsh_preset_unsupported: 409,
  dsh_agents_unavailable: 502,
  executor_error: 502,
  too_many_pending: 429,
  // Issued at prepare since M2 review F-6 (no injection path for this
  // agent); same 422 the execute-side defense-in-depth check maps to.
  unsupported_agent: 422,
}

/**
 * `inject.execute` failed-outcome code → HTTP status. Unlisted codes
 * (executor-native vocab) and codeless failures fall back to 502.
 */
const EXECUTE_ERROR_STATUS: Readonly<Record<string, number>> = {
  inject_disabled: 403,
  target_not_found: 404,
  target_changed: 409,
  target_dead: 409,
  working_session: 409,
  dead_session: 409,
  child_session: 422,
  remote_session: 422,
  invalid_session: 422,
  token_missing: 401,
  token_expired: 401,
  token_reused: 409,
  token_mismatch: 409,
  dsh_model_unconfigured: 409,
  dsh_preset_unsupported: 409,
  dsh_agents_unavailable: 502,
  unsupported_agent: 422,
  executor_error: 502,
}

/**
 * Analysis-engine error code → HTTP status (task spec mapping). `cancelled`
 * stays 200: it is a terminal fact about the analysis session carried in
 * the result outcome, not a transport failure. Unknown codes fall back to
 * 502 like the execute map does.
 */
const ANALYSIS_ERROR_STATUS: Readonly<Record<string, number>> = {
  analysis_disabled: 403,
  too_many_active: 429,
  timeout: 504,
  create_failed: 502,
  cancelled: 200,
}

const ANALYSIS_ACTION_TYPES = new Set([
  'analysis.request',
  'analysis.followup',
  'analysis.cancel',
])

const ARCHIVE_ACTION_TYPES = new Set([
  'archive.preview',
  'archive.apply',
  'archive.unarchive',
  'archive.list',
])

/**
 * Daemon archive error code → HTTP status. Transport codes and unknown
 * daemon vocabulary fall back to 502 (the daemon spoke, we could not).
 */
const ARCHIVE_ERROR_STATUS: Readonly<Record<string, number>> = {
  invalid_request: 400,
  invalid_token: 409,
  archive_corrupt: 500,
  archive_error: 500,
  unknown_op: 501,
  timeout: 504,
  connection_failed: 503,
  connection_closed: 503,
}

/** Upper bound on targets one `archive.apply`/`archive.unarchive` may carry. */
const MAX_ARCHIVE_TARGETS = 500

/** Parse `[{agent, sessionId}]`, or null when any entry is malformed. */
function parseArchiveTargets(value: unknown): ArchiveTarget[] | null {
  if (!Array.isArray(value) || value.length === 0) return null
  if (value.length > MAX_ARCHIVE_TARGETS) return null
  const targets: ArchiveTarget[] = []
  for (const raw of value) {
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
    const { agent, sessionId } = raw as Record<string, unknown>
    if (typeof agent !== 'string' || agent === '') return null
    if (typeof sessionId !== 'string' || sessionId === '') return null
    targets.push({ agent, sessionId })
  }
  return targets
}

interface SseClient {
  res: ServerResponse
  /** Frames queued while the socket is backpressured. */
  pending: string[]
  /** True after a `res.write` returned false, until the next 'drain'. */
  blocked: boolean
  closed: boolean
  heartbeat: ReturnType<typeof setInterval> | null
}

function writeJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
  })
  res.end(JSON.stringify(body))
}

function writeMethodNotAllowed(res: ServerResponse, allow: string): void {
  res.writeHead(405, {
    allow,
    'content-type': 'application/json; charset=utf-8',
  })
  res.end(JSON.stringify({ reason: 'method_not_allowed' }))
}

/**
 * Path inside the namespace ('' for the bare prefix), or null when the
 * request is outside {@link API_PREFIX} or the URL is unparsable. The
 * carrier already parsed the same string to match the route, so the null
 * arms only matter when `handle` is exercised directly.
 */
function subpathOf(rawUrl: string | undefined): string | null {
  let pathname: string
  try {
    pathname = new URL(rawUrl ?? '/', 'http://dsh.internal').pathname
  } catch {
    return null
  }
  if (pathname === API_PREFIX) return ''
  if (pathname.startsWith(`${API_PREFIX}/`)) return pathname.slice(API_PREFIX.length + 1)
  return null
}

/** Query string of the request (empty params when the URL is unparsable). */
function queryOf(rawUrl: string | undefined): URLSearchParams {
  try {
    return new URL(rawUrl ?? '/', 'http://dsh.internal').searchParams
  } catch {
    return new URLSearchParams()
  }
}

/**
 * Timeline pagination token: `<seq|'-'>~<epoch-ms>`. Deliberately not the
 * raw JSON cursor object so the query-string round-trip stays trivial and
 * the wire shape is decoupled from fusion's internal cursor type.
 */
function encodeCursor(cursor: TimelineCursor): string {
  return `${cursor.seq === null ? '-' : cursor.seq}~${cursor.ts}`
}

function decodeCursor(raw: string): TimelineCursor | null {
  const sep = raw.indexOf('~')
  if (sep <= 0 || sep === raw.length - 1) return null
  const seqPart = raw.slice(0, sep)
  const tsPart = raw.slice(sep + 1)
  const ts = Number(tsPart)
  if (!Number.isInteger(ts) || ts < 0) return null
  if (seqPart === '-') return { seq: null, ts }
  const seq = Number(seqPart)
  if (!Number.isInteger(seq) || seq < 0) return null
  return { seq, ts }
}

/** Bound on caller-supplied page sizes (timeline entries / search hits). */
const MAX_PAGE_LIMIT = 500

/** Search result bound when the caller supplies no limit (fusion default). */
const DEFAULT_SEARCH_ROUTE_LIMIT = 50

/** Match fusion's project correlation key: strip trailing slashes (keep `/`). */
function normalizeProjectKey(project: string): string {
  if (project.length > 1 && project.endsWith('/')) {
    const stripped = project.replace(/\/+$/, '')
    return stripped === '' ? '/' : stripped
  }
  return project
}

/**
 * Parse an optional positive-integer query param bounded by
 * {@link MAX_PAGE_LIMIT}. `undefined` when absent, `null` when invalid.
 */
function parseLimit(params: URLSearchParams, name: string): number | undefined | null {
  const raw = params.get(name)
  if (raw === null || raw === '') return undefined
  const value = Number(raw)
  if (!Number.isInteger(value) || value < 1 || value > MAX_PAGE_LIMIT) return null
  return value
}

/** JSON wire shape of one timeline page (adds the encoded `nextCursor`). */
function timelineBody(page: TimelinePage): Record<string, unknown> {
  return {
    sessionId: page.sessionId,
    entries: page.entries,
    cursor: page.cursor,
    nextCursor: page.cursor === null ? null : encodeCursor(page.cursor),
    sources: page.sources,
    sourceOutcomes: page.sourceOutcomes,
    degraded: page.degraded,
    reason: page.reason,
  }
}

/** A successful source consultation is positive evidence that the target exists. */
function timelineConfirmsTarget(page: TimelinePage): boolean {
  return Object.values(page.sourceOutcomes).some((outcome) => outcome === 'succeeded')
}

/** `event: <name>` + single-line JSON data (JSON.stringify never emits raw newlines). */
function sseFrame(event: string, data: string): string {
  return `event: ${event}\ndata: ${data}\n\n`
}

/** Outcome of the bounded body read for `POST action`. */
type BodyRead = { kind: 'ok'; text: string } | { kind: 'too_large' } | { kind: 'error' }

/**
 * Read the request body up to {@link MAX_ACTION_BODY_BYTES}. On overflow the
 * promise settles immediately ('too_large') while the rest of the stream
 * keeps draining, so the keep-alive connection is left in a clean state.
 */
function readActionBody(req: IncomingMessage): Promise<BodyRead> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = []
    let size = 0
    let settled = false
    const settle = (result: BodyRead): void => {
      if (settled) return
      settled = true
      resolve(result)
    }
    req.on('data', (chunk: Buffer | string) => {
      if (settled) return // overflow already answered; keep draining
      const buf = typeof chunk === 'string' ? Buffer.from(chunk, 'utf8') : chunk
      size += buf.length
      if (size > MAX_ACTION_BODY_BYTES) {
        settle({ kind: 'too_large' })
        return
      }
      chunks.push(buf)
    })
    req.on('end', () => settle({ kind: 'ok', text: Buffer.concat(chunks).toString('utf8') }))
    req.on('error', () => settle({ kind: 'error' }))
  })
}

/**
 * Build the M1 route surface. All state lives in the returned closure;
 * multiple instances never share anything.
 */
export function createRoutes(deps: RoutesDeps, opts: RoutesOptions = {}): Routes {
  const maxSseClients = opts.maxSseClients ?? DEFAULT_MAX_SSE_CLIENTS
  const sseHeartbeatMs = opts.sseHeartbeatMs ?? DEFAULT_SSE_HEARTBEAT_MS
  const sseBufferLimit = opts.sseBufferLimit ?? DEFAULT_SSE_BUFFER_LIMIT

  // Route assembly owns both objects, so bind the gateway to the same
  // sanitized SessionStore projection served by state/detail/SSE. This
  // supersedes the legacy status-only verifier without exposing raw rows.
  deps.injectGateway?.bindTargetVerifier?.(async (target) => {
    const view = deps.store.getSession(target.agent, target.sessionId)
    if (view === null) return null
    return {
      agent: view.agent,
      sessionId: view.session_id,
      status: view.status,
      title: view.title,
      project: view.project,
      inject_eligibility: view.inject_eligibility,
    }
  })

  const clients = new Set<SseClient>()
  let disposed = false

  const buildSnapshot = (): StateSnapshot => ({
    daemon: { state: deps.supervisor.state, lastPing: deps.supervisor.lastPing },
    board: deps.store.getBoardState(),
    capabilities: {
      inject: deps.guardOptions.allowWriteActions(),
      dispose:
        deps.guardOptions.allowWriteActions() &&
        deps.dispose !== undefined &&
        deps.dispose.available(),
    },
  })

  // ------------------------------------------------------------------ SSE

  const cleanupClient = (client: SseClient): void => {
    if (client.closed) return
    client.closed = true
    if (client.heartbeat !== null) clearInterval(client.heartbeat)
    client.heartbeat = null
    client.pending.length = 0
    clients.delete(client)
    deps.log('info', 'sse client disconnected', { clients: clients.size })
  }

  const dropClient = (client: SseClient, reason: string): void => {
    deps.log('warn', 'sse client dropped', {
      reason,
      pending: client.pending.length,
      limit: sseBufferLimit,
    })
    cleanupClient(client)
    client.res.destroy()
  }

  const push = (client: SseClient, frame: string): void => {
    if (client.closed) return
    if (client.blocked) {
      client.pending.push(frame)
      if (client.pending.length > sseBufferLimit) {
        dropClient(client, 'buffer_overflow')
      }
      return
    }
    if (!client.res.write(frame)) client.blocked = true
  }

  const flush = (client: SseClient): void => {
    if (client.closed) return
    client.blocked = false
    while (!client.blocked) {
      const frame = client.pending.shift()
      if (frame === undefined) return
      if (!client.res.write(frame)) client.blocked = true
    }
  }

  const acceptStream = (res: ServerResponse): void => {
    if (clients.size >= maxSseClients) {
      deps.log('warn', 'sse connection rejected: client limit reached', {
        max: maxSseClients,
      })
      writeJson(res, 503, { reason: 'too_many_stream_clients' })
      return
    }
    res.writeHead(200, {
      'content-type': 'text/event-stream',
      'cache-control': 'no-cache',
      connection: 'keep-alive',
    })
    const client: SseClient = {
      res,
      pending: [],
      blocked: false,
      closed: false,
      heartbeat: null,
    }
    clients.add(client)
    // 'close' fires for both client aborts and our own end()/destroy().
    res.on('close', () => cleanupClient(client))
    res.on('drain', () => flush(client))
    client.heartbeat = setInterval(() => push(client, HEARTBEAT_FRAME), sseHeartbeatMs)
    deps.log('info', 'sse client connected', { clients: clients.size })
    push(client, sseFrame('state', JSON.stringify(buildSnapshot())))
  }

  /** One change → one full snapshot frame to every client (M1 granularity). */
  const onMutation = (): void => {
    if (disposed || clients.size === 0) return
    const frame = sseFrame('state', JSON.stringify(buildSnapshot()))
    for (const client of [...clients]) push(client, frame)
  }

  const unsubscribes: Array<() => void> = [
    deps.store.onChange(onMutation),
    deps.supervisor.onStateChange(onMutation),
  ]

  // --------------------------------------------------------------- routes

  /** Decoded session id, or null (already answered 404) on a bad escape. */
  const decodeId = (res: ServerResponse, rawId: string): string | null => {
    try {
      const id = decodeURIComponent(rawId)
      if (id !== '') return id
    } catch {
      // fall through to the 404
    }
    writeJson(res, 404, { reason: 'session_not_found' })
    return null
  }

  const handleSession = async (res: ServerResponse, rawId: string): Promise<void> => {
    const id = decodeId(res, rawId)
    if (id === null) return
    const view = deps.store.getSessionDetail(id) ?? undefined
    const fusion = deps.fusion
    if (fusion === undefined) {
      // M1 contract preserved: detail == card data, timeline placeholder.
      if (view === undefined) {
        writeJson(res, 404, { reason: 'session_not_found' })
        return
      }
      writeJson(res, 200, {
        session: view,
        timeline: null,
        timelineNote: 'timeline_not_available_until_m3',
      })
      return
    }
    // M3: the unified view also resolves dsh-live sessions the sidecar has
    // not observed on disk yet, so those answer 200 instead of 404.
    let unified = fusion.getUnifiedSessions().find((s) => s.sessionId === id) ?? null
    const page = await fusion.getSessionTimeline(id)
    // A direct live-session lookup during the timeline pull may have seeded a
    // session that predated the fusion subscription; refresh without scanning.
    if (unified === null) {
      unified = fusion.getUnifiedSessions().find((s) => s.sessionId === id) ?? null
    }
    if (view === undefined && unified === null && !timelineConfirmsTarget(page)) {
      writeJson(res, 404, { reason: 'session_not_found' })
      return
    }
    writeJson(res, 200, {
      session: view ?? null,
      unified,
      timeline: timelineBody(page),
    })
  }

  const handleTimeline = async (
    res: ServerResponse,
    rawId: string,
    params: URLSearchParams,
  ): Promise<void> => {
    const fusion = deps.fusion
    if (fusion === undefined) {
      writeJson(res, 501, { reason: 'fusion_not_wired' })
      return
    }
    const id = decodeId(res, rawId)
    if (id === null) return
    const rawCursor = params.get('cursor')
    let before: TimelineCursor | null = null
    if (rawCursor !== null && rawCursor !== '') {
      before = decodeCursor(rawCursor)
      if (before === null) {
        writeJson(res, 400, { reason: 'invalid_cursor' })
        return
      }
    }
    const limit = parseLimit(params, 'limit')
    if (limit === null) {
      writeJson(res, 400, { reason: 'invalid_limit' })
      return
    }
    const page = await fusion.getSessionTimeline(id, { before, limit })
    // Board/fusion-known sessions stay 200 even when every usable source
    // failed, so an empty degraded page cannot masquerade as target absence.
    if (!timelineConfirmsTarget(page)) {
      const known =
        deps.store.getBoardState().sessions.some((s) => s.session_id === id) ||
        fusion.getUnifiedSessions().some((s) => s.sessionId === id)
      if (!known) {
        writeJson(res, 404, { reason: 'session_not_found' })
        return
      }
    }
    writeJson(res, 200, timelineBody(page))
  }

  const handleLineage = async (res: ServerResponse, rawId: string): Promise<void> => {
    const fusion = deps.fusion
    if (fusion === undefined) {
      writeJson(res, 501, { reason: 'fusion_not_wired' })
      return
    }
    const id = decodeId(res, rawId)
    if (id === null) return
    // Degradation (sessionQuery absent / trace failed) is DATA, not an
    // error: always 200 with {available, trace, reason} (design §4.e.4).
    writeJson(res, 200, await fusion.getLineage(id))
  }

  const handleSearch = async (res: ServerResponse, params: URLSearchParams): Promise<void> => {
    const fusion = deps.fusion
    if (fusion === undefined) {
      writeJson(res, 501, { reason: 'fusion_not_wired' })
      return
    }
    const query = (params.get('q') ?? '').trim()
    const project = (params.get('project') ?? '').trim()
    if (query === '' && project === '') {
      writeJson(res, 400, {
        reason: 'invalid_request',
        detail: 'search needs q= (text query) and/or project= (project filter)',
      })
      return
    }
    const limit = parseLimit(params, 'limit')
    if (limit === null) {
      writeJson(res, 400, { reason: 'invalid_limit' })
      return
    }
    let mode: 'full-text' | 'filter-only'
    let items: Array<{ session: { project: string }; matchedBy: string; snippet: string | null }>
    if (query !== '') {
      const result = await fusion.searchSessions(query, limit === undefined ? {} : { limit })
      mode = result.mode
      items = result.items
    } else {
      // Project-only search: a plain filter over the unified view.
      mode = 'filter-only'
      items = fusion
        .getUnifiedSessions()
        .map((session) => ({ session, matchedBy: 'project', snippet: null }))
    }
    if (project !== '') {
      // Exact-path filter after trailing-slash normalization (`project` is
      // the group key handed out by GET projects).
      const wanted = normalizeProjectKey(project)
      items = items.filter((item) => normalizeProjectKey(item.session.project) === wanted)
    }
    items = items.slice(0, limit ?? DEFAULT_SEARCH_ROUTE_LIMIT)
    writeJson(res, 200, {
      mode,
      query,
      project: project === '' ? null : project,
      items,
    })
  }

  const handleProjects = (res: ServerResponse): void => {
    const fusion = deps.fusion
    if (fusion === undefined) {
      writeJson(res, 501, { reason: 'fusion_not_wired' })
      return
    }
    writeJson(res, 200, { groups: fusion.getProjectGroups() })
  }

  // -------------------------------------------------------------- actions

  /**
   * Route-log discipline (S8): only the action type, status and vocabulary
   * codes — never the message body, preview, or gateway detail text.
   */
  const logAction = (type: string, status: number, meta: object = {}): void => {
    deps.log('info', 'action handled', { type, status, ...meta })
  }

  const handlePrepare = async (
    gateway: InjectGatewayApi,
    envelope: Record<string, unknown>,
    res: ServerResponse,
  ): Promise<void> => {
    const rawTarget = envelope.target
    const targetObj =
      typeof rawTarget === 'object' && rawTarget !== null
        ? (rawTarget as Record<string, unknown>)
        : undefined
    const agent = targetObj?.agent
    const sessionId = targetObj?.sessionId
    const mode = envelope.mode
    const message = envelope.message
    if (
      typeof agent !== 'string' ||
      typeof sessionId !== 'string' ||
      (mode !== 'queue' && mode !== 'steer') ||
      typeof message !== 'string'
    ) {
      logAction('inject.prepare', 400, { reason: 'invalid_request' })
      writeJson(res, 400, {
        reason: 'invalid_request',
        detail: 'inject.prepare needs target{agent,sessionId}, mode queue|steer, and a string message',
      })
      return
    }
    const result = await gateway.prepare({ target: { agent, sessionId }, mode, message })
    if (result.ok) {
      logAction('inject.prepare', 200, { requestId: result.requestId })
      writeJson(res, 200, {
        requestId: result.requestId,
        confirmToken: result.confirmToken,
        plan: result.plan,
        expiresAt: result.expiresAt,
      })
      return
    }
    const status = PREPARE_ERROR_STATUS[result.errorCode] ?? 400
    logAction('inject.prepare', status, { errorCode: result.errorCode })
    writeJson(res, status, {
      reason: result.errorCode,
      ...(result.detail !== undefined ? { detail: result.detail } : {}),
    })
  }

  const handleExecute = async (
    gateway: InjectGatewayApi,
    envelope: Record<string, unknown>,
    res: ServerResponse,
  ): Promise<void> => {
    const { requestId, confirmToken, message } = envelope
    if (
      typeof requestId !== 'string' ||
      typeof confirmToken !== 'string' ||
      typeof message !== 'string'
    ) {
      logAction('inject.execute', 400, { reason: 'invalid_request' })
      writeJson(res, 400, {
        reason: 'invalid_request',
        detail: 'inject.execute needs string requestId, confirmToken and message',
      })
      return
    }
    // Exactly one gateway dispatch per HTTP request. `outcome: 'unknown'` is
    // answered 200 as a terminal "do not retry" — never re-fired here (S6).
    const result = await gateway.execute({ requestId, confirmToken, message })
    const status =
      result.outcome === 'failed'
        ? (EXECUTE_ERROR_STATUS[result.errorCode ?? ''] ?? 502)
        : 200
    logAction('inject.execute', status, {
      outcome: result.outcome,
      ...(result.errorCode !== undefined ? { errorCode: result.errorCode } : {}),
      ...(result.replayed !== undefined ? { replayed: result.replayed } : {}),
    })
    writeJson(res, status, result)
  }

  // ------------------------------------------------------ analysis actions

  /**
   * Answer one engine result: status per {@link ANALYSIS_ERROR_STATUS},
   * body is the result verbatim (it already carries outcome /
   * analysisSessionId / summary / truncated / disclaimer). The log line
   * keeps only outcome/codes/ids — never summaries or questions (S8).
   */
  const respondAnalysisResult = (
    type: string,
    res: ServerResponse,
    result: AnalysisResult,
  ): void => {
    const status =
      result.errorCode !== undefined
        ? (ANALYSIS_ERROR_STATUS[result.errorCode] ?? 502)
        : 200
    logAction(type, status, {
      outcome: result.outcome,
      ...(result.errorCode !== undefined ? { errorCode: result.errorCode } : {}),
      ...(result.analysisSessionId !== undefined
        ? { analysisSessionId: result.analysisSessionId }
        : {}),
      ...(result.truncated ? { truncated: true } : {}),
    })
    writeJson(res, status, result)
  }

  const rejectInvalidAnalysis = (type: string, res: ServerResponse, detail: string): void => {
    logAction(type, 400, { reason: 'invalid_request' })
    writeJson(res, 400, { reason: 'invalid_request', detail })
  }

  const handleAnalysisRequest = async (
    analysis: AnalysisApi,
    envelope: Record<string, unknown>,
    res: ServerResponse,
  ): Promise<void> => {
    const { targetKind, targetId, question } = envelope
    if (
      (targetKind !== 'session' && targetKind !== 'project' && targetKind !== 'cross-agent') ||
      (targetId !== undefined && typeof targetId !== 'string') ||
      (question !== undefined && typeof question !== 'string')
    ) {
      rejectInvalidAnalysis(
        'analysis.request',
        res,
        'analysis.request needs targetKind session|project|cross-agent, optional string targetId and question',
      )
      return
    }
    if ((targetKind === 'session' || targetKind === 'project') && (targetId === undefined || targetId === '')) {
      rejectInvalidAnalysis(
        'analysis.request',
        res,
        `analysis.request with targetKind ${targetKind} needs a non-empty targetId`,
      )
      return
    }
    const input = await analysis.buildInput({
      targetKind,
      ...(targetId !== undefined ? { targetId } : {}),
      ...(question !== undefined ? { question } : {}),
    })
    if (input === null) {
      logAction('analysis.request', 404, { reason: 'target_not_found', targetKind })
      writeJson(res, 404, { reason: 'target_not_found' })
      return
    }
    respondAnalysisResult('analysis.request', res, await analysis.engine.request(input))
  }

  const handleAnalysisFollowup = async (
    analysis: AnalysisApi,
    envelope: Record<string, unknown>,
    res: ServerResponse,
  ): Promise<void> => {
    const { analysisSessionId, question } = envelope
    if (
      typeof analysisSessionId !== 'string' ||
      analysisSessionId === '' ||
      typeof question !== 'string' ||
      question === ''
    ) {
      rejectInvalidAnalysis(
        'analysis.followup',
        res,
        'analysis.followup needs non-empty string analysisSessionId and question',
      )
      return
    }
    respondAnalysisResult(
      'analysis.followup',
      res,
      await analysis.engine.followup(analysisSessionId, question),
    )
  }

  const handleAnalysisCancel = async (
    analysis: AnalysisApi,
    envelope: Record<string, unknown>,
    res: ServerResponse,
  ): Promise<void> => {
    const { analysisSessionId } = envelope
    if (typeof analysisSessionId !== 'string' || analysisSessionId === '') {
      rejectInvalidAnalysis(
        'analysis.cancel',
        res,
        'analysis.cancel needs a non-empty string analysisSessionId',
      )
      return
    }
    // Idempotent by engine contract: an unknown id is a logged no-op.
    await analysis.engine.cancel(analysisSessionId)
    logAction('analysis.cancel', 200, { analysisSessionId })
    writeJson(res, 200, { ok: true, analysisSessionId })
  }

  // ------------------------------------------------------- archive actions

  /**
   * Run one daemon archive op and answer it. A coded daemon/transport
   * failure becomes an honest status from {@link ARCHIVE_ERROR_STATUS};
   * session titles and ids stay out of the route log (S8).
   */
  const runArchiveOp = async (
    type: string,
    res: ServerResponse,
    op: () => Promise<unknown>,
  ): Promise<void> => {
    try {
      const result = await op()
      logAction(type, 200)
      writeJson(res, 200, result)
    } catch (err) {
      const code = typeof (err as { code?: unknown })?.code === 'string'
        ? (err as { code: string }).code
        : 'archive_failed'
      const status = ARCHIVE_ERROR_STATUS[code] ?? 502
      logAction(type, status, { errorCode: code })
      writeJson(res, status, { reason: code })
    }
  }

  const handleDispose = async (
    dispose: DshDisposeApi,
    envelope: Record<string, unknown>,
    res: ServerResponse,
  ): Promise<void> => {
    const sessionId = envelope.sessionId
    if (typeof sessionId !== 'string' || sessionId === '') {
      logAction('dsh.dispose', 400, { reason: 'invalid_request' })
      writeJson(res, 400, {
        reason: 'invalid_request',
        detail: 'dsh.dispose needs a nonempty sessionId',
      })
      return
    }
    let result
    try {
      result = await dispose.dispose(sessionId)
    } catch {
      // The executor already normalizes every host failure into an
      // outcome; a throw past it is a bug on this side, not a host answer.
      logAction('dsh.dispose', 500, { reason: 'executor_error' })
      writeJson(res, 500, { reason: 'executor_error' })
      return
    }
    // Every settled attempt answers 200 with its outcome, mirroring
    // inject.execute: "the host refused" is a result the UI renders, not a
    // transport error it should retry.
    logAction('dsh.dispose', 200, { outcome: result.outcome })
    writeJson(res, 200, { outcome: result.outcome })
  }

  const handleArchiveAction = async (
    archive: ArchiveApi,
    type: string,
    envelope: Record<string, unknown>,
    res: ServerResponse,
  ): Promise<void> => {
    if (type === 'archive.list') {
      await runArchiveOp(type, res, () => archive.list())
      return
    }

    if (type === 'archive.preview') {
      const idleSeconds = envelope.idleSeconds
      const rawStatuses = envelope.statuses
      if (
        typeof idleSeconds !== 'number' ||
        !Number.isFinite(idleSeconds) ||
        idleSeconds <= 0
      ) {
        logAction(type, 400, { reason: 'invalid_request' })
        writeJson(res, 400, {
          reason: 'invalid_request',
          detail: 'archive.preview needs a positive idleSeconds',
        })
        return
      }
      let statuses: string[] | undefined
      if (rawStatuses !== undefined) {
        if (
          !Array.isArray(rawStatuses) ||
          rawStatuses.some((name) => typeof name !== 'string')
        ) {
          logAction(type, 400, { reason: 'invalid_request' })
          writeJson(res, 400, {
            reason: 'invalid_request',
            detail: 'archive.preview statuses must be an array of strings',
          })
          return
        }
        statuses = rawStatuses as string[]
      }
      await runArchiveOp(type, res, () => archive.preview(idleSeconds, statuses))
      return
    }

    if (type === 'archive.apply') {
      const targets = parseArchiveTargets(envelope.targets)
      const token = envelope.token
      if (targets === null || typeof token !== 'string' || token === '') {
        logAction(type, 400, { reason: 'invalid_request' })
        writeJson(res, 400, {
          reason: 'invalid_request',
          detail: 'archive.apply needs a nonempty targets[{agent,sessionId}] and a preview token',
        })
        return
      }
      await runArchiveOp(type, res, () => archive.apply(targets, token))
      return
    }

    // archive.unarchive
    if (envelope.all === true) {
      await runArchiveOp(type, res, () => archive.unarchive('all'))
      return
    }
    const targets = parseArchiveTargets(envelope.targets)
    if (targets === null) {
      logAction(type, 400, { reason: 'invalid_request' })
      writeJson(res, 400, {
        reason: 'invalid_request',
        detail: 'archive.unarchive needs targets[{agent,sessionId}] or all=true',
      })
      return
    }
    await runArchiveOp(type, res, () => archive.unarchive(targets))
  }

  /** M2 dispatcher over the action envelope (gateway present, guard 1-4 passed). */
  const handleAction = async (
    gateway: InjectGatewayApi,
    verdict: GuardVerdict,
    req: IncomingMessage,
    res: ServerResponse,
  ): Promise<void> => {
    const body = await readActionBody(req)
    if (body.kind === 'too_large') {
      deps.log('warn', 'action rejected', { reason: 'body_too_large', limit: MAX_ACTION_BODY_BYTES })
      writeJson(res, 400, { reason: 'body_too_large' })
      return
    }
    if (body.kind === 'error') {
      deps.log('warn', 'action rejected', { reason: 'body_read_error' })
      writeJson(res, 400, { reason: 'body_read_error' })
      return
    }
    let parsed: unknown
    try {
      parsed = JSON.parse(body.text)
    } catch {
      deps.log('warn', 'action rejected', { reason: 'invalid_json' })
      writeJson(res, 400, { reason: 'invalid_json' })
      return
    }
    const envelope =
      typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
        ? (parsed as Record<string, unknown>)
        : null

    if (envelope !== null) {
      const type = typeof envelope.type === 'string' ? envelope.type : null

      if (type === 'daemon.retry') {
        // Daemon management is a capability of its own: `inject.enabled`
        // gates injection only (design §6), so retry passes guard layers
        // 1-4 without the write-action gate. retry() itself is a no-op
        // outside FAILED.
        deps.supervisor.retry()
        const state = deps.supervisor.state
        logAction('daemon.retry', 200, { state })
        writeJson(res, 200, { state })
        return
      }

      if (type !== null && ARCHIVE_ACTION_TYPES.has(type)) {
        // No inject write gate: see the ArchiveApi doc — hiding a session
        // from this board mutates sidecar-local state only.
        const archive = deps.archive
        if (archive === undefined) {
          logAction(type, 501, { reason: 'archive_unavailable' })
          writeJson(res, 501, { reason: 'archive_unavailable' })
          return
        }
        await handleArchiveAction(archive, type, envelope, res)
        return
      }

      if (type === 'dsh.dispose') {
        // Dispose ends a live dsh session, so unlike archiving it passes
        // through the same write gate as injection: `inject.enabled` is
        // the deployment's "this plugin may mutate agent state" switch,
        // and ending a session is the largest mutation on offer.
        const writeVerdict = guardWriteAction(verdict, deps.guardOptions)
        if (!writeVerdict.ok) {
          logAction(type, writeVerdict.status, { reason: writeVerdict.reason })
          writeJson(res, writeVerdict.status, { reason: writeVerdict.reason })
          return
        }
        const dispose = deps.dispose
        if (dispose === undefined || !dispose.available()) {
          logAction(type, 501, { reason: 'dispose_unavailable' })
          writeJson(res, 501, { reason: 'dispose_unavailable' })
          return
        }
        await handleDispose(dispose, envelope, res)
        return
      }

      if (type === 'inject.prepare' || type === 'inject.execute') {
        const writeVerdict = guardWriteAction(verdict, deps.guardOptions)
        if (!writeVerdict.ok) {
          logAction(type, writeVerdict.status, { reason: writeVerdict.reason })
          writeJson(res, writeVerdict.status, { reason: writeVerdict.reason })
          return
        }
        if (type === 'inject.prepare') {
          await handlePrepare(gateway, envelope, res)
        } else {
          await handleExecute(gateway, envelope, res)
        }
        return
      }

      if (type !== null && ANALYSIS_ACTION_TYPES.has(type)) {
        // The analysis write gate (guard layers 1-4 already passed via
        // `verdict`): request/followup are gated by analysis.enabled —
        // NOT the guard's inject.enabled gate — and fail-closed when the
        // dep is absent. analysis.cancel deliberately BYPASSES the gate
        // (F3): it is a cleanup/stop-loss action that spends no tokens,
        // and flipping the kill switch off must not lock in-flight
        // sessions out of cancellation until their timeout.
        if (
          type !== 'analysis.cancel' &&
          (deps.analysisEnabled === undefined || !deps.analysisEnabled())
        ) {
          logAction(type, 403, { reason: 'analysis_disabled' })
          writeJson(res, 403, { reason: 'analysis_disabled' })
          return
        }
        // Past the gate: no engine wired, or the agents service is not
        // bound in this composition → honest degradation, never a crash.
        const analysis = deps.analysis
        if (analysis === undefined || !analysis.available()) {
          logAction(type, 501, { reason: 'analysis_unavailable' })
          writeJson(res, 501, { reason: 'analysis_unavailable' })
          return
        }
        // Model pre-check (A-1), request only: no explicit analysis
        // provider/model and no host default → 403 before any session is
        // created. Same status family as analysis_disabled: the action is
        // well-formed but the deployment configuration forbids running it.
        if (
          type === 'analysis.request' &&
          analysis.modelConfigured !== undefined &&
          !analysis.modelConfigured()
        ) {
          logAction(type, 403, { reason: 'analysis_model_unconfigured' })
          writeJson(res, 403, { reason: 'analysis_model_unconfigured' })
          return
        }
        if (type === 'analysis.request') {
          await handleAnalysisRequest(analysis, envelope, res)
        } else if (type === 'analysis.followup') {
          await handleAnalysisFollowup(analysis, envelope, res)
        } else {
          await handleAnalysisCancel(analysis, envelope, res)
        }
        return
      }
    }

    deps.log('warn', 'action rejected', { reason: 'unknown_action' })
    writeJson(res, 400, { reason: 'unknown_action' })
  }

  const handle = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    if (disposed) {
      writeJson(res, 503, { reason: 'shutting_down' })
      return
    }

    // Preserve Node's lossless header list at the framework boundary. The
    // guard uses it to count Host/Origin before `headers` first-wins/joins.
    const verdict = guardRequest(
      {
        method: req.method,
        url: req.url,
        headers: req.headers,
        rawHeaders: req.rawHeaders,
        socket: req.socket,
      },
      deps.guardOptions,
    )
    const method = (req.method ?? '').toUpperCase()
    const subpath = subpathOf(req.url)
    // The gateway-backed action handler reads its own bounded body; every
    // other body-bearing request is drained so the keep-alive connection is
    // left in a clean state either way.
    const actionReadsBody =
      verdict.ok &&
      subpath === 'action' &&
      method === 'POST' &&
      deps.injectGateway !== undefined
    if (
      !actionReadsBody &&
      (method === 'POST' || method === 'PUT' || method === 'PATCH')
    ) {
      req.resume()
    }
    if (!verdict.ok) {
      writeJson(res, verdict.status, { reason: verdict.reason })
      return
    }

    if (subpath === null || subpath === '') {
      writeJson(res, 404, { reason: 'not_found' })
      return
    }

    if (subpath === 'state') {
      if (method !== 'GET') return writeMethodNotAllowed(res, 'GET')
      writeJson(res, 200, buildSnapshot())
      return
    }

    if (subpath === 'stream') {
      if (method !== 'GET') return writeMethodNotAllowed(res, 'GET')
      acceptStream(res)
      return
    }

    if (subpath === 'action') {
      if (method !== 'POST') return writeMethodNotAllowed(res, 'POST')
      const gateway = deps.injectGateway
      if (gateway === undefined) {
        // M1 wiring (no gateway assembled): keep the placeholder contract —
        // the write gate is live (403 when inject is off), then 501.
        const writeVerdict = guardWriteAction(verdict, deps.guardOptions)
        if (!writeVerdict.ok) {
          writeJson(res, writeVerdict.status, { reason: writeVerdict.reason })
          return
        }
        writeJson(res, 501, { reason: 'not_implemented_until_m2' })
        return
      }
      await handleAction(gateway, verdict, req, res)
      return
    }

    if (subpath === 'projects') {
      if (method !== 'GET') return writeMethodNotAllowed(res, 'GET')
      handleProjects(res)
      return
    }

    if (subpath === 'search') {
      if (method !== 'GET') return writeMethodNotAllowed(res, 'GET')
      await handleSearch(res, queryOf(req.url))
      return
    }

    if (subpath.startsWith('lineage/')) {
      if (method !== 'GET') return writeMethodNotAllowed(res, 'GET')
      await handleLineage(res, subpath.slice('lineage/'.length))
      return
    }

    if (subpath.startsWith('session/')) {
      if (method !== 'GET') return writeMethodNotAllowed(res, 'GET')
      const rest = subpath.slice('session/'.length)
      // `<id>/timeline` splits on the LAST path segment; percent-encoded
      // ids can never contain a literal '/' so the split is unambiguous.
      if (rest.endsWith('/timeline')) {
        await handleTimeline(res, rest.slice(0, -'/timeline'.length), queryOf(req.url))
        return
      }
      await handleSession(res, rest)
      return
    }

    writeJson(res, 404, { reason: 'not_found' })
  }

  const dispose = (): void => {
    if (disposed) return
    disposed = true
    for (const unsubscribe of unsubscribes) unsubscribe()
    for (const client of [...clients]) {
      cleanupClient(client)
      // Graceful end: the terminal chunk lets EventSource/fetch readers see
      // a clean stream end. Socket lifecycle belongs to the webServer.
      client.res.end()
    }
    deps.log('info', 'routes disposed')
  }

  return { handle, dispose }
}
