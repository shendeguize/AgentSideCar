/**
 * FusionQuery — bypass-analysis data-fusion layer of the host half
 * (design §4.e / M3, T5.1): merges the in-process dsh session-event feed
 * with sidecar socket data into one cross-agent query surface.
 *
 * API facts verified against the installed SDK (authoritative over the
 * design sketch; both design assumptions CONFIRMED):
 *
 * - In-process dsh event feed: `@deepseek-ai/dsh-session` augments the
 *   cordis `Events` map with `'session/event'(session, event)`
 *   (lib/types/index.d.ts:66 — post-commit, fire-and-forget append feed),
 *   plus `'session/created'` (:44) and `'session/disposed'` (:54). A
 *   root-scoped listener receives all sessions (scope filtering only
 *   narrows agent-scoped listeners). `Session` exposes `id` (:122),
 *   `header` (:120 — `cwd?`/`parentSession?`/`createdAt`,
 *   types.d.ts:40-78) and the on-demand immutable log snapshot `events`
 *   (:174). `SessionEvent` is `{type, seq, time(epoch ms), data}`
 *   (types.d.ts:425-457). Titles are NOT header fields: they are
 *   log-only `'session/title'` events with `{title: string}` data
 *   (`@deepseek-ai/dsh-session-title` lib/types/index.d.ts:37-45,73),
 *   folded latest-wins here.
 * - Deep-query service: `@deepseek-ai/dsh-session-query` augments
 *   `Context` with `sessionQuery: SessionQueryEngine`
 *   (lib/types/index.d.ts:19-23). Methods used: `traceSession` (:123 →
 *   `SessionLineageTrace`, types.d.ts:59-76), `readSession` (:62 → full
 *   raw log) and `searchSessions` (:42 → `SessionSearchPage
 *   <SessionSearchHit>`, types.d.ts:224-235/:255-258). Headless or
 *   trimmed compositions may not mount it, so it is resolved lazily on
 *   every use and every capability degrades instead of throwing.
 * - Sidecar side: session rows come from the SessionStore board state
 *   (`plugin/src/session-store.ts`); `updated_at` is epoch SECONDS
 *   (sidecar/model.py:50) while dsh `time` is epoch ms, normalized here
 *   to ms. The dsh adapter reuses the raw dsh session id as
 *   `session_id` (sidecar/adapters/dsh.py:311-372), which is what makes
 *   `session_id` a valid dedup key, and conditionally mirrors the native
 *   seq into `extra.seq` (dsh.py:263-269). One dsh record may normalize
 *   into several events sharing that seq (content blocks), so timeline
 *   dedup identity is seq+kind+text, never seq alone (F1).
 *   `SessionView` today drops `extra` and
 *   `parent_id`; the store face marks them optional so the extra
 *   supplement activates when the wiring supplies fuller rows.
 *
 * Fusion rules (design §4.e):
 * - Same `session_id` dsh session: the in-process feed is the
 *   authoritative primary source (real-time); the sidecar row only
 *   supplements fields the feed lacks (status estimate, `extra`
 *   stats/plan, normalized last-event summary) and serves as the cold
 *   fallback when the session is not live in this dsh process. Non-dsh
 *   agents only ever have the sidecar source.
 * - Cross-agent correlation key: normalized project path + time window
 *   (`getProjectGroups`).
 * - No bulk event retention: dsh timelines are read on demand from the
 *   live session's own log snapshot (or `readSession` when cold);
 *   sidecar events are kept only in a small bounded per-session ring
 *   fed by the wiring, and {@link SidecarReplayFace} is the seam for the
 *   daemon `replay` op (T5.2 provides, T5.3 consumes).
 *
 * Pure DI: no cordis/dsh imports. All faces are minimal structural
 * shapes extracted from the d.ts; method-syntax members keep parameter
 * checks bivariant, so the SDK's branded `SessionId` and wider request
 * types remain assignable (same pattern as dsh-inject.ts).
 *
 * @module
 */

// ---------------------------------------------------------------------------
// In-process dsh faces (see header for d.ts provenance).
// ---------------------------------------------------------------------------

/** `SessionHeader` subset (dsh-session types.d.ts:40-78). */
export interface DshSessionHeaderFace {
  readonly createdAt: number
  readonly cwd?: string
  readonly parentSession?: string
}

/** `SessionEvent` subset (dsh-session types.d.ts:425-457). */
export interface DshSessionEventFace {
  readonly type: string
  /** Monotonic sequence number within the session. */
  readonly seq: number
  /** Unix epoch milliseconds. */
  readonly time: number
  readonly data: unknown
}

/** `Session` subset: identity, header, on-demand log snapshot (index.d.ts:106-174). */
export interface DshSessionFace {
  readonly id: string
  readonly header: DshSessionHeaderFace
  readonly events: readonly DshSessionEventFace[]
}

/**
 * In-process dsh session-event subscription face — the `ctx.on` shape
 * for the three `@deepseek-ai/dsh-session` events this layer consumes
 * (index.d.ts:44/:54/:66). Each call returns the disposer.
 */
export interface DshEventFace {
  on(
    event: 'session/event',
    handler: (session: DshSessionFace, ev: DshSessionEventFace) => void,
  ): () => void
  on(event: 'session/created', handler: (session: DshSessionFace) => void): () => void
  on(event: 'session/disposed', handler: (session: DshSessionFace) => void): () => void
  /**
   * Optional direct live-session lookup (`ctx.sessions.get`). When the
   * event subscription binds after a session was announced, this lets an
   * on-demand timeline read seed the cache without scanning every session.
   */
  get?(sessionId: string): DshSessionFace | undefined
}

