/**
 * Transport helpers for the detail view's M3 read endpoints. api.ts (T2.1)
 * predates M3: its `fetchSession` is typed for the M1 placeholder response
 * and it has no timeline-pagination call — and per the task boundary its
 * existing exports must not change (the S7 integration wave unifies the
 * data layer). So this module carries the missing calls with the same
 * posture as api.ts (same-origin relative paths, bounded timeout,
 * normalized ApiError, injectable primitives), reusing api.ts's exported
 * building blocks instead of redefining them.
 *
 * Read-only surface, no retry policy here (transport, not policy).
 *
 * @module
 */

import {
  API_PREFIX,
  ApiError,
  DEFAULT_TIMEOUT_MS,
  type AbortControllerLike,
  type FetchLike,
  type RequestOptions,
  type ResponseLike,
  type SessionView,
  type TimerHandle,
} from '../api.ts'
import type { TimelinePageWire } from './logic.ts'

// ---------------------------------------------------------------------------
// Wire mirrors of the M3 `GET session/<id>` body (host: routes.ts
// handleSession with fusion wired; unified row: fusion.ts UnifiedSession).
// ---------------------------------------------------------------------------

/** One deduplicated cross-agent session row (host: fusion.ts). */
export interface UnifiedSessionWire {
  agent: string
  sessionId: string
  origin: 'dsh-live' | 'sidecar' | 'merged'
  live: boolean
  status: string
  title: string
  project: string
  /** Unix epoch ms of the freshest signal across both sources. */
  lastActivityAt: number
  lastEvent: { ts: string; kind: string; text: string } | null
  lastSeq: number | null
  gap: boolean
  parentId: string | null
  extra: Record<string, unknown>
}

/** Body of `GET session/<id>` on an M3 host (fusion wired). */
export interface SessionDetailWire {
  /** Sidecar board row; null for dsh-live sessions the sidecar has not seen. */
  session: SessionView | null
  /** Fused row; null when only the board knows the session. */
  unified: UnifiedSessionWire | null
  /** Newest timeline page; null only on a pre-M3 host (placeholder contract). */
  timeline: TimelinePageWire | null
}

// ---------------------------------------------------------------------------
// Bounded same-origin GET (api.ts `request` is module-private; this is the
// same discipline in miniature: timeout, external abort, ApiError taxonomy).
// ---------------------------------------------------------------------------

const defaultSetTimeout = (fn: () => void, ms: number): TimerHandle =>
  globalThis.setTimeout(fn, ms)

const defaultClearTimeout = (handle: TimerHandle): void => {
  globalThis.clearTimeout(handle as ReturnType<typeof globalThis.setTimeout>)
}

const defaultCreateAbortController = (): AbortControllerLike => new AbortController()

function resolveFetch(opts: RequestOptions): FetchLike {
  if (opts.fetch !== undefined) return opts.fetch
  return globalThis.fetch as unknown as FetchLike
}

async function getJson(path: string, opts: RequestOptions): Promise<unknown> {
  const doFetch = resolveFetch(opts)
  const controller = (opts.createAbortController ?? defaultCreateAbortController)()
  const setT = opts.setTimeout ?? defaultSetTimeout
  const clearT = opts.clearTimeout ?? defaultClearTimeout

  let timedOut = false
  let externallyAborted = false
  const timer = setT(() => {
    timedOut = true
    controller.abort()
  }, opts.timeoutMs ?? DEFAULT_TIMEOUT_MS)

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
      res = await doFetch(path, { method: 'GET', signal: controller.signal })
    } catch (err) {
      if (timedOut) throw new ApiError('timeout', 'request_timeout', null, err)
      if (externallyAborted) throw new ApiError('aborted', 'request_aborted', null, err)
      throw new ApiError('network', 'network_error', null, err)
    }
    if (!res.ok) {
      let reason = `http_${res.status}`
      try {
        const body = await res.json()
        if (typeof body === 'object' && body !== null) {
          const value = (body as Record<string, unknown>)['reason']
          if (typeof value === 'string' && value !== '') reason = value
        }
      } catch {
        // Non-JSON error body: the status-derived reason stands.
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

/**
 * `GET <prefix>/session/<id>` typed for the M3 body. Unknown ids reject
 * with an ApiError carrying the server's `session_not_found` reason; a
 * pre-M3 host answers `timeline: null` (the caller degrades honestly).
 */
export async function fetchSessionDetail(
  sessionId: string,
  opts: RequestOptions = {},
): Promise<SessionDetailWire> {
  const path = `${API_PREFIX}/session/${encodeURIComponent(sessionId)}`
  return (await getJson(path, opts)) as SessionDetailWire
}

/**
 * `GET <prefix>/session/<id>/timeline?cursor=&limit=` — one older history
 * page. `cursor` is the opaque `nextCursor` token from a previous page,
 * passed through verbatim (the server rejects tampered tokens with 400
 * `invalid_cursor`); omit it for the newest window (listen-mode refetch).
 */
export async function fetchTimelinePage(
  sessionId: string,
  opts: RequestOptions & { cursor?: string | null; limit?: number } = {},
): Promise<TimelinePageWire> {
  const params = new URLSearchParams()
  if (opts.cursor !== undefined && opts.cursor !== null && opts.cursor !== '') {
    params.set('cursor', opts.cursor)
  }
  if (opts.limit !== undefined) params.set('limit', String(opts.limit))
  const query = params.toString()
  const path = `${API_PREFIX}/session/${encodeURIComponent(sessionId)}/timeline${
    query === '' ? '' : `?${query}`
  }`
  return (await getJson(path, opts)) as TimelinePageWire
}
