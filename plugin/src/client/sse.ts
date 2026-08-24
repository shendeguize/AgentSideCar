/**
 * StateStream — the browser half's live data feed (design §5.3 / ADR-3).
 *
 * One class, two modes, one UI-facing surface (onSnapshot/onStatus):
 *
 * - 'sse' (main channel): EventSource on `GET <prefix>/stream`. Every
 *   `state` event carries a full StateSnapshot (routes.ts pushes full
 *   snapshots; heartbeats are comment frames the browser never surfaces).
 *   On top of EventSource's native auto-reconnect, consecutive errors
 *   beyond the threshold — or a CLOSED readyState — degrade the stream:
 *   the instance is torn down and manually rebuilt on a 5s→30s capped
 *   exponential backoff. `degraded` is a visible status, never a circuit
 *   break; a successful reconnect resets the ladder.
 * - 'poll' (settings fallback, stream.mode=poll): pollFn (defaults to
 *   api.fetchState) on a dual cadence — 3s while the latest snapshot has
 *   a `working` session, 15s otherwise. `visible()` returning false
 *   pauses fetching (ticks keep running as cheap visibility checks), and
 *   {@link StateStream.pollNow} gives the visibilitychange handler an
 *   immediate resume fetch.
 *
 * The status vocabulary is unified across modes:
 * 'connecting' | 'open' | 'degraded'. stop() is terminal and tears
 * everything down: timers, the EventSource, the in-flight poll fetch
 * (aborted), and both listener sets.
 *
 * Pure data layer: no React, no slots SDK, injectable primitives only.
 *
 * @module
 */

import {
  API_PREFIX,
  fetchState,
  type AbortControllerLike,
  type AbortSignalLike,
  type StateSnapshot,
  type TimerHandle,
} from './api.ts'

/** SSE endpoint within the plugin route namespace (host: routes.ts). */
export const STREAM_PATH = `${API_PREFIX}/stream`

export type StreamMode = 'sse' | 'poll'

/** Unified connection status shown to the UI in both modes. */
export type StreamStatus = 'connecting' | 'open' | 'degraded'

export type SnapshotListener = (snapshot: StateSnapshot) => void
export type StatusListener = (status: StreamStatus) => void

/** Minimal shape of a named SSE message event. */
export interface EventSourceMessageLike {
  data?: unknown
}

/** Structural EventSource, so node tests can fake it and DOM satisfies it. */
export interface EventSourceLike {
  readonly readyState: number
  addEventListener(type: string, listener: (ev: EventSourceMessageLike) => void): void
  close(): void
}

export type EventSourceFactory = (url: string) => EventSourceLike

/** Poll-mode snapshot source; receives an abort signal wired to stop(). */
export type PollFn = (opts: { signal?: AbortSignalLike }) => Promise<StateSnapshot>

/** EventSource.CLOSED (numeric literal so fakes need no DOM constants). */
const EVENTSOURCE_CLOSED = 2

const DEFAULT_POLL_ACTIVE_MS = 3_000
const DEFAULT_POLL_IDLE_MS = 15_000
const DEFAULT_ERROR_THRESHOLD = 3
const DEFAULT_BACKOFF_BASE_MS = 5_000
const DEFAULT_BACKOFF_CAP_MS = 30_000

export interface StateStreamOptions {
  /** Stream endpoint for 'sse' mode; see {@link STREAM_PATH}. */
  url: string
  mode: StreamMode
  /** SSE transport factory. Default: `new EventSource(url)`. */
  eventSourceFactory?: EventSourceFactory
  /** Poll-mode snapshot source. Default: api.fetchState. */
  pollFn?: PollFn
  /** Poll cadence while some session is `working`. Default 3000. */
  pollActiveMs?: number
  /** Poll cadence while everything is idle. Default 15000. */
  pollIdleMs?: number
  /** Visibility gate (inverse of document.hidden); absent = always visible. */
  visible?: () => boolean
  setTimeout?: (fn: () => void, ms: number) => TimerHandle
  clearTimeout?: (handle: TimerHandle) => void
  /** Abort factory for in-flight poll fetches. Default: real AbortController. */
  createAbortController?: () => AbortControllerLike
  /** Consecutive SSE errors tolerated before degrading. Default 3. */
  errorThreshold?: number
  /** First manual-rebuild backoff delay. Default 5000. */
  backoffBaseMs?: number
  /** Manual-rebuild backoff ceiling. Default 30000. */
  backoffCapMs?: number
}