// ---------------------------------------------------------------------------
// sessionQuery faces (dsh-session-query d.ts; lazily resolved, optional).
// ---------------------------------------------------------------------------

/** `SessionRecord` subset (session-query types.d.ts:14-21). */
export interface DshLineageRecordFace {
  readonly header: {
    readonly id: string
    readonly createdAt: number
    readonly cwd?: string
    readonly parentSession?: string
  }
  readonly live: boolean
  readonly persisted: boolean
}

/** `SessionLineageNode` subset (types.d.ts:52-57). */
export interface DshLineageNodeFace {
  readonly session: DshLineageRecordFace
  readonly descendants: readonly DshLineageNodeFace[]
}

/**
 * `SessionLineageTrace` subset (types.d.ts:59-76). The SDK type is a
 * discriminated union on `complete`; this face widens it so both
 * branches are assignable.
 */
export interface DshLineageTraceFace {
  readonly target: DshLineageRecordFace
  readonly ancestors: readonly DshLineageRecordFace[]
  readonly descendants: readonly DshLineageNodeFace[]
  readonly complete: boolean
  readonly root?: DshLineageRecordFace
  readonly unresolvedParentId?: string
}

/** `SessionLogSnapshot` subset (types.d.ts:32-37). */
export interface DshSessionLogFace {
  readonly events: readonly DshSessionEventFace[]
}

/** One cross-session full-text hit (`SessionSearchHit`, types.d.ts:255-258). */
export interface DshSearchHitFace {
  readonly header: { readonly id: string }
  readonly bestMatch: { readonly seq: number; readonly snippet: string }
}

/**
 * Minimal `ctx.sessionQuery` (`SessionQueryEngine`) face: lineage
 * (index.d.ts:123), cold raw-log read (:62), full-text search (:42).
 */
export interface SessionQueryFace {
  traceSession(sessionId: string, signal?: AbortSignal): Promise<DshLineageTraceFace>
  readSession(sessionId: string): Promise<DshSessionLogFace>
  searchSessions(request: {
    query: string
    limit?: number
  }): Promise<{ items: readonly DshSearchHitFace[] }>
}

// ---------------------------------------------------------------------------
// Sidecar faces (wire shapes of bridge.ts / session-store.ts, kept local
// per module-ownership rules; the real objects satisfy them structurally).
// ---------------------------------------------------------------------------

/** One normalized sidecar event (`Event.to_dict`; bridge.ts `SidecarEvent`). */
export interface SidecarEventFace {
  readonly ts: string
  readonly agent: string
  readonly session_id: string
  readonly kind: string
  readonly text: string
  readonly extra?: Record<string, unknown>
}

/**
 * One sidecar board row. `SessionView` (session-store.ts) satisfies the
 * required fields; `extra`/`parent_id` are optional because the current
 * board projection drops them — a fuller wiring adapter can supply them
 * and the extra supplement activates automatically.
 */
export interface SidecarSessionRowFace {
  readonly agent: string
  readonly session_id: string
  readonly status: string
  readonly title: string
  readonly project: string
  /** Unix epoch SECONDS (sidecar/model.py:50). */
  readonly updated_at: number
  readonly last_event?: { readonly ts: string; readonly kind: string; readonly text: string } | null
  readonly gap?: boolean
  readonly extra?: Record<string, unknown>
  readonly parent_id?: string | null
}

/** What fusion needs from the session cache (satisfied by SessionStore). */
export interface FusionStoreFace {
  getBoardState(): {
    sessions: readonly SidecarSessionRowFace[]
    streamHealth: string
  }
}

/**
 * Seam for the daemon `replay {session_id, after_seq}` op (T5.2). Absent
 * until wired; the timeline then relies on the bounded ring alone.
 */
export interface SidecarReplayFace {
  replay(request: {
    sessionId: string
    afterSeq?: number | null
  }): Promise<readonly SidecarEventFace[]>
}

// ---------------------------------------------------------------------------
// Fused result types.
// ---------------------------------------------------------------------------

/** Provenance of one unified session row. */
export type UnifiedOrigin = 'dsh-live' | 'sidecar' | 'merged'

/** One deduplicated cross-agent session row. */
export interface UnifiedSession {
  agent: string
  sessionId: string
  origin: UnifiedOrigin
  /** True when the session is live in this dsh process (in-process feed). */
  live: boolean
  /** Sidecar-inferred status; `'unknown'` when only the live feed knows the session. */
  status: string
  title: string
  project: string
  /** Unix epoch ms of the freshest signal across both sources. */
  lastActivityAt: number
  /** Sidecar last-event summary (normalized text), when known. */
  lastEvent: { readonly ts: string; readonly kind: string; readonly text: string } | null
  /** Latest known dsh seq (in-process preferred, sidecar `extra.seq` fallback). */
  lastSeq: number | null
  /** Sidecar-observed seq discontinuity flag. */
  gap: boolean
  parentId: string | null
  /** Sidecar extra supplement (stats/plan/...); `{}` when unknown. */
  extra: Record<string, unknown>
}

/** One project correlation group (cross-agent view, design §4.e.2). */
export interface ProjectGroup {
  project: string
  /** Distinct agents active in the group, sorted. */
  agents: string[]
  /** Member sessions, most recent first. */
  sessions: UnifiedSession[]
  lastActivityAt: number
}

