/**
 * Project-correlation data glue (T5.10b): a framework-free store feeding
 * the controlled ProjectView with `GET projects` wire groups. Derivation
 * (normalize/merge/sort) stays in board/project-view-logic.ts — this store
 * owns transport orchestration and refresh throttling only.
 *
 * Refresh model: an explicit `refresh()` on view entry, plus SSE-driven
 * `notifySnapshot()` refreshes throttled to {@link DEFAULT_MIN_REFRESH_MS}
 * (state frames arrive per board mutation; the projects endpoint recomputes
 * groups per call, so hammering it buys nothing). Stale groups stay
 * rendered through failed refreshes (honest banner, never a blank).
 *
 * Same store discipline as controller.ts: subscribe/getState for
 * `useSyncExternalStore`, immutable snapshots, dispose() = late no-ops.
 *
 * @module
 */

import { isApiError, type RequestOptions } from './api.ts'
import { fetchProjects, type ProjectsResponseVM } from './m3-transport.ts'
import type { ProjectGroupVM } from './board/project-view-logic.ts'
import type { DetailHeaderHint } from './detail-glue.ts'

/** Minimum spacing between SSE-triggered refreshes. */
export const DEFAULT_MIN_REFRESH_MS = 5_000

export interface ProjectsGlueState {
  groups: ProjectGroupVM[]
  /** True while a fetch is in flight AND nothing was loaded yet. */
  loading: boolean
  /** Machine reason code of the last failure, or null. */
  error: string | null
  /** Epoch ms of the last successful load; null before the first one. */
  loadedAt: number | null
}

export interface ProjectsStoreOptions {
  fetchProjectsFn?: (opts?: RequestOptions) => Promise<ProjectsResponseVM>
  minRefreshMs?: number
  /** Clock injection for throttling tests. */
  now?: () => number
}

export class ProjectsStore {
  private state: ProjectsGlueState = { groups: [], loading: false, error: null, loadedAt: null }
  private readonly listeners = new Set<() => void>()
  private readonly fetchProjectsFn: NonNullable<ProjectsStoreOptions['fetchProjectsFn']>
  private readonly minRefreshMs: number
  private readonly now: () => number
  private disposed = false
  private inFlight = false
  private lastAttemptAt: number | null = null

  constructor(options: ProjectsStoreOptions = {}) {
    this.fetchProjectsFn = options.fetchProjectsFn ?? fetchProjects
    this.minRefreshMs = options.minRefreshMs ?? DEFAULT_MIN_REFRESH_MS
    this.now = options.now ?? Date.now
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => { this.listeners.delete(fn) }
  }

  getState = (): ProjectsGlueState => this.state

  private setState(patch: Partial<ProjectsGlueState>): void {
    if (this.disposed) return
    this.state = { ...this.state, ...patch }
    for (const fn of [...this.listeners]) fn()
  }

  /** Fetch the groups now (deduped while one call is in flight). */
  async refresh(): Promise<void> {
    if (this.disposed || this.inFlight) return
    this.inFlight = true
    this.lastAttemptAt = this.now()
    if (this.state.loadedAt === null) this.setState({ loading: true, error: null })
    try {
      const body = await this.fetchProjectsFn()
      if (this.disposed) return
      this.setState({
        groups: body.groups,
        loading: false,
        error: null,
        loadedAt: this.now(),
      })
    } catch (err) {
      if (this.disposed) return
      // Previously loaded groups stay on screen; the view banners the code.
      this.setState({
        loading: false,
        error: isApiError(err) ? err.reason : 'network_error',
      })
    } finally {
      this.inFlight = false
    }
  }

  /** SSE `state` frame hook: throttled refresh. */
  notifySnapshot(): void {
    if (this.disposed || this.inFlight) return
    if (this.lastAttemptAt !== null && this.now() - this.lastAttemptAt < this.minRefreshMs) return
    void this.refresh()
  }

  dispose(): void {
    this.disposed = true
    this.listeners.clear()
  }
}

/**
 * Find a session inside loaded project groups → header hint for the
 * detail view (project rows carry no board card), or null when unknown.
 */
export function findProjectSessionHint(
  groups: readonly ProjectGroupVM[],
  sessionId: string,
): DetailHeaderHint | null {
  for (const group of groups) {
    for (const session of group.sessions) {
      if (session.sessionId === sessionId) {
        return {
          agent: session.agent,
          title: session.title,
          project: group.project,
          status: session.status,
        }
      }
    }
  }
  return null
}
