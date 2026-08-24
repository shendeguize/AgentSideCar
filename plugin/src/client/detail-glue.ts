/**
 * Session-detail data glue (T5.10b): one framework-free store per opened
 * detail view, feeding the controlled SessionDetail / LineageTree
 * components. Owns transport orchestration only — accumulation and
 * presentation stay in detail/logic.ts and dsh-tools/logic.ts:
 *
 * - initial load via detail/transport.ts `fetchSessionDetail` (header +
 *   newest timeline page in one round-trip);
 * - older-history pagination via `fetchTimelinePage(cursor)`;
 * - listen mode: the controller's SSE `state` frames carry no per-session
 *   events (ADR-2/ADR-3), so the integration calls {@link
 *   DetailStore.notifySnapshot} on every frame and the store refetches the
 *   newest window — coalesced so at most one refetch is in flight and at
 *   most one more is queued;
 * - dsh-exclusive lineage: fetched only for dsh sessions; non-dsh agents
 *   get the client-minted `not_dsh_session` degradation without dialing
 *   (dsh-tools/logic.ts `externalLineageFallback`).
 *
 * Same store discipline as controller.ts: subscribe/getState for
 * `useSyncExternalStore`, immutable state snapshots, `dispose()` makes
 * late settlements no-ops. All transport is injectable for node tests.
 *
 * @module
 */

import { isApiError, type RequestOptions } from './api.ts'
import {
  fetchSessionDetail,
  fetchTimelinePage,
  type SessionDetailWire,
} from './detail/transport.ts'
import { fetchLineage } from './m3-transport.ts'
import {
  applyListenPage,
  applyTimelinePage,
  createTimelineVM,
  type TimelinePageWire,
  type TimelineVM,
} from './detail/logic.ts'
import {
  externalLineageFallback,
  type LineageResponseVM,
  type LineageTraceVM,
} from './dsh-tools/logic.ts'

// ---------------------------------------------------------------------------
// State shapes.
// ---------------------------------------------------------------------------

/**
 * Header seed carried from the surface that opened the detail view (board
 * card / project row); structurally SessionDetailHeaderVM. The
 * authoritative header comes from the fetched detail body — the hint just
 * paints the first frame and covers degraded loads.
 */
export interface DetailHeaderHint {
  agent: string
  title: string
  project: string
  status: string
}

/** Lineage panel slice (mirrors LineageTree props). */
export interface LineageSliceState {
  /** True until the first resolution (fetch settle or client degrade). */
  loading: boolean
  /** Transport/HTTP failure reason code, or null. */
  error: string | null
  available: boolean
  reason: string | null
  detail: string | null
  trace: LineageTraceVM | null
}

/** Everything the detail view renders (mirrors SessionDetail props). */
export interface DetailGlueState {
  sessionId: string
  header: DetailHeaderHint
  timeline: TimelineVM
  /** True while the initial load or an older-page fetch is in flight. */
  loading: boolean
  /** Machine reason code of the last failure, or null. */
  error: string | null
  hasMore: boolean
  listening: boolean
  /** True while a manual newest-window refresh is in flight (UX-07). */
  refreshing: boolean
  /** True once the initial load succeeded (timeline usable). */
  ready: boolean
  lineage: LineageSliceState
}

const EMPTY_HEADER: DetailHeaderHint = { agent: '', title: '', project: '', status: '' }

const INITIAL_LINEAGE: LineageSliceState = {
  loading: true,
  error: null,
  available: false,
  reason: null,
  detail: null,
  trace: null,
}

/** Map any settlement failure to a stable machine reason code. */
function reasonOf(err: unknown): string {
  return isApiError(err) ? err.reason : 'network_error'
}

// ---------------------------------------------------------------------------
// Injectable transport (defaults are the real calls).
// ---------------------------------------------------------------------------

export interface DetailStoreOptions {
  /** Header seed from the opening surface; null paints an empty header. */
  hint?: DetailHeaderHint | null
  /** Newest-window size for listen-mode refetches (server default if unset). */
  listenLimit?: number
  fetchDetailFn?: (sessionId: string, opts?: RequestOptions) => Promise<SessionDetailWire>
  fetchPageFn?: (
    sessionId: string,
    opts?: RequestOptions & { cursor?: string | null; limit?: number },
  ) => Promise<TimelinePageWire>
  fetchLineageFn?: (sessionId: string, opts?: RequestOptions) => Promise<LineageResponseVM>
}

// ---------------------------------------------------------------------------
// The store.
// ---------------------------------------------------------------------------