/** One merged timeline entry. */
export interface TimelineEntry {
  origin: 'dsh' | 'sidecar'
  /** dsh log seq (native or mirrored via sidecar `extra.seq`); null when unknown. */
  seq: number | null
  /** Unix epoch ms. */
  ts: number
  /** dsh native event type for dsh entries, normalized kind for sidecar entries. */
  kind: string
  /** Normalized sidecar text; `''` for dsh entries without a sidecar twin. */
  text: string
  /** Raw dsh event data (undefined for sidecar-only entries). */
  data: unknown
  /** Sidecar extra of this entry or of the deduplicated twin. */
  extra: Record<string, unknown> | null
}

/** Backward-pagination cursor: identity of the oldest entry of a page. */
export interface TimelineCursor {
  seq: number | null
  ts: number
}

/** Which sources contributed to a timeline page. */
export interface TimelineSources {
  dshLive: boolean
  dshCold: boolean
  sidecarReplay: boolean
  sidecarBuffer: boolean
}

/** Stable, content-free result of consulting one timeline source. */
export type TimelineSourceOutcome =
  | 'succeeded'
  | 'unavailable'
  | 'not_found'
  | 'replay_unsupported'
  | 'source_failed'

/** Per-source observability; `sessionQuery` is the legacy `dshCold` source. */
export interface TimelineSourceOutcomes {
  liveSession: TimelineSourceOutcome
  sessionQuery: TimelineSourceOutcome
  sidecarReplay: TimelineSourceOutcome
  buffer: TimelineSourceOutcome
}

/** Stable page-level degradation reason (never an upstream error detail). */
export type TimelineDegradedReason =
  | 'partial_source_failure'
  | 'all_sources_failed'
  | null

/** One on-demand timeline page (entries ascending). */
export interface TimelinePage {
  sessionId: string
  entries: TimelineEntry[]
  /** Cursor for the next older page; null when the page reached the log start. */
  cursor: TimelineCursor | null
  /** Legacy contribution flags retained for source counts and existing clients. */
  sources: TimelineSources
  sourceOutcomes: TimelineSourceOutcomes
  degraded: boolean
  reason: TimelineDegradedReason
}

/** `getLineage` result; degrades instead of throwing (design §4.e.4). */
export interface LineageResult {
  available: boolean
  trace: DshLineageTraceFace | null
  reason: 'session_query_unavailable' | 'trace_failed' | null
  detail?: string
}

/** Capability advertisement for the UI (honest degradation labelling). */
export interface FusionCapabilities {
  dshEvents: { available: boolean; liveSessions: number }
  sessionQuery: { available: boolean; reason: 'session_query_unavailable' | null }
  search: { mode: 'full-text' | 'filter-only' }
}

/** One search match. */
export interface SearchMatch {
  session: UnifiedSession
  matchedBy: 'full-text' | 'title' | 'project'
  /** Best-match excerpt (full-text hits only). */
  snippet: string | null
}

/** Search result; `filter-only` is the sessionQuery-absent degradation. */
export interface SearchResult {
  mode: 'full-text' | 'filter-only'
  items: SearchMatch[]
}

// ---------------------------------------------------------------------------
// Tunables.
// ---------------------------------------------------------------------------

/** Default timeline page size. */
export const DEFAULT_TIMELINE_LIMIT = 100
/** Default project correlation window (matches `ui.time-window-hours` = 24). */
export const DEFAULT_PROJECT_WINDOW_MS = 24 * 60 * 60 * 1000
/** Default search page size. */
export const DEFAULT_SEARCH_LIMIT = 50
/** Per-session bound of the sidecar event ring (hints, not a store). */
export const DEFAULT_MAX_BUFFERED_EVENTS_PER_SESSION = 200
/** Bound on distinct session rings (least-recently-fed evicted first). */
export const DEFAULT_MAX_BUFFERED_SESSIONS = 256

const DSH_AGENT = 'dsh'
const KEY_SEP = '\u0000'
const ANALYSIS_SESSION_PREFIX = 'agent-sidecar-analysis-'

/** Analysis sessions are private tool sessions and cannot become board data. */
function isAnalysisSession(
  sessionId: string,
  extra?: Record<string, unknown>,
): boolean {
  return sessionId.startsWith(ANALYSIS_SESSION_PREFIX)
    || extra?.['agentSidecarAnalysis'] === true
}

// ---------------------------------------------------------------------------
// Internals.
// ---------------------------------------------------------------------------

interface DshLiveEntry {
  session: DshSessionFace
  /** Latest-wins fold of `session/title` events; null before the first title. */
  title: string | null
  lastSeq: number | null
  /** Unix epoch ms of the latest observed event. */
  lastEventAt: number | null
}

function integerOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) ? value : null
}

function secondsToMs(seconds: number): number {
  return typeof seconds === 'number' && Number.isFinite(seconds) ? Math.round(seconds * 1000) : 0
}

function parseTs(ts: string): number {
  const ms = Date.parse(ts)
  return Number.isFinite(ms) ? ms : 0
}

/** Latest-wins `session/title` payload fold (`{title: string}`). */
function extractTitle(data: unknown): string | null {
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return null
  const title = (data as Record<string, unknown>)['title']
  return typeof title === 'string' && title !== '' ? title : null
}

