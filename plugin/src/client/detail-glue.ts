/**
 * Session-detail data glue (T5.10b): one framework-free store per opened
 * detail view, feeding the controlled SessionDetail / LineageTree
 * components. Owns transport orchestration only — accumulation and
 * presentation stay in detail/logic.ts and dsh-tools/logic.ts:
 *
 * - initial load via detail/transport.ts `fetchSessionDetail` (header +
 *   newest timeline page in one round-trip);
 * - every SSE snapshot invalidates any older detail request immediately,
 *   then runs at most one latest queued detail refresh; only that generation
 *   may publish authoritative header metadata;
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
 * `useSyncExternalStore`, immutable state snapshots, generation-scoped
 * requests, and `dispose()` cancellation plus late-settlement no-ops. All
 * transport is injectable for node tests.
 *
 * @module
 */

import {
  isApiError,
  LEGACY_TIMELINE_HEALTH,
  normalizeTimelineHealth,
  type RequestOptions,
  type TimelineHealth,
} from './api.ts'
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
  /** Sanitized availability aggregated across accepted pages in the view. */
  timelineHealth: TimelineHealth
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

/** A degraded page remains visible until a fresh generation proves healthy. */
function isDegradedHealth(
  health: TimelineHealth,
): health is Extract<TimelineHealth, { kind: 'partial' | 'failed' }> {
  return health.kind === 'partial' || health.kind === 'failed'
}

/**
 * Accumulate health across accepted pages. A healthy pagination/listen page
 * cannot erase an earlier degraded page. Starting a new newest-window
 * generation can clear that warning only with an explicit (non-legacy)
 * healthy verdict; legacy or unverified hosts retain the prior warning.
 */