export class DetailStore {
  private state: DetailGlueState
  private readonly listeners = new Set<() => void>()
  private readonly fetchDetailFn: NonNullable<DetailStoreOptions['fetchDetailFn']>
  private readonly fetchPageFn: NonNullable<DetailStoreOptions['fetchPageFn']>
  private readonly fetchLineageFn: NonNullable<DetailStoreOptions['fetchLineageFn']>
  private readonly listenLimit: number | undefined
  private disposed = false
  private opened = false
  private paging = false
  private listenInFlight = false
  private listenQueued = false
  private refreshInFlight = false
  private lineageStarted = false

  constructor(sessionId: string, options: DetailStoreOptions = {}) {
    this.fetchDetailFn = options.fetchDetailFn ?? fetchSessionDetail
    this.fetchPageFn = options.fetchPageFn ?? fetchTimelinePage
    this.fetchLineageFn = options.fetchLineageFn ?? fetchLineage
    this.listenLimit = options.listenLimit
    this.state = {
      sessionId,
      header: options.hint ?? EMPTY_HEADER,
      timeline: createTimelineVM(sessionId),
      loading: false,
      error: null,
      hasMore: false,
      listening: false,
      refreshing: false,
      ready: false,
      lineage: INITIAL_LINEAGE,
    }
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => { this.listeners.delete(fn) }
  }

  getState = (): DetailGlueState => this.state

  private setState(patch: Partial<DetailGlueState>): void {
    if (this.disposed) return
    this.state = { ...this.state, ...patch }
    for (const fn of [...this.listeners]) fn()
  }

  /** Initial load: header + newest timeline page, then lineage. Idempotent. */
  async open(): Promise<void> {
    if (this.opened || this.disposed) return
    this.opened = true
    this.setState({ loading: true, error: null })
    try {
      const wire = await this.fetchDetailFn(this.state.sessionId)
      if (this.disposed) return
      const header = headerFromDetailWire(wire) ?? this.state.header
      if (wire.timeline === null) {
        // Pre-M3 host placeholder contract: card data only, no timeline.
        this.setState({ header, loading: false, error: 'fusion_not_wired' })
      } else {
        const timeline = applyTimelinePage(
          createTimelineVM(this.state.sessionId),
          wire.timeline,
        )
        this.setState({
          header,
          timeline,
          loading: false,
          error: null,
          hasMore: timeline.nextCursor !== null,
          ready: true,
        })
      }
      void this.loadLineage(header.agent)
    } catch (err) {
      if (this.disposed) return
      this.setState({ loading: false, error: reasonOf(err) })
      // The board hint still tells the agent kind; resolve the lineage
      // slice anyway so the panel degrades instead of spinning forever.
      void this.loadLineage(this.state.header.agent)
    }
  }

  /** Fetch one older history page via the accumulated cursor. */
  async loadMore(): Promise<void> {
    const { timeline } = this.state
    if (this.disposed || this.paging || !this.state.ready) return
    if (timeline.nextCursor === null) return
    this.paging = true
    this.setState({ loading: true, error: null })
    try {
      const page = await this.fetchPageFn(this.state.sessionId, {
        cursor: timeline.nextCursor,
      })
      if (this.disposed) return
      const next = applyTimelinePage(this.state.timeline, page)
      this.setState({
        timeline: next,
        loading: false,
        hasMore: next.nextCursor !== null,
      })
    } catch (err) {
      if (this.disposed) return
      // Entries already shown stay visible; the view renders the reason
      // as an inline banner (deriveDetailBodyState 'list' arm).
      this.setState({ loading: false, error: reasonOf(err) })
    } finally {
      this.paging = false
    }
  }

  /** Flip listen mode; turning it on refetches the newest window at once. */
  toggleListen(): void {
    const listening = !this.state.listening
    this.setState({ listening })
    if (listening) this.scheduleListenRefetch()
  }

  /**
   * Manual newest-window refetch with visible feedback (UX-07), also fired
   * once after a delivered injection (UX-05 observation loop). Unlike the
   * silent best-effort listen refetch, it reports in-flight state and
   * surfaces a failure reason (rendered as the inline banner). Appended
   * entries get the listen-merge highlight. Coalesced: at most one manual
   * refresh in flight, extra calls are dropped.
   */
  async refreshNewest(): Promise<void> {
    if (this.disposed || !this.state.ready || this.refreshInFlight) return
    this.refreshInFlight = true
    this.setState({ refreshing: true, error: null })
    try {
      const page = await this.fetchPageFn(this.state.sessionId, {
        ...(this.listenLimit !== undefined ? { limit: this.listenLimit } : {}),
      })
      if (this.disposed) return
      this.setState({
        timeline: applyListenPage(this.state.timeline, page),
        refreshing: false,
      })
    } catch (err) {
      if (this.disposed) return
      this.setState({ refreshing: false, error: reasonOf(err) })
    } finally {
      this.refreshInFlight = false
    }
  }