/** Correlation-key normalization: strip trailing slashes (keep root `/`). */
function normalizeProject(project: string): string {
  if (project.length > 1 && project.endsWith('/')) {
    const stripped = project.replace(/\/+$/, '')
    return stripped === '' ? '/' : stripped
  }
  return project
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Which outcomes count as the page being degraded.
 *
 * `replay_unsupported` is deliberately NOT one of them. It means the daemon
 * understood the request and answered that this session's transcript shape
 * has no replay — the same category of answer as a source that was never
 * wired, not a source that broke. Counting it as a failure painted every
 * such session's timeline as degraded and, when it was the only usable
 * source, as `all_sources_failed`, which told the operator their data was
 * lost when nothing was wrong.
 */
const TIMELINE_FAILURE_OUTCOMES = new Set<TimelineSourceOutcome>(['source_failed'])

/** Outcomes that mean "this source had nothing to contribute", not "it broke". */
const TIMELINE_INERT_OUTCOMES = new Set<TimelineSourceOutcome>([
  'unavailable',
  'not_found',
  'replay_unsupported',
])

/**
 * Extract only a bounded, known machine code for internal classification.
 * The returned timeline contract never includes this value or the upstream
 * message, which may contain paths, ids, prompts, or other private content.
 */
function knownTimelineErrorCode(error: unknown): string | null {
  const knownCodes = [
    'unknown_session',
    'not_found',
    'SESSION_NOT_FOUND',
    'replay_unsupported',
  ] as const
  if (typeof error === 'object' && error !== null) {
    const record = error as Record<string, unknown>
    for (const key of ['code', 'errorCode'] as const) {
      const value = record[key]
      if (typeof value === 'string' && knownCodes.some((code) => value === code)) return value
    }
  }
  if (error instanceof Error) {
    for (const code of knownCodes) {
      if (
        error.message === code ||
        error.message.startsWith(`${code}:`) ||
        error.message.startsWith(`${code} `)
      ) {
        return code
      }
    }
  }
  return null
}

function classifySourceFailure(error: unknown): TimelineSourceOutcome {
  const code = knownTimelineErrorCode(error)
  return code === 'unknown_session' || code === 'not_found' || code === 'SESSION_NOT_FOUND'
    ? 'not_found'
    : 'source_failed'
}

function classifyReplayFailure(error: unknown): TimelineSourceOutcome {
  return knownTimelineErrorCode(error) === 'replay_unsupported'
    ? 'replay_unsupported'
    : classifySourceFailure(error)
}

function degradationOf(sourceOutcomes: TimelineSourceOutcomes, entriesEmpty: boolean): {
  degraded: boolean
  reason: TimelineDegradedReason
} {
  const outcomes = Object.values(sourceOutcomes)
  const failures = outcomes.filter((outcome) => TIMELINE_FAILURE_OUTCOMES.has(outcome))
  if (failures.length === 0) return { degraded: false, reason: null }
  const usable = outcomes.filter((outcome) => !TIMELINE_INERT_OUTCOMES.has(outcome))
  return {
    degraded: true,
    reason:
      entriesEmpty &&
      usable.length > 0 &&
      usable.every((outcome) => TIMELINE_FAILURE_OUTCOMES.has(outcome))
        ? 'all_sources_failed'
        : 'partial_source_failure',
  }
}

/**
 * Dedup identity of one sidecar event within a single session's timeline.
 * The dsh adapter legally normalizes ONE dsh record into SEVERAL events
 * sharing the same `extra.seq` (reasoning+text blocks of an assistant
 * message, multi-block user messages, spliced inbox inserts —
 * sidecar/adapters/dsh.py content_block_events), and no per-block ordinal
 * exists on the wire — so seq alone would silently drop sibling events.
 * The identity is therefore `seq+kind+text`: the same underlying event
 * seen through both replay and the ring still collapses (identical
 * normalized kind/text), while same-seq siblings stay distinct. Must stay
 * in sync with the client mirror (client/detail/logic.ts `entryKey`).
 */
function sidecarEventKey(ev: SidecarEventFace): string {
  const seq = integerOrNull(ev.extra?.['seq'])
  return seq !== null
    ? `s:${seq}${KEY_SEP}${ev.kind}${KEY_SEP}${ev.text}`
    : `t:${ev.ts}${KEY_SEP}${ev.kind}${KEY_SEP}${ev.text}`
}

/** Strictly-older-than-cursor predicate over the merged ascending order. */
function isBeforeCursor(entry: TimelineEntry, cursor: TimelineCursor): boolean {
  if (cursor.seq !== null && entry.seq !== null) return entry.seq < cursor.seq
  return entry.ts < cursor.ts
}

/** One seq-carrying sidecar event as its own timeline entry. */
function sidecarSeqEntry(seq: number, ev: SidecarEventFace): TimelineEntry {
  return {
    origin: 'sidecar',
    seq,
    ts: parseTs(ev.ts),
    kind: ev.kind,
    text: ev.text,
    data: undefined,
    extra: ev.extra ?? null,
  }
}

/**
 * Merge dsh events (authoritative seq domain) with sidecar events.
 * One dsh record can normalize into several sidecar events sharing the
 * same `extra.seq` (multi-block messages), so twins are grouped per seq:
 * the FIRST twin folds into the matching dsh entry (normalized text +
 * extra supplement, dsh primary) and every further sibling stays its own
 * entry — dropping siblings would silently lose blocks (F1). Seq-carrying
 * entries keep exact seq order (same-seq groups keep dsh-then-block
 * arrival order via the stable sort); seq-less entries interleave by
 * timestamp.
 */
function mergeTimeline(
  dshEvents: readonly DshSessionEventFace[],
  sidecarEvents: readonly SidecarEventFace[],
): TimelineEntry[] {
  const twinsBySeq = new Map<number, SidecarEventFace[]>()
  const unseqed: TimelineEntry[] = []
  for (const ev of sidecarEvents) {
    const seq = integerOrNull(ev.extra?.['seq'])
    if (seq !== null) {
      const group = twinsBySeq.get(seq)
      if (group === undefined) twinsBySeq.set(seq, [ev])
      else group.push(ev)
    } else {
      unseqed.push({
        origin: 'sidecar',
        seq: null,
        ts: parseTs(ev.ts),
        kind: ev.kind,
        text: ev.text,
        data: undefined,
        extra: ev.extra ?? null,
      })
    }
  }

  const seqDomain: TimelineEntry[] = []
  const dshSeqs = new Set<number>()
  for (const ev of dshEvents) {
    const twins = dshSeqs.has(ev.seq) ? undefined : twinsBySeq.get(ev.seq)
    dshSeqs.add(ev.seq)
    const first = twins?.[0]
    seqDomain.push({
      origin: 'dsh',
      seq: ev.seq,
      ts: ev.time,
      kind: ev.type,
      text: first?.text ?? '',
      data: ev.data,
      extra: first?.extra ?? null,
    })
    if (twins !== undefined) {
      for (let i = 1; i < twins.length; i += 1) {
        const sibling = twins[i]
        if (sibling !== undefined) seqDomain.push(sidecarSeqEntry(ev.seq, sibling))
      }
    }
  }
  for (const [seq, twins] of twinsBySeq) {
    if (dshSeqs.has(seq)) continue
    for (const ev of twins) seqDomain.push(sidecarSeqEntry(seq, ev))
  }
  // Array.prototype.sort is stable: same-seq entries keep their push
  // order (dsh entry first, then siblings in block/arrival order).
  seqDomain.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0))
  unseqed.sort((a, b) => a.ts - b.ts)

  // Two-pointer merge by ts (tie: seq domain first) preserves exact seq
  // order even when event clocks tie or wobble within the seq domain.
  const out: TimelineEntry[] = []
  let i = 0
  let j = 0
  for (;;) {
    const a = seqDomain[i]
    const b = unseqed[j]
    if (a === undefined && b === undefined) break
    if (b === undefined || (a !== undefined && a.ts <= b.ts)) {
      if (a !== undefined) {
        out.push(a)
        i += 1
      }
    } else {
      out.push(b)
      j += 1
    }
  }
  return out
}