const defaultSetTimeout = (fn: () => void, ms: number): TimerHandle =>
  globalThis.setTimeout(fn, ms)

const defaultClearTimeout = (handle: TimerHandle): void => {
  globalThis.clearTimeout(handle as ReturnType<typeof globalThis.setTimeout>)
}

const defaultCreateAbortController = (): AbortControllerLike => new AbortController()

const defaultEventSourceFactory: EventSourceFactory = (url) => {
  const Ctor = (globalThis as { EventSource?: new (url: string) => unknown }).EventSource
  if (Ctor === undefined) {
    throw new Error('EventSource unavailable: inject eventSourceFactory or use poll mode')
  }
  return new Ctor(url) as EventSourceLike
}

const defaultPollFn: PollFn = (opts) => fetchState({ signal: opts.signal })

export class StateStream {
  private readonly url: string
  private readonly streamMode: StreamMode
  private readonly esFactory: EventSourceFactory
  private readonly pollFn: PollFn
  private readonly pollActiveMs: number
  private readonly pollIdleMs: number
  private readonly visibleFn: (() => boolean) | undefined
  private readonly setT: (fn: () => void, ms: number) => TimerHandle
  private readonly clearT: (handle: TimerHandle) => void
  private readonly createController: () => AbortControllerLike
  private readonly errorThreshold: number
  private readonly backoffBaseMs: number
  private readonly backoffCapMs: number

  private currentStatus: StreamStatus = 'connecting'
  private started = false
  private stopped = false
  private readonly snapshotListeners = new Set<SnapshotListener>()
  private readonly statusListeners = new Set<StatusListener>()

  // --- sse state ---
  private es: EventSourceLike | null = null
  private sseErrors = 0
  private rebuildAttempts = 0
  private rebuildTimer: TimerHandle | null = null

  // --- poll state ---
  private pollTimer: TimerHandle | null = null
  private inFlight: AbortControllerLike | null = null
  private lastSnapshot: StateSnapshot | null = null

  constructor(opts: StateStreamOptions) {
    this.url = opts.url
    this.streamMode = opts.mode
    this.esFactory = opts.eventSourceFactory ?? defaultEventSourceFactory
    this.pollFn = opts.pollFn ?? defaultPollFn
    this.pollActiveMs = opts.pollActiveMs ?? DEFAULT_POLL_ACTIVE_MS
    this.pollIdleMs = opts.pollIdleMs ?? DEFAULT_POLL_IDLE_MS
    this.visibleFn = opts.visible
    this.setT = opts.setTimeout ?? defaultSetTimeout
    this.clearT = opts.clearTimeout ?? defaultClearTimeout
    this.createController = opts.createAbortController ?? defaultCreateAbortController
    this.errorThreshold = opts.errorThreshold ?? DEFAULT_ERROR_THRESHOLD
    this.backoffBaseMs = opts.backoffBaseMs ?? DEFAULT_BACKOFF_BASE_MS
    this.backoffCapMs = opts.backoffCapMs ?? DEFAULT_BACKOFF_CAP_MS
  }

  get mode(): StreamMode {
    return this.streamMode
  }

  get status(): StreamStatus {
    return this.currentStatus
  }

  /** Begin streaming. One-shot: calling again (or after stop) is a no-op. */
  start(): void {
    if (this.started || this.stopped) return
    this.started = true
    if (this.streamMode === 'sse') this.connectSse()
    else this.pollTick()
  }

  /**
   * Terminal teardown: clears every timer, closes the EventSource, aborts
   * the in-flight poll fetch, and drops all listeners. Idempotent; a
   * stopped stream cannot be restarted (build a fresh instance instead).
   */
  stop(): void {
    if (this.stopped) return
    this.stopped = true
    if (this.rebuildTimer !== null) {
      this.clearT(this.rebuildTimer)
      this.rebuildTimer = null
    }
    if (this.pollTimer !== null) {
      this.clearT(this.pollTimer)
      this.pollTimer = null
    }
    const es = this.es
    this.es = null
    if (es !== null) es.close()
    const inFlight = this.inFlight
    this.inFlight = null
    if (inFlight !== null) inFlight.abort()
    this.snapshotListeners.clear()
    this.statusListeners.clear()
  }

  /** Subscribe to snapshots; returns the unsubscribe function. */
  onSnapshot(cb: SnapshotListener): () => void {
    if (this.stopped) return () => {}
    this.snapshotListeners.add(cb)
    return () => {
      this.snapshotListeners.delete(cb)
    }
  }