function aggregateTimelineHealth(
  current: TimelineHealth,
  incoming: TimelineHealth,
  initialOfGeneration: boolean,
): TimelineHealth {
  if (initialOfGeneration && incoming.kind === 'healthy' && !incoming.legacy) {
    return incoming
  }
  if (current.kind === 'failed' || incoming.kind === 'failed') {
    return current.kind === 'failed' ? current : incoming
  }
  if (isDegradedHealth(current) || isDegradedHealth(incoming)) {
    return isDegradedHealth(current) ? current : incoming
  }
  return incoming
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
  private detailGeneration = 0
  private detailInFlight: {
    generation: number
    request: AbortController
  } | null = null
  private detailQueued = false
  private timelineGeneration = 0
  private readonly pendingRequests = new Set<AbortController>()
  private readonly timelineRequests = new Set<AbortController>()
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
      timelineHealth: LEGACY_TIMELINE_HEALTH,
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

  /** Allocate one cancellable transport request owned by this store. */
  private startRequest(timeline: boolean): AbortController {
    const request = new AbortController()
    this.pendingRequests.add(request)
    if (timeline) this.timelineRequests.add(request)
    return request
  }

  private finishRequest(request: AbortController): void {
    this.pendingRequests.delete(request)
    this.timelineRequests.delete(request)
  }

  private isCurrentTimelineRequest(
    generation: number,
    request: AbortController,
  ): boolean {
    return (
      !this.disposed &&
      !request.signal.aborted &&
      generation === this.timelineGeneration
    )
  }

  private isCurrentDetailRequest(
    generation: number,
    request: AbortController,
  ): boolean {
    return (
      !this.disposed &&
      !request.signal.aborted &&
      generation === this.detailGeneration &&
      this.detailInFlight?.request === request
    )
  }

  /**
   * A newest-window request starts a new timeline generation. Abort pending
   * page/listen/refresh transports and reset their local ownership flags so
   * late settlements cannot mutate entries, health, cursors, or busy state.
   */
  private beginTimelineGeneration(): number {
    this.timelineGeneration += 1
    for (const request of this.timelineRequests) request.abort()
    this.timelineRequests.clear()
    this.paging = false
    this.listenInFlight = false
    this.listenQueued = false
    this.refreshInFlight = false
    return this.timelineGeneration
  }

  /**
   * Start the current detail generation. Once the timeline is ready, a detail
   * refresh commits header metadata only; its nested newest page must not
   * compete with the independent timeline generation/cursor state.
   */
  private startDetailRefresh(): Promise<void> {
    const generation = this.detailGeneration
    const timelineGeneration = this.timelineGeneration
    const request = this.startRequest(false)
    this.detailInFlight = { generation, request }
    const sessionId = this.state.sessionId
    const run = async (): Promise<void> => {
      try {
        const wire = await this.fetchDetailFn(sessionId, { signal: request.signal })
        if (!this.isCurrentDetailRequest(generation, request)) return
        const header = headerFromDetailWire(wire) ?? this.state.header
        if (this.state.ready) {
          // Detail refresh is independent after initialization: only metadata
          // commits here. Timeline entries/health/cursor belong to its own
          // generation and cannot be rolled back by a late detail body.
          this.setState({ header })
        } else if (
          wire.timeline === null ||
          timelineGeneration !== this.timelineGeneration
        ) {
          // Pre-M3 host placeholder contract: card data only, no timeline.
          this.setState({ header, loading: false, error: 'fusion_not_wired' })
        } else {
          const timeline = applyTimelinePage(
            createTimelineVM(sessionId),
            wire.timeline,
          )
          this.setState({
            header,
            timeline,
            timelineHealth: aggregateTimelineHealth(
              this.state.timelineHealth,
              normalizeTimelineHealth(wire.timeline),
              true,
            ),
            loading: false,
            error: null,
            hasMore: timeline.nextCursor !== null,
            ready: true,
          })
        }
        void this.loadLineage(header.agent)
      } catch (err) {
        if (!this.isCurrentDetailRequest(generation, request)) return
        if (!this.state.ready) {
          this.setState({ loading: false, error: reasonOf(err) })
          // The board hint still tells the agent kind; resolve the lineage
          // slice anyway so the panel degrades instead of spinning forever.
          void this.loadLineage(this.state.header.agent)
        }
      } finally {
        this.finishRequest(request)
        if (this.detailInFlight?.request !== request) return
        this.detailInFlight = null
        if (this.detailQueued && !this.disposed) {
          this.detailQueued = false
          void this.startDetailRefresh()
        }
      }
    }
    return run()
  }

  /**
   * Invalidate the pending detail response synchronously. Bursts retain only
   * one queued refresh carrying the latest generation; real fetch observes
   * Abort immediately, while injected transports remain protected by the
   * generation check even if they ignore cancellation.
   */
  private scheduleDetailRefresh(): void {
    if (this.disposed || !this.opened) return
    this.detailGeneration += 1
    if (this.detailInFlight !== null) {
      this.detailInFlight.request.abort()
      this.detailQueued = true
      return
    }
    void this.startDetailRefresh()
  }

  /** Initial load: header + newest timeline page, then lineage. Idempotent. */
  async open(): Promise<void> {
    if (this.opened || this.disposed) return
    this.opened = true
    this.beginTimelineGeneration()
    this.detailGeneration += 1
    this.setState({ loading: true, error: null })
    await this.startDetailRefresh()
  }

  /** Fetch one older history page via the accumulated cursor. */
  async loadMore(): Promise<void> {
    const { timeline } = this.state
    if (
      this.disposed ||
      this.paging ||
      this.refreshInFlight ||
      !this.state.ready
    ) return
    if (timeline.nextCursor === null) return
    const generation = this.timelineGeneration
    const request = this.startRequest(true)
    const sessionId = this.state.sessionId
    this.paging = true
    this.setState({ loading: true, error: null })
    try {
      const page = await this.fetchPageFn(sessionId, {
        cursor: timeline.nextCursor,
        signal: request.signal,
      })
      if (!this.isCurrentTimelineRequest(generation, request)) return
      const next = applyTimelinePage(this.state.timeline, page)
      this.setState({
        timeline: next,
        timelineHealth: aggregateTimelineHealth(
          this.state.timelineHealth,
          normalizeTimelineHealth(page),
          false,
        ),
        loading: false,
        hasMore: next.nextCursor !== null,
      })
    } catch (err) {
      if (!this.isCurrentTimelineRequest(generation, request)) return
      // Entries already shown stay visible; the view renders the reason
      // as an inline banner (deriveDetailBodyState 'list' arm).
      this.setState({ loading: false, error: reasonOf(err) })
    } finally {
      this.finishRequest(request)
      if (generation === this.timelineGeneration) this.paging = false
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
   * entries get the listen-merge highlight. Every call starts a generation:
   * a newer refresh supersedes pending paging/listen/refresh requests.
   */
  async refreshNewest(): Promise<void> {
    if (this.disposed || !this.state.ready) return
    const generation = this.beginTimelineGeneration()
    const request = this.startRequest(true)
    const sessionId = this.state.sessionId
    this.refreshInFlight = true
    this.setState({ loading: false, refreshing: true, error: null })
    try {
      const page = await this.fetchPageFn(sessionId, {
        ...(this.listenLimit !== undefined ? { limit: this.listenLimit } : {}),
        signal: request.signal,
      })
      if (!this.isCurrentTimelineRequest(generation, request)) return
      this.setState({
        timeline: applyListenPage(this.state.timeline, page),
        timelineHealth: aggregateTimelineHealth(
          this.state.timelineHealth,
          normalizeTimelineHealth(page),
          true,
        ),
        refreshing: false,
      })
    } catch (err) {
      if (!this.isCurrentTimelineRequest(generation, request)) return
      this.setState({ refreshing: false, error: reasonOf(err) })
    } finally {
      this.finishRequest(request)
      if (generation === this.timelineGeneration) this.refreshInFlight = false
    }
  }

  /**
   * SSE `state` frame hook (one call per controller notification). The card
   * hint paints immediately, then a generation-safe detail refresh resolves
   * authoritative metadata. Listen mode independently refetches the newest
   * timeline window.
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
    this.scheduleDetailRefresh()
    if (this.state.listening && this.state.ready) this.scheduleListenRefetch()
  }

  /** At most one refetch in flight; at most one more queued (idempotence). */
  private scheduleListenRefetch(): void {
    if (this.disposed || !this.state.ready || this.refreshInFlight) return
    if (this.listenInFlight) {
      this.listenQueued = true
      return
    }
    const generation = this.timelineGeneration
    const request = this.startRequest(true)
    this.listenInFlight = true
    void this.runListenRefetch(generation, request)
  }

  private async runListenRefetch(
    generation: number,
    request: AbortController,
  ): Promise<void> {
    const sessionId = this.state.sessionId
    try {
      const page = await this.fetchPageFn(sessionId, {
        ...(this.listenLimit !== undefined ? { limit: this.listenLimit } : {}),
        signal: request.signal,
      })
      if (!this.isCurrentTimelineRequest(generation, request)) return
      this.setState({
        timeline: applyListenPage(this.state.timeline, page),
        timelineHealth: aggregateTimelineHealth(
          this.state.timelineHealth,
          normalizeTimelineHealth(page),
          false,
        ),
      })
    } catch {
      // Listen refetch is best-effort: the next SSE frame retries; the
      // already-rendered timeline must not degrade into an error state.
    } finally {
      this.finishRequest(request)
      if (generation !== this.timelineGeneration || this.disposed) return
      this.listenInFlight = false
      if (this.listenQueued) {
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
      const request = this.startRequest(false)
      let body: LineageResponseVM
      try {
        body = await this.fetchLineageFn(this.state.sessionId, {
          signal: request.signal,
        })
      } finally {
        this.finishRequest(request)
      }
      if (this.disposed || request.signal.aborted) return
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
    if (this.disposed) return
    this.disposed = true
    this.detailGeneration += 1
    this.timelineGeneration += 1
    for (const request of this.pendingRequests) request.abort()
    this.pendingRequests.clear()
    this.timelineRequests.clear()
    this.detailInFlight = null
    this.detailQueued = false
    this.listenQueued = false
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