// ---------------------------------------------------------------------------
// FusionQuery.
// ---------------------------------------------------------------------------

export interface FusionQueryOptions {
  /** Sidecar session cache (read-only board state). */
  store: FusionStoreFace
  /** In-process dsh event feed; absent → sidecar-only fusion (no error). */
  dshEvents?: DshEventFace | null
  /**
   * Lazy `ctx.sessionQuery` resolver, re-evaluated on every use (the
   * service may mount late or never); undefined/null/throw → absent.
   */
  getSessionQuery?: (() => SessionQueryFace | null | undefined) | null
  /** Daemon replay op seam (T5.2); absent until wired. */
  replay?: SidecarReplayFace | null
  /** Clock override for tests. */
  now?: () => number
  maxBufferedEventsPerSession?: number
  maxBufferedSessions?: number
}

/**
 * The fused query surface. Lifecycle: `start()` subscribes to the dsh
 * feed, `stop()` disposes subscriptions and drops all cached state; the
 * wiring feeds the sidecar subscribe stream through
 * {@link ingestSidecarEvent}. All query methods are on-demand pulls.
 */
export class FusionQuery {
  private readonly store: FusionStoreFace
  private readonly dshEvents: DshEventFace | null
  private readonly getSessionQueryThunk: (() => SessionQueryFace | null | undefined) | null
  private readonly replaySource: SidecarReplayFace | null
  private readonly now: () => number
  private readonly maxEventsPerSession: number
  private readonly maxSessions: number

  /** Live in-process dsh sessions keyed by session id. */
  private readonly live = new Map<string, DshLiveEntry>()
  /** Bounded per-session sidecar event rings; insertion order = feed recency. */
  private readonly buffers = new Map<string, SidecarEventFace[]>()
  private disposers: Array<() => void> = []
  private started = false

  constructor(opts: FusionQueryOptions) {
    this.store = opts.store
    this.dshEvents = opts.dshEvents ?? null
    this.getSessionQueryThunk = opts.getSessionQuery ?? null
    this.replaySource = opts.replay ?? null
    this.now = opts.now ?? Date.now
    this.maxEventsPerSession =
      opts.maxBufferedEventsPerSession ?? DEFAULT_MAX_BUFFERED_EVENTS_PER_SESSION
    this.maxSessions = opts.maxBufferedSessions ?? DEFAULT_MAX_BUFFERED_SESSIONS
    if (this.maxEventsPerSession <= 0 || this.maxSessions <= 0) {
      throw new RangeError('fusion buffer bounds are invalid')
    }
  }

