/**
 * Transport helpers for the M3 deep-query read endpoints (lineage /
 * search / projects), completing what detail/transport.ts started for
 * `session/<id>` + timeline. Same posture as api.ts: same-origin relative
 * paths under {@link API_PREFIX}, bounded timeout, normalized ApiError,
 * injectable browser primitives so node tests run without a DOM.
 *
 * Response typing reuses the components' own wire mirrors
 * (dsh-tools/logic.ts, board/project-view-logic.ts) so the transport and
 * the render pipelines can never disagree about a shape.
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
  type TimerHandle,
} from './api.ts'
import type { LineageResponseVM, SearchResponseVM } from './dsh-tools/logic.ts'
import type { ProjectGroupVM } from './board/project-view-logic.ts'

/** Body of `GET <prefix>/projects` (host: routes.ts handleProjects). */
export interface ProjectsResponseVM {
  groups: ProjectGroupVM[]
}

// ---------------------------------------------------------------------------
// Bounded same-origin GET (api.ts `request` is module-private; this is the
// same discipline detail/transport.ts uses: timeout, external abort,
// ApiError taxonomy).
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
 * `GET <prefix>/lineage/<id>` — dsh lineage trace. Degradation
 * (sessionQuery absent / trace failed) is DATA: the host answers 200 with
 * `{available:false, reason}`, so only transport/HTTP failures reject
 * (e.g. 501 `fusion_not_wired` on a pre-M3 host).
 */
export async function fetchLineage(
  sessionId: string,
  opts: RequestOptions = {},
): Promise<LineageResponseVM> {
  const path = `${API_PREFIX}/lineage/${encodeURIComponent(sessionId)}`
  return (await getJson(path, opts)) as LineageResponseVM
}

/**
 * `GET <prefix>/search?q=&project=&limit=` — cross-agent session search.
 * At least one of `q` / `project` must be non-blank (the host answers 400
 * `invalid_request` otherwise — callers gate before dialing). The response
 * echoes the mode; `filter-only` is the honest degradation, not an error.
 */
export async function fetchSearch(
  opts: RequestOptions & { q?: string; project?: string | null; limit?: number } = {},
): Promise<SearchResponseVM> {
  const params = new URLSearchParams()
  if (opts.q !== undefined && opts.q.trim() !== '') params.set('q', opts.q)
  if (opts.project !== undefined && opts.project !== null && opts.project.trim() !== '') {
    params.set('project', opts.project)
  }
  if (opts.limit !== undefined) params.set('limit', String(opts.limit))
  return (await getJson(`${API_PREFIX}/search?${params.toString()}`, opts)) as SearchResponseVM
}

/** `GET <prefix>/projects` — cross-agent project groups. */
export async function fetchProjects(opts: RequestOptions = {}): Promise<ProjectsResponseVM> {
  return (await getJson(`${API_PREFIX}/projects`, opts)) as ProjectsResponseVM
}
