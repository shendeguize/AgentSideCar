/**
 * HTTP route layer for the plugin's self-registered namespace (design §4.f).
 *
 * M1 scope: three read endpoints (`GET state`, `GET session/<id>`,
 * `GET stream` SSE) plus a `POST action` placeholder that already enforces
 * the write gate (403 when `inject.enabled` is off, 501 otherwise — the
 * real gateway is M2).
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
import { guardRequest, guardWriteAction, type GuardOptions } from './guard.ts'
import type { BoardState, SessionStore } from './session-store.ts'
import type { DaemonSupervisor, PingInfo, SupervisorState } from './supervisor.ts'

/** dsh webServer route handler shape (see module doc for the evidence). */
export type WebRouteHandler = (
  req: IncomingMessage,
  res: ServerResponse,
) => void | Promise<void>

/** Route namespace, per the `/plugins/<package>/` convention (design §4.f). */
export const API_PREFIX = '/plugins/agent-sidecar/api'

/** Everything the route layer consumes; all live objects, none owned here. */
export interface RoutesDeps {
  store: SessionStore
  supervisor: DaemonSupervisor
  /** Live `inject.enabled` reader shared with the guard's write gate. */
  guardOptions: GuardOptions
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

  const handle = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    if (disposed) {
      writeJson(res, 503, { reason: 'shutting_down' })
      return
    }

    const verdict = guardRequest(req, deps.guardOptions)
    // M1 never reads a body; drain body-bearing requests so the keep-alive
    // connection is left in a clean state either way.
    const method = (req.method ?? '').toUpperCase()
    if (method === 'POST' || method === 'PUT' || method === 'PATCH') req.resume()
    if (!verdict.ok) {
      writeJson(res, verdict.status, { reason: verdict.reason })
      return
    }

    const subpath = subpathOf(req.url)
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
      // M2 placeholder: the write gate is already live (403 when inject is
      // off), the gateway itself is not (501).
      if (method !== 'POST') return writeMethodNotAllowed(res, 'POST')
      const writeVerdict = guardWriteAction(verdict, deps.guardOptions)
      if (!writeVerdict.ok) {
        writeJson(res, writeVerdict.status, { reason: writeVerdict.reason })
        return
      }
      writeJson(res, 501, { reason: 'not_implemented_until_m2' })
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