  /** Subscribe to the in-process feed (idempotent). */
  start(): void {
    if (this.started) return
    this.started = true
    if (this.dshEvents === null) return
    this.disposers.push(
      this.dshEvents.on('session/created', (session) => {
        this.ensureLive(session)
      }),
      this.dshEvents.on('session/event', (session, ev) => {
        this.handleDshEvent(session, ev)
      }),
      this.dshEvents.on('session/disposed', (session) => {
        this.live.delete(session.id)
      }),
    )
  }

  /** Dispose subscriptions and drop all cached state (idempotent). */
  stop(): void {
    if (!this.started) return
    this.started = false
    const disposers = this.disposers
    this.disposers = []
    for (const dispose of disposers) dispose()
    this.live.clear()
    this.buffers.clear()
  }

  /**
   * Feed one sidecar subscribe-stream event into the bounded ring
   * (timeline hints only; the stream stays a trigger signal, ADR-2).
   */
  ingestSidecarEvent(ev: SidecarEventFace): void {
    if (typeof ev.session_id !== 'string' || ev.session_id === '') return
    let ring = this.buffers.get(ev.session_id)
    if (ring === undefined) {
      ring = []
    } else {
      // Delete + re-set keeps Map insertion order as feed recency.
      this.buffers.delete(ev.session_id)
    }
    ring.push(ev)
    if (ring.length > this.maxEventsPerSession) {
      ring.splice(0, ring.length - this.maxEventsPerSession)
    }
    this.buffers.set(ev.session_id, ring)
    while (this.buffers.size > this.maxSessions) {
      const oldest = this.buffers.keys().next()
      if (oldest.done === true) break
      this.buffers.delete(oldest.value)
    }
  }

  /**
   * Deduplicated cross-agent session list, most recently active first.
   * dsh sessions live in this process win over their sidecar rows
   * (which then only supplement); cold dsh sessions and non-dsh agents
   * come from the sidecar alone.
   */
  getUnifiedSessions(): UnifiedSession[] {
    const board = this.store.getBoardState()
    const out = new Map<string, UnifiedSession>()
    const mergedIds = new Set<string>()
    for (const row of board.sessions) {
      if (isAnalysisSession(row.session_id, row.extra)) continue
      const liveEntry = row.agent === DSH_AGENT ? this.live.get(row.session_id) : undefined
      if (liveEntry !== undefined) {
        mergedIds.add(row.session_id)
        out.set(`${row.agent}${KEY_SEP}${row.session_id}`, this.mergeRow(liveEntry, row))
      } else {
        out.set(`${row.agent}${KEY_SEP}${row.session_id}`, fromSidecarRow(row))
      }
    }
    for (const [id, entry] of this.live) {
      if (isAnalysisSession(id)) continue
      if (mergedIds.has(id)) continue
      out.set(`${DSH_AGENT}${KEY_SEP}${id}`, fromDshLive(entry))
    }
    const sessions = [...out.values()]
    sessions.sort(
      (a, b) => b.lastActivityAt - a.lastActivityAt || a.sessionId.localeCompare(b.sessionId),
    )
    return sessions
  }

  /**
   * Cross-agent project correlation groups within a time window
   * (project path + window is the correlation key, design §4.e.2).
   */
  getProjectGroups(opts: { windowMs?: number; now?: number } = {}): ProjectGroup[] {
    const windowMs = opts.windowMs ?? DEFAULT_PROJECT_WINDOW_MS
    const cutoff = (opts.now ?? this.now()) - windowMs
    const groups = new Map<string, ProjectGroup>()
    for (const session of this.getUnifiedSessions()) {
      if (session.lastActivityAt < cutoff) continue
      const project = normalizeProject(session.project)
      let group = groups.get(project)
      if (group === undefined) {
        group = { project, agents: [], sessions: [], lastActivityAt: 0 }
        groups.set(project, group)
      }
      group.sessions.push(session)
      if (!group.agents.includes(session.agent)) group.agents.push(session.agent)
      if (session.lastActivityAt > group.lastActivityAt) {
        group.lastActivityAt = session.lastActivityAt
      }
    }
    const out = [...groups.values()]
    for (const group of out) group.agents.sort()
    out.sort((a, b) => b.lastActivityAt - a.lastActivityAt || a.project.localeCompare(b.project))
    return out
  }

