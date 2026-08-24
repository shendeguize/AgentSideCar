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
export type InjectGatewayApi = Pick<InjectGateway, 'prepare' | 'execute'>

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
  capabilities: { inject: boolean }
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
  target_dead: 409,
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
  token_missing: 401,
  token_expired: 401,
  token_reused: 409,
  token_mismatch: 409,
  unsupported_agent: 422,
  executor_error: 502,
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

  const clients = new Set<SseClient>()
  let disposed = false

  const buildSnapshot = (): StateSnapshot => ({
    daemon: { state: deps.supervisor.state, lastPing: deps.supervisor.lastPing },
    board: deps.store.getBoardState(),
    capabilities: { inject: deps.guardOptions.allowWriteActions() },
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

  const handleSession = (res: ServerResponse, rawId: string): void => {
    let id: string
    try {
      id = decodeURIComponent(rawId)
    } catch {
      writeJson(res, 404, { reason: 'session_not_found' })
      return
    }
    const view =
      id === ''
        ? undefined
        : deps.store.getBoardState().sessions.find((s) => s.session_id === id)
    if (view === undefined) {
      writeJson(res, 404, { reason: 'session_not_found' })
      return
    }
    // M1: detail == card data. The event timeline is M3; the placeholder
    // shape is already contractual so the client half can render "pending".
    writeJson(res, 200, {
      session: view,
      timeline: null,
      timelineNote: 'timeline_not_available_until_m3',
    })
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
    }

    deps.log('warn', 'action rejected', { reason: 'unknown_action' })
    writeJson(res, 400, { reason: 'unknown_action' })
  }

  const handle = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    if (disposed) {
      writeJson(res, 503, { reason: 'shutting_down' })
      return
    }

    const verdict = guardRequest(req, deps.guardOptions)
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

    if (subpath.startsWith('session/')) {
      if (method !== 'GET') return writeMethodNotAllowed(res, 'GET')
      handleSession(res, subpath.slice('session/'.length))
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