  /**
   * Subscribe to status changes; fires synchronously with the current
   * status on subscription, then on every transition. Returns the
   * unsubscribe function.
   */
  onStatus(cb: StatusListener): () => void {
    if (this.stopped) return () => {}
    this.statusListeners.add(cb)
    cb(this.currentStatus)
    return () => {
      this.statusListeners.delete(cb)
    }
  }

  /**
   * Immediate out-of-cadence poll (poll mode only). This is the
   * visibility-resume hook: wire the document's `visibilitychange` event
   * to call this when the page becomes visible again, so "resume → fetch
   * immediately" holds without waiting for the next scheduled tick. Any
   * pending tick is cancelled first, so cadence never doubles up.
   */
  pollNow(): void {
    if (!this.started || this.stopped || this.streamMode !== 'poll') return
    if (this.pollTimer !== null) {
      this.clearT(this.pollTimer)
      this.pollTimer = null
    }
    this.pollTick()
  }

  // ------------------------------------------------------------------ sse

  private connectSse(): void {
    if (this.stopped) return
    const es = this.esFactory(this.url)
    this.es = es
    es.addEventListener('open', () => {
      if (this.stopped || this.es !== es) return
      this.sseErrors = 0
      this.rebuildAttempts = 0
      this.setStatus('open')
    })
    es.addEventListener('state', (ev) => {
      if (this.stopped || this.es !== es) return
      if (typeof ev.data !== 'string') return
      let parsed: unknown
      try {
        parsed = JSON.parse(ev.data)
      } catch {
        return
      }
      if (typeof parsed !== 'object' || parsed === null) return
      this.emitSnapshot(parsed as StateSnapshot)
    })
    es.addEventListener('error', () => {
      if (this.stopped || this.es !== es) return
      this.sseErrors += 1
      if (es.readyState === EVENTSOURCE_CLOSED || this.sseErrors > this.errorThreshold) {
        this.scheduleRebuild()
      } else {
        // EventSource's native auto-reconnect is still in charge.
        this.setStatus('connecting')
      }
    })
  }

  /**
   * Take over from native auto-reconnect: close the instance, surface
   * `degraded`, and rebuild after a doubling delay capped at
   * backoffCapMs. Never gives up — the ladder just stays at the cap.
   */
  private scheduleRebuild(): void {
    const es = this.es
    this.es = null
    if (es !== null) es.close()
    this.setStatus('degraded')
    const delay = Math.min(this.backoffBaseMs * 2 ** this.rebuildAttempts, this.backoffCapMs)
    this.rebuildAttempts += 1
    this.rebuildTimer = this.setT(() => {
      this.rebuildTimer = null
      this.connectSse()
    }, delay)
  }

  // ----------------------------------------------------------------- poll

  private hasWorkingSession(): boolean {
    const snap = this.lastSnapshot
    if (snap === null) return false
    return snap.board.sessions.some((s) => s.status === 'working')
  }

  private scheduleNextPoll(): void {
    if (this.stopped) return
    const delay = this.hasWorkingSession() ? this.pollActiveMs : this.pollIdleMs
    this.pollTimer = this.setT(() => {
      this.pollTimer = null
      this.pollTick()
    }, delay)
  }

  private pollTick(): void {
    if (this.stopped) return
    if (this.visibleFn !== undefined && !this.visibleFn()) {
      // Hidden: pause fetching but keep ticking as a cheap visibility
      // check, so a tab left hidden resumes by itself at the next tick.
      this.scheduleNextPoll()
      return
    }
    if (this.inFlight !== null) {
      // A slow previous poll is still running; never overlap fetches.
      this.scheduleNextPoll()
      return
    }
    const controller = this.createController()
    this.inFlight = controller
    this.pollFn({ signal: controller.signal }).then(
      (snapshot) => {
        if (this.stopped || this.inFlight !== controller) return
        this.inFlight = null
        this.lastSnapshot = snapshot
        this.setStatus('open')
        this.emitSnapshot(snapshot)
        this.scheduleNextPoll()
      },
      () => {
        if (this.stopped || this.inFlight !== controller) return
        this.inFlight = null
        this.setStatus('degraded')
        this.scheduleNextPoll()
      },
    )
  }

  // ------------------------------------------------------------- plumbing

  private setStatus(status: StreamStatus): void {
    if (this.currentStatus === status) return
    this.currentStatus = status
    for (const cb of [...this.statusListeners]) cb(status)
  }

  private emitSnapshot(snapshot: StateSnapshot): void {
    for (const cb of [...this.snapshotListeners]) cb(snapshot)
  }
}