  /**
   * One merged timeline page for a session, ascending, deduplicated by
   * event identity (seq+kind+text for seq-carrying events — same-seq
   * sibling events from multi-block records all survive), newest window
   * first with a backward cursor. Sources are pulled on demand; a
   * missing/failing source narrows the page while `sourceOutcomes`,
   * `degraded`, and `reason` retain content-free observability.
   */
  async getSessionTimeline(
    sessionId: string,
    opts: { limit?: number; before?: TimelineCursor | null } = {},
  ): Promise<TimelinePage> {
    const limit = Math.max(1, Math.floor(opts.limit ?? DEFAULT_TIMELINE_LIMIT))
    const sources: TimelineSources = {
      dshLive: false,
      dshCold: false,
      sidecarReplay: false,
      sidecarBuffer: false,
    }
    const sourceOutcomes: TimelineSourceOutcomes = {
      liveSession: this.dshEvents === null ? 'unavailable' : 'not_found',
      sessionQuery: 'unavailable',
      sidecarReplay: this.replaySource === null ? 'unavailable' : 'not_found',
      buffer: 'not_found',
    }

    let dshEvents: readonly DshSessionEventFace[] = []
    let liveEntry = this.live.get(sessionId)
    if (liveEntry === undefined && this.dshEvents?.get !== undefined) {
      try {
        const session = this.dshEvents.get(sessionId)
        if (session !== undefined) liveEntry = this.ensureLive(session)
      } catch (error) {
        sourceOutcomes.liveSession = classifySourceFailure(error)
      }
    }
    if (liveEntry !== undefined) {
      dshEvents = liveEntry.session.events
      sources.dshLive = true
      sourceOutcomes.liveSession = 'succeeded'
    } else {
      const resolved = this.resolveSessionQueryForTimeline()
      const engine = resolved.engine
      sourceOutcomes.sessionQuery = resolved.outcome
      if (engine !== null) {
        try {
          dshEvents = (await engine.readSession(sessionId)).events
          sources.dshCold = true
          sourceOutcomes.sessionQuery = 'succeeded'
        } catch (error) {
          sourceOutcomes.sessionQuery = classifySourceFailure(error)
        }
      }
    }

    const sidecarEvents: SidecarEventFace[] = []
    const seen = new Set<string>()
    const addSidecar = (ev: SidecarEventFace): boolean => {
      const key = sidecarEventKey(ev)
      if (seen.has(key)) return false
      seen.add(key)
      sidecarEvents.push(ev)
      return true
    }
    if (this.replaySource !== null) {
      try {
        const replayed = await this.replaySource.replay({ sessionId })
        for (const ev of replayed) addSidecar(ev)
        sources.sidecarReplay = true
        sourceOutcomes.sidecarReplay = 'succeeded'
      } catch (error) {
        sourceOutcomes.sidecarReplay = classifyReplayFailure(error)
      }
    }
    const ring = this.buffers.get(sessionId)
    if (ring !== undefined && ring.length > 0) {
      sources.sidecarBuffer = true
      sourceOutcomes.buffer = 'succeeded'
      for (const ev of ring) addSidecar(ev)
    }

    const entries = mergeTimeline(dshEvents, sidecarEvents)

    let endIdx = entries.length
    const before = opts.before ?? null
    if (before !== null) {
      endIdx = 0
      while (endIdx < entries.length) {
        const entry = entries[endIdx]
        if (entry === undefined || !isBeforeCursor(entry, before)) break
        endIdx += 1
      }
    }
    let startIdx = Math.max(0, endIdx - limit)
    // Never split a same-seq sibling group across the page boundary: the
    // cursor identifies entries by seq (strictly-older predicate), so a
    // group straddling it would lose its older siblings to pagination.
    // Widening keeps the cursor group-aligned at a small bounded overshoot.
    const boundary = entries[startIdx]
    if (boundary !== undefined && boundary.seq !== null) {
      while (startIdx > 0 && entries[startIdx - 1]?.seq === boundary.seq) startIdx -= 1
    }
    const window = entries.slice(startIdx, endIdx)
    const first = window[0]
    const cursor =
      startIdx > 0 && first !== undefined ? { seq: first.seq, ts: first.ts } : null
    const degradation = degradationOf(sourceOutcomes, entries.length === 0)
    return {
      sessionId,
      entries: window,
      cursor,
      sources,
      sourceOutcomes,
      ...degradation,
    }
  }

  /**
   * dsh lineage via `sessionQuery.traceSession`; degrades to
   * `trace: null` + reason when the service is absent or the trace
   * fails (never throws).
   */
  async getLineage(sessionId: string): Promise<LineageResult> {
    const engine = this.resolveSessionQuery()
    if (engine === null) {
      return { available: false, trace: null, reason: 'session_query_unavailable' }
    }
    try {
      const trace = await engine.traceSession(sessionId)
      return { available: true, trace, reason: null }
    } catch (error) {
      return {
        available: false,
        trace: null,
        reason: 'trace_failed',
        detail: describeError(error),
      }
    }
  }

  /**
   * Cross-agent search. With sessionQuery mounted, dsh sessions get
   * full-text ranking (hits first, engine order); without it — or when
   * the engine call fails — the deep query degrades to title/project
   * substring filtering over the unified view (`filter-only`), without
   * error. Non-dsh agents always use the filter path (the sidecar has
   * no search API).
   */
  async searchSessions(query: string, opts: { limit?: number } = {}): Promise<SearchResult> {
    const limit = Math.max(1, Math.floor(opts.limit ?? DEFAULT_SEARCH_LIMIT))
    const needle = query.trim().toLowerCase()
    const engine = this.resolveSessionQuery()
    let mode: SearchResult['mode'] = engine !== null ? 'full-text' : 'filter-only'
    if (needle === '') return { mode, items: [] }

    const unified = this.getUnifiedSessions()
    const items: SearchMatch[] = []
    const seen = new Set<string>()

    if (engine !== null) {
      try {
        const page = await engine.searchSessions({ query, limit })
        const dshById = new Map<string, UnifiedSession>()
        for (const session of unified) {
          if (session.agent === DSH_AGENT) dshById.set(session.sessionId, session)
        }
        for (const hit of page.items) {
          const session = dshById.get(hit.header.id)
          // Hits outside the unified view (evicted from every local
          // source) have nothing to attach to and are skipped.
          if (session === undefined) continue
          const key = `${session.agent}${KEY_SEP}${session.sessionId}`
          if (seen.has(key)) continue
          seen.add(key)
          items.push({ session, matchedBy: 'full-text', snippet: hit.bestMatch.snippet })
        }
      } catch {
        mode = 'filter-only'
      }
    }

    for (const session of unified) {
      const key = `${session.agent}${KEY_SEP}${session.sessionId}`
      if (seen.has(key)) continue
      if (session.title.toLowerCase().includes(needle)) {
        seen.add(key)
        items.push({ session, matchedBy: 'title', snippet: null })
      } else if (session.project.toLowerCase().includes(needle)) {
        seen.add(key)
        items.push({ session, matchedBy: 'project', snippet: null })
      }
    }
    return { mode, items: items.slice(0, limit) }
  }

