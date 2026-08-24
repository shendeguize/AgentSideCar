/**
 * SidecarController — the browser half's one data controller (T2.4).
 *
 * Owns the live feed (a {@link StateStream}, default 'sse' mode on the
 * plugin's stream route) and folds it into two externally-subscribable
 * stores consumed via `useSyncExternalStore`:
 *
 * - view state: the host `StateSnapshot` mapped onto the board view models
 *   (wire `updated_at` epoch SECONDS → `updatedAtMs` epoch MILLISECONDS at
 *   this boundary, per board/logic.ts's contract), plus the composite
 *   stream health (browser stream status overrides the host-reported
 *   subscribe health — see {@link combineStreamHealth});
 * - filters: board filter state persisted to localStorage under a
 *   package-name-prefixed key; until the user touches them, the ui.*
 *   settings defaults may be adopted ({@link SidecarController.adoptConfigDefaults}).
 *
 * Pure mapping functions are exported for unit tests. Browser primitives
 * (stream, storage) are injectable so the whole controller runs under
 * plain node. No React, no slots SDK.
 *
 * @module
 */

import {
  fetchState,
  type PingInfo,
  type SessionView,
  type StateSnapshot,
  type StreamHealth,
} from './api.ts'
import { StateStream, STREAM_PATH, type StreamMode, type StreamStatus } from './sse.ts'
import {
  DEFAULT_TIME_WINDOW_HOURS,
  type BoardFilterState,
  type DaemonStateToken,
  type SessionCardVM,
  type StreamHealthToken,
} from './board/logic.ts'

/**
 * Package name, used as the localStorage key prefix and the style-tag owner
 * mark. Mirrors package.json `name` (client code cannot read package.json at
 * runtime); pinned against it by test/client-integration.test.ts.
 */
export const PLUGIN_ID = '@shendeguize/dsh-agent-sidecar'

/** localStorage key for the persisted board filters. */
export const FILTERS_STORAGE_KEY = `${PLUGIN_ID}:board-filters`

// ---------------------------------------------------------------------------
// View state and pure mapping (exported for tests).
// ---------------------------------------------------------------------------

/** Everything the three mounted surfaces render from. */
export interface SidecarViewState {
  daemonState: DaemonStateToken
  /** Last successful daemon ping, if any. */
  lastPing: PingInfo | null
  /** Hover detail for the daemon badge, e.g. "pid 123 · v0.6.0". */
  daemonDetail: string | undefined
  /** Composite health: browser stream status folded over host-reported health. */
  streamHealth: StreamHealthToken
  /** Raw browser-side stream status. */
  streamStatus: StreamStatus
  lastReconcileAtMs: number | null
  sessions: SessionCardVM[]
  /** Host capabilities.inject (M2 write surface; informational in M1). */
  injectCapability: boolean
  /** False until the first snapshot arrives in this page life. */
  hasSnapshot: boolean
}

/** Pre-first-snapshot state: probing daemon, unknown health, empty board. */
export function initialViewState(): SidecarViewState {
  return {
    daemonState: 'probe',
    lastPing: null,
    daemonDetail: undefined,
    streamHealth: 'unknown',
    streamStatus: 'connecting',
    lastReconcileAtMs: null,
    sessions: [],
    injectCapability: false,
    hasSnapshot: false,
  }
}

/**
 * Wire sessions → board card view models. The one place the epoch-seconds
 * `updated_at` becomes the epoch-milliseconds `updatedAtMs` (board/logic.ts
 * consumes milliseconds everywhere).
 */
export function mapSessions(sessions: readonly SessionView[]): SessionCardVM[] {
  return sessions.map((session) => ({
    agent: session.agent,
    sessionId: session.session_id,
    status: session.status,
    title: session.title,
    project: session.project,
    updatedAtMs: session.updated_at * 1000,
    lastEvent:
      session.last_event === null
        ? null
        : { kind: session.last_event.kind, text: session.last_event.text },
    gap: session.gap,
  }))
}