  /**
   * SSE `state` frame hook (one call per controller notification). Refreshes
   * the header from the live board card when given, and in listen mode
   * triggers a coalesced newest-window refetch.
   */
  notifySnapshot(card: DetailHeaderHint | null): void {
    if (this.disposed) return
    if (card !== null) {
      const h = this.state.header
      if (
        card.agent !== h.agent ||
        card.title !== h.title ||
        card.project !== h.project ||
        card.status !== h.status
      ) {
        this.setState({ header: card })
      }
    }
    if (this.state.listening && this.state.ready) this.scheduleListenRefetch()
  }

  /** At most one refetch in flight; at most one more queued (idempotence). */
  private scheduleListenRefetch(): void {
    if (this.disposed || !this.state.ready) return
    if (this.listenInFlight) {
      this.listenQueued = true
      return
    }
    this.listenInFlight = true
    void this.runListenRefetch()
  }

  private async runListenRefetch(): Promise<void> {
    try {
      const page = await this.fetchPageFn(this.state.sessionId, {
        ...(this.listenLimit !== undefined ? { limit: this.listenLimit } : {}),
      })
      if (this.disposed) return
      this.setState({ timeline: applyListenPage(this.state.timeline, page) })
    } catch {
      // Listen refetch is best-effort: the next SSE frame retries; the
      // already-rendered timeline must not degrade into an error state.
    } finally {
      this.listenInFlight = false
      if (this.listenQueued && !this.disposed) {
        this.listenQueued = false
        this.scheduleListenRefetch()
      }
    }
  }

  /** Resolve the lineage slice once (dsh-only capability; see module doc). */
  private async loadLineage(agent: string): Promise<void> {
    if (this.lineageStarted || this.disposed) return
    if (agent.trim() === '') {
      // Agent unknown (load failed before the header resolved): degrade
      // as non-dsh rather than dialing a lineage endpoint blind.
      this.lineageStarted = true
      this.setState({
        lineage: {
          loading: false, error: null, available: false,
          reason: 'not_dsh_session', detail: null, trace: null,
        },
      })
      return
    }
    this.lineageStarted = true
    const fallback = externalLineageFallback(agent)
    if (fallback !== null) {
      this.setState({
        lineage: {
          loading: false,
          error: null,
          available: fallback.available,
          reason: fallback.reason,
          detail: null,
          trace: fallback.trace,
        },
      })
      return
    }
    try {
      const body = await this.fetchLineageFn(this.state.sessionId)
      if (this.disposed) return
      this.setState({
        lineage: {
          loading: false,
          error: null,
          available: body.available,
          reason: body.reason,
          detail: body.detail ?? null,
          trace: body.trace,
        },
      })
    } catch (err) {
      if (this.disposed) return
      this.setState({ lineage: { ...INITIAL_LINEAGE, loading: false, error: reasonOf(err) } })
    }
  }

  /** Late settlements become no-ops; subscribers are dropped. Idempotent. */
  dispose(): void {
    this.disposed = true
    this.listeners.clear()
  }
}

// ---------------------------------------------------------------------------
// Pure helpers (exported for tests and for the mount integration).
// ---------------------------------------------------------------------------

/**
 * Authoritative header from the M3 detail body: the fused row wins (it
 * merges both sources), the sidecar board row covers fusion-less hosts;
 * null when the body carries neither (caller keeps its hint).
 */
export function headerFromDetailWire(wire: SessionDetailWire): DetailHeaderHint | null {
  if (wire.unified !== null) {
    return {
      agent: wire.unified.agent,
      title: wire.unified.title,
      project: wire.unified.project,
      status: wire.unified.status,
    }
  }
  if (wire.session !== null) {
    return {
      agent: wire.session.agent,
      title: wire.session.title,
      project: wire.session.project,
      status: wire.session.status,
    }
  }
  return null
}

/**
 * Find the live board card of a session (controller SessionCardVM rows) →
 * header hint, or null when off-board.
 */
export function findCardHint(
  sessions: ReadonlyArray<{
    agent: string
    sessionId: string
    title: string
    project: string
    status: string
  }>,
  sessionId: string,
): DetailHeaderHint | null {
  const card = sessions.find((s) => s.sessionId === sessionId)
  if (card === undefined) return null
  return { agent: card.agent, title: card.title, project: card.project, status: card.status }
}