  /** Current capability face (sessionQuery re-resolved on every call). */
  getCapabilities(): FusionCapabilities {
    const engineAvailable = this.resolveSessionQuery() !== null
    return {
      dshEvents: { available: this.dshEvents !== null, liveSessions: this.live.size },
      sessionQuery: {
        available: engineAvailable,
        reason: engineAvailable ? null : 'session_query_unavailable',
      },
      search: { mode: engineAvailable ? 'full-text' : 'filter-only' },
    }
  }

  // -------------------------------------------------------------------------

  private resolveSessionQueryForTimeline(): {
    engine: SessionQueryFace | null
    outcome: TimelineSourceOutcome
  } {
    if (this.getSessionQueryThunk === null) {
      return { engine: null, outcome: 'unavailable' }
    }
    try {
      const engine = this.getSessionQueryThunk() ?? null
      return engine === null
        ? { engine: null, outcome: 'unavailable' }
        : { engine, outcome: 'not_found' }
    } catch (error) {
      return { engine: null, outcome: classifySourceFailure(error) }
    }
  }

  private resolveSessionQuery(): SessionQueryFace | null {
    if (this.getSessionQueryThunk === null) return null
    try {
      return this.getSessionQueryThunk() ?? null
    } catch {
      return null
    }
  }

  /**
   * Register a live session (first `session/created` or, when the feed
   * attached late, first `session/event`), folding title/seq facts from
   * the existing log tail without copying it.
   */
  private ensureLive(session: DshSessionFace): DshLiveEntry {
    let entry = this.live.get(session.id)
    if (entry !== undefined) return entry
    const events = session.events
    const tail = events.length > 0 ? events[events.length - 1] : undefined
    let title: string | null = null
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const ev = events[i]
      if (ev !== undefined && ev.type === 'session/title') {
        const candidate = extractTitle(ev.data)
        if (candidate !== null) {
          title = candidate
          break
        }
      }
    }
    entry = {
      session,
      title,
      lastSeq: tail !== undefined ? tail.seq : null,
      lastEventAt: tail !== undefined ? tail.time : null,
    }
    this.live.set(session.id, entry)
    return entry
  }

  private handleDshEvent(session: DshSessionFace, ev: DshSessionEventFace): void {
    const entry = this.ensureLive(session)
    if (entry.lastSeq === null || ev.seq > entry.lastSeq) entry.lastSeq = ev.seq
    if (entry.lastEventAt === null || ev.time > entry.lastEventAt) entry.lastEventAt = ev.time
    if (ev.type === 'session/title') {
      const title = extractTitle(ev.data)
      if (title !== null) entry.title = title
    }
  }

  /** Merge one live dsh entry with its sidecar row (dsh primary). */
  private mergeRow(liveEntry: DshLiveEntry, row: SidecarSessionRowFace): UnifiedSession {
    const header = liveEntry.session.header
    const dshActivityMs = liveEntry.lastEventAt ?? header.createdAt
    return {
      agent: DSH_AGENT,
      sessionId: liveEntry.session.id,
      origin: 'merged',
      live: true,
      // The in-process feed has no status estimator; the sidecar's
      // inferred status stays the best-known value (supplement role).
      status: row.status,
      title: liveEntry.title ?? row.title,
      project: header.cwd ?? row.project,
      lastActivityAt: Math.max(dshActivityMs, secondsToMs(row.updated_at)),
      lastEvent: row.last_event ?? null,
      lastSeq: liveEntry.lastSeq ?? integerOrNull(row.extra?.['seq']),
      gap: row.gap === true,
      parentId:
        header.parentSession ?? (typeof row.parent_id === 'string' ? row.parent_id : null),
      extra: row.extra ?? {},
    }
  }
}

/** Cold fallback / non-dsh row: sidecar is the only source. */
function fromSidecarRow(row: SidecarSessionRowFace): UnifiedSession {
  return {
    agent: row.agent,
    sessionId: row.session_id,
    origin: 'sidecar',
    live: false,
    status: row.status,
    title: row.title,
    project: row.project,
    lastActivityAt: secondsToMs(row.updated_at),
    lastEvent: row.last_event ?? null,
    lastSeq: integerOrNull(row.extra?.['seq']),
    gap: row.gap === true,
    parentId: typeof row.parent_id === 'string' ? row.parent_id : null,
    extra: row.extra ?? {},
  }
}

/** Live dsh session the sidecar has not (yet) observed on disk. */
function fromDshLive(entry: DshLiveEntry): UnifiedSession {
  const header = entry.session.header
  return {
    agent: DSH_AGENT,
    sessionId: entry.session.id,
    origin: 'dsh-live',
    live: true,
    status: 'unknown',
    title: entry.title ?? '',
    project: header.cwd ?? '',
    lastActivityAt: entry.lastEventAt ?? header.createdAt,
    lastEvent: null,
    lastSeq: entry.lastSeq,
    gap: false,
    parentId: header.parentSession ?? null,
    extra: {},
  }
}