/** Daemon badge hover detail from the last ping ("pid 123 · v0.6.0"). */
export function daemonDetailOf(ping: PingInfo | null): string | undefined {
  if (ping === null) return undefined
  return `pid ${ping.pid} · v${ping.version}`
}

/**
 * Composite stream health for the UI:
 * - before the first snapshot nothing is known → 'unknown';
 * - a degraded BROWSER stream overrides (the host may still be healthy,
 *   but what this page shows is stale);
 * - otherwise the host-reported daemon-subscribe health stands ('connecting'
 *   during a native EventSource reconnect keeps the last host verdict —
 *   the reconcile timestamp already conveys staleness).
 */
export function combineStreamHealth(
  hostHealth: StreamHealth | null,
  browserStatus: StreamStatus,
  hasSnapshot: boolean,
): StreamHealthToken {
  if (!hasSnapshot || hostHealth === null) return 'unknown'
  if (browserStatus === 'degraded') return 'degraded'
  return hostHealth
}

/** Full snapshot → view state fold (pure; exported for tests). */
export function mapSnapshot(
  snapshot: StateSnapshot,
  browserStatus: StreamStatus,
): SidecarViewState {
  return {
    daemonState: snapshot.daemon.state,
    lastPing: snapshot.daemon.lastPing,
    daemonDetail: daemonDetailOf(snapshot.daemon.lastPing),
    streamHealth: combineStreamHealth(snapshot.board.streamHealth, browserStatus, true),
    streamStatus: browserStatus,
    lastReconcileAtMs: snapshot.board.lastReconcileAt,
    sessions: mapSessions(snapshot.board.sessions),
    injectCapability: snapshot.capabilities.inject,
    hasSnapshot: true,
  }
}

// ---------------------------------------------------------------------------
// Filter persistence.
// ---------------------------------------------------------------------------

/** Structural localStorage face (node tests inject a Map-backed fake). */
export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
}

/** Resolve the real localStorage; some privacy modes throw on access. */
function defaultStorage(): StorageLike | null {
  try {
    const storage = (globalThis as { localStorage?: StorageLike }).localStorage
    return storage ?? null
  } catch {
    return null
  }
}

/** Parse + validate persisted filters; anything malformed reads as absent. */
export function readStoredFilters(storage: StorageLike | null): BoardFilterState | null {
  if (storage === null) return null
  try {
    const raw = storage.getItem(FILTERS_STORAGE_KEY)
    if (raw === null) return null
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return null
    const candidate = parsed as { timeWindowHours?: unknown; showDead?: unknown }
    if (
      typeof candidate.timeWindowHours !== 'number'
      || !Number.isFinite(candidate.timeWindowHours)
      || candidate.timeWindowHours <= 0
      || typeof candidate.showDead !== 'boolean'
    ) {
      return null
    }
    return { timeWindowHours: candidate.timeWindowHours, showDead: candidate.showDead }
  } catch {
    return null
  }
}

// ---------------------------------------------------------------------------
// Controller.
// ---------------------------------------------------------------------------

/** Structural stream face ({@link StateStream} satisfies it; tests fake it). */
export interface StateStreamLike {
  readonly status: StreamStatus
  readonly mode: StreamMode
  start(): void
  stop(): void
  onSnapshot(cb: (snapshot: StateSnapshot) => void): () => void
  onStatus(cb: (status: StreamStatus) => void): () => void
  pollNow(): void
}

export interface SidecarControllerOptions {
  /** Live feed; default: `new StateStream({url: STREAM_PATH, mode: 'sse'})`. */
  stream?: StateStreamLike
  /** Filter persistence; `null` disables, absent resolves real localStorage. */
  storage?: StorageLike | null
  /** Manual-refresh fetch; default api.fetchState. */
  fetchStateFn?: typeof fetchState
  /** Visibility gate handed to the default stream (poll-mode pause). */
  visible?: () => boolean
}

const defaultVisible = (): boolean =>
  typeof document === 'undefined' || !document.hidden

/**
 * One instance per plugin apply. `start()` wires the stream, `stop()` is
 * terminal (the underlying StateStream cannot restart — a re-apply builds
 * a fresh controller).
 */
export class SidecarController {
  private readonly stream: StateStreamLike
  private readonly storage: StorageLike | null
  private readonly fetchFn: typeof fetchState
  private readonly listeners = new Set<() => void>()

  private state: SidecarViewState = initialViewState()
  private filters: BoardFilterState
  /** True once filters came from storage or a user gesture (config defaults then stop adopting). */
  private filtersTouched: boolean
  private lastHostHealth: StreamHealth | null = null
  private started = false

  constructor(opts: SidecarControllerOptions = {}) {
    this.storage = opts.storage === undefined ? defaultStorage() : opts.storage
    this.fetchFn = opts.fetchStateFn ?? fetchState
    this.stream =
      opts.stream
      ?? new StateStream({
        url: STREAM_PATH,
        mode: 'sse',
        visible: opts.visible ?? defaultVisible,
      })
    const stored = readStoredFilters(this.storage)
    this.filters = stored ?? { timeWindowHours: DEFAULT_TIME_WINDOW_HOURS, showDead: false }
    this.filtersTouched = stored !== null
  }

  /** Wire stream listeners and begin streaming (idempotent). */
  start(): void {
    if (this.started) return
    this.started = true
    this.stream.onSnapshot((snapshot) => {
      this.applySnapshot(snapshot)
    })
    this.stream.onStatus((status) => {
      this.applyStatus(status)
    })
    this.stream.start()
  }

  /** Terminal teardown of the live feed. */
  stop(): void {
    this.stream.stop()
  }

  /** Forward the visibility resume to the stream (poll-mode immediate fetch). */
  pollNow(): void {
    this.stream.pollNow()
  }

  /** Change notifications for BOTH stores (state and filters). */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /** Stable-reference view state (uSES getSnapshot source). */
  getState(): SidecarViewState {
    return this.state
  }

  /** Stable-reference filters (uSES getSnapshot source). */
  getFilters(): BoardFilterState {
    return this.filters
  }

  /** User filter change: persist (package-prefixed key) and notify. */
  setFilters(next: BoardFilterState): void {
    this.filters = { ...next }
    this.filtersTouched = true
    if (this.storage !== null) {
      try {
        this.storage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(this.filters))
      } catch {
        // Quota/privacy failures degrade to session-only filters.
      }
    }
    this.notify()
  }

  /**
   * Adopt ui.* settings defaults as the filter values — only while the user
   * has never touched the filters (no stored value, no gesture). Not
   * persisted: an untouched board keeps following the settings defaults.
   */
  adoptConfigDefaults(ui: { timeWindowHours: number; showDead: boolean }): void {
    if (this.filtersTouched) return
    if (
      this.filters.timeWindowHours === ui.timeWindowHours
      && this.filters.showDead === ui.showDead
    ) {
      return
    }
    this.filters = { timeWindowHours: ui.timeWindowHours, showDead: ui.showDead }
    this.notify()
  }

  /** Manual refresh (board's refresh button): one out-of-band snapshot pull. */
  async refresh(): Promise<void> {
    try {
      const snapshot = await this.fetchFn({})
      this.applySnapshot(snapshot)
    } catch (err) {
      // The stream (and its status surface) remains the health authority;
      // a failed manual pull only logs.
      console.error('agent-sidecar: manual refresh failed', err)
    }
  }

  private applySnapshot(snapshot: StateSnapshot): void {
    this.lastHostHealth = snapshot.board.streamHealth
    this.state = mapSnapshot(snapshot, this.stream.status)
    this.notify()
  }

  private applyStatus(status: StreamStatus): void {
    if (status === this.state.streamStatus) return
    this.state = {
      ...this.state,
      streamStatus: status,
      streamHealth: combineStreamHealth(this.lastHostHealth, status, this.state.hasSnapshot),
    }
    this.notify()
  }

  private notify(): void {
    for (const listener of [...this.listeners]) listener()
  }
}
