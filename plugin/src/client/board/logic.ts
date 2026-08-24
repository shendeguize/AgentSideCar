/**
 * Pure view-model logic for the cross-agent session board (design §5.1
 * view 1) and the footer status widget (view 4). No React, no I/O, no
 * imports from the data layer (api/sse) — everything arrives as plain
 * values and leaves as plain values, so this whole module is unit-testable
 * in a bare node environment.
 *
 * Decoupling contract (T2.2 ↔ T2.4): the types below are this module's OWN
 * view models. The integration task maps the host wire shapes
 * (`SessionView` / `StateSnapshot` from the state endpoint) onto them.
 * Notably every timestamp here is **epoch milliseconds** — the sidecar
 * snapshot carries epoch seconds (`updated_at`), so the mapping layer
 * multiplies by 1000 once at the boundary.
 *
 * @module
 */

import { BOARD_STRINGS } from './strings.ts'

// ---------------------------------------------------------------------------
// View-model types (the props contract consumed by Board.tsx / widget.tsx).
// ---------------------------------------------------------------------------

/** Normalized session status vocabulary; anything else maps to 'unknown'. */
export type SessionStatusToken = 'working' | 'waiting' | 'idle' | 'dead' | 'unknown'

/** Mirror of the host's StreamHealth union (kept local by design). */
export type StreamHealthToken = 'ok' | 'degraded' | 'unknown'

/** Mirror of the host's SupervisorState union (kept local by design). */
export type DaemonStateToken =
  | 'probe'
  | 'adopted'
  | 'defer'
  | 'reprobe'
  | 'hosting'
  | 'hosted'
  | 'backoff'
  | 'failed'

/** Color-token buckets the CSS maps to `--dsw-alias-state-*` variables. */
export type BadgeTone = 'success' | 'warn' | 'neutral' | 'muted' | 'danger'

/** One session card as fed by the integration layer (raw, underived). */
export interface SessionCardVM {
  agent: string
  sessionId: string
  /** Raw observed status string from the sidecar snapshot (open vocabulary). */
  status: string
  title: string
  /** Working directory path; empty string groups under 未知项目. */
  project: string
  /** Epoch milliseconds (mapping layer converts sidecar epoch seconds). */
  updatedAtMs: number
  /** Most recent normalized event summary, if any. */
  lastEvent: { kind: string; text: string } | null
  /** True when a dsh seq discontinuity was observed since the last reconcile. */
  gap: boolean
}

/** Board filter controls (wired to ui.time-window-hours / ui.show-dead). */
export interface BoardFilterState {
  timeWindowHours: number
  showDead: boolean
}

/** Derived status badge: color token + label + attention marker. */
export interface StatusBadgeVM {
  status: SessionStatusToken
  tone: BadgeTone
  label: string
  /** 'gap' (per-session data hole) outranks 'stale' (global stream health). */
  attention: 'gap' | 'stale' | null
  attentionLabel: string | null
}

/** A session card with every render-ready derivation attached. */
export interface DerivedSessionCardVM extends SessionCardVM {
  badge: StatusBadgeVM
  glyph: string
  shortId: string
  relativeTime: string
  /** Hover title: observed-value disclaimer + raw status + lastReconcileAt. */
  hoverTitle: string
}

/** One project section on the board. */
export interface ProjectGroupVM<T extends SessionCardVM = DerivedSessionCardVM> {
  /** Raw project path; '' for the unknown-project bucket. */
  key: string
  /** Display name (path basename, or 未知项目). */
  label: string
  /** Full path for the hover title; '' when unknown. */
  fullPath: string
  cards: T[]
}

export interface BoardBannerVM {
  tone: 'danger' | 'warn'
  text: string
}

export type BoardEmptyKind = 'daemon-failed' | 'daemon-defer' | 'filtered' | 'no-sessions'

export interface BoardEmptyStateVM {
  kind: BoardEmptyKind
  title: string
  hint: string
}

export interface DaemonBadgeVM {
  tone: BadgeTone
  label: string
}

/** Footer widget connection dot state. */
export type WidgetConnection = 'ok' | 'degraded' | 'off'

/** Everything `buildBoardViewModel` needs (all plain values). */
export interface BoardComputeInput {
  sessions: SessionCardVM[]
  filters: BoardFilterState
  daemonState: DaemonStateToken
  streamHealth: StreamHealthToken
  /** Epoch ms of the last authoritative snapshot reconcile, or null. */
  lastReconcileAtMs: number | null
  /** Clock injection (epoch ms) for deterministic derivation. */
  nowMs: number
}

/** Fully derived board render model. */
export interface BoardViewModel {
  groups: Array<ProjectGroupVM<DerivedSessionCardVM>>
  banner: BoardBannerVM | null
  emptyState: BoardEmptyStateVM | null
  daemonBadge: DaemonBadgeVM
  streamLabel: string
  streamTone: BadgeTone
  visibleCount: number
  totalCount: number
  workingCount: number
}

// ---------------------------------------------------------------------------
// Small shared helpers.
// ---------------------------------------------------------------------------

const MINUTE_MS = 60_000
const HOUR_MS = 3_600_000
const DAY_MS = 86_400_000

/** Default board time window, aligned with the ui.time-window-hours setting. */
export const DEFAULT_TIME_WINDOW_HOURS = 48

/** Resolve `{name}` placeholders in a message template. */
export function formatTemplate(
  template: string,
  params: Record<string, string | number>,
): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) => {
    const value = params[key]
    return value === undefined ? match : String(value)
  })
}

// ---------------------------------------------------------------------------
// Status normalization and ordering.
// ---------------------------------------------------------------------------

const KNOWN_STATUSES: readonly SessionStatusToken[] = ['working', 'waiting', 'idle', 'dead']

/** Map a raw observed status onto the badge vocabulary ('unknown' fallback). */
export function normalizeStatus(raw: string): SessionStatusToken {
  const cleaned = raw.trim().toLowerCase()
  return (KNOWN_STATUSES as readonly string[]).includes(cleaned)
    ? (cleaned as SessionStatusToken)
    : 'unknown'
}

const STATUS_RANK: Record<SessionStatusToken, number> = {
  working: 0,
  waiting: 1,
  idle: 2,
  unknown: 3,
  dead: 4,
}

/** Sort rank: working > waiting > idle > unknown > dead (lower sorts first). */
export function statusRank(status: SessionStatusToken): number {
  return STATUS_RANK[status]
}

const STATUS_TONE: Record<SessionStatusToken, BadgeTone> = {
  working: 'success',
  waiting: 'warn',
  idle: 'neutral',
  unknown: 'neutral',
  dead: 'muted',
}

// ---------------------------------------------------------------------------
// Time-window filtering.
// ---------------------------------------------------------------------------

/**
 * Visibility rules (task spec):
 * - dead sessions are hidden unless `showDead`;
 * - working sessions are always visible (even outside the window);
 * - everything else hides once `updatedAtMs` falls strictly beyond the
 *   window (age === window is still visible);
 * - a non-finite or non-positive window disables the age filter entirely.
 */
export function isSessionVisible(
  session: SessionCardVM,
  filters: BoardFilterState,
  nowMs: number,
): boolean {
  const status = normalizeStatus(session.status)
  if (status === 'dead' && !filters.showDead) return false
  if (status === 'working') return true
  const windowMs = filters.timeWindowHours * HOUR_MS
  if (!Number.isFinite(windowMs) || windowMs <= 0) return true
  return nowMs - session.updatedAtMs <= windowMs
}

/** Apply {@link isSessionVisible} across a session list (order preserved). */
export function filterSessions<T extends SessionCardVM>(
  sessions: readonly T[],
  filters: BoardFilterState,
  nowMs: number,
): T[] {
  return sessions.filter((s) => isSessionVisible(s, filters, nowMs))
}

// ---------------------------------------------------------------------------
// Project grouping.
// ---------------------------------------------------------------------------

/** Human display name for a project path ('' → 未知项目). */
export function projectDisplayName(project: string): string {
  const trimmed = project.trim().replace(/[/\\]+$/, '')
  if (trimmed === '') return BOARD_STRINGS.unknownProject
  const segments = trimmed.split(/[/\\]/)
  const base = segments[segments.length - 1] ?? ''
  return base === '' ? trimmed : base
}

/** Card ordering inside a group: status rank, then recency, then id. */
export function compareCards(a: SessionCardVM, b: SessionCardVM): number {
  const rankDelta = statusRank(normalizeStatus(a.status)) - statusRank(normalizeStatus(b.status))
  if (rankDelta !== 0) return rankDelta
  if (a.updatedAtMs !== b.updatedAtMs) return b.updatedAtMs - a.updatedAtMs
  return a.sessionId.localeCompare(b.sessionId)
}

/**
 * Group sessions by project. Empty/whitespace projects share the
 * unknown-project bucket (key ''). Groups are ordered by their most recent
 * `updatedAtMs` descending, except the unknown bucket which always sorts
 * last (named projects are more actionable than the catch-all). Cards
 * inside each group follow {@link compareCards}.
 */
export function groupSessions<T extends SessionCardVM>(
  sessions: readonly T[],
): Array<ProjectGroupVM<T>> {
  const buckets = new Map<string, T[]>()
  for (const session of sessions) {
    const key = session.project.trim() === '' ? '' : session.project
    const bucket = buckets.get(key)
    if (bucket === undefined) buckets.set(key, [session])
    else bucket.push(session)
  }
  const groups: Array<ProjectGroupVM<T>> = []
  for (const [key, cards] of buckets) {
    cards.sort(compareCards)
    groups.push({
      key,
      label: key === '' ? BOARD_STRINGS.unknownProject : projectDisplayName(key),
      fullPath: key,
      cards,
    })
  }
  const newest = (group: ProjectGroupVM<T>): number =>
    group.cards.reduce((max, card) => Math.max(max, card.updatedAtMs), Number.NEGATIVE_INFINITY)
  groups.sort((a, b) => {
    if (a.key === '') return b.key === '' ? 0 : 1
    if (b.key === '') return -1
    return newest(b) - newest(a) || a.key.localeCompare(b.key)
  })
  return groups
}

// ---------------------------------------------------------------------------
// Badge derivation.
// ---------------------------------------------------------------------------

/**
 * status + gap + streamHealth → badge tone/label/attention.
 *
 * Priority: a per-session `gap` marker (a known data hole for THIS session)
 * outranks the global stale marker (stream reconnecting affects everyone
 * and is already surfaced by the top banner). Unknown raw statuses keep
 * their raw text as the label — the board never invents a state.
 */
export function deriveBadge(
  rawStatus: string,
  gap: boolean,
  streamHealth: StreamHealthToken,
): StatusBadgeVM {
  const status = normalizeStatus(rawStatus)
  const trimmed = rawStatus.trim()
  const label =
    status === 'unknown'
      ? trimmed === ''
        ? BOARD_STRINGS.status.unknown
        : trimmed
      : BOARD_STRINGS.status[status]
  let attention: StatusBadgeVM['attention'] = null
  if (gap) attention = 'gap'
  else if (streamHealth !== 'ok') attention = 'stale'
  return {
    status,
    tone: STATUS_TONE[status],
    label,
    attention,
    attentionLabel: attention === null ? null : BOARD_STRINGS.attention[attention],
  }
}

/**
 * Hover title for a status badge: the observed-value disclaimer (design
 * §5.3 wording), the raw observed status, and the last reconcile time.
 */
export function badgeHoverTitle(
  rawStatus: string,
  lastReconcileAtMs: number | null,
  nowMs: number,
): string {
  const observed = rawStatus.trim() === '' ? BOARD_STRINGS.status.unknown : rawStatus.trim()
  const reconcile =
    lastReconcileAtMs === null
      ? BOARD_STRINGS.card.neverReconciled
      : formatTemplate(BOARD_STRINGS.card.lastReconcile, {
          time: formatRelativeTime(lastReconcileAtMs, nowMs),
        })
  return [
    BOARD_STRINGS.card.observedDisclaimer,
    formatTemplate(BOARD_STRINGS.card.observedValue, { status: observed }),
    reconcile,
  ].join('\n')
}

// ---------------------------------------------------------------------------
// Time formatting.
// ---------------------------------------------------------------------------

/**
 * Coarse relative time: <60s (including clock skew into the future) is
 * 刚刚, then whole minutes/hours/days. Non-finite input renders empty.
 */
export function formatRelativeTime(thenMs: number, nowMs: number): string {
  if (!Number.isFinite(thenMs)) return ''
  const delta = nowMs - thenMs
  if (delta < MINUTE_MS) return BOARD_STRINGS.time.justNow
  if (delta < HOUR_MS) {
    return formatTemplate(BOARD_STRINGS.time.minutesAgo, { n: Math.floor(delta / MINUTE_MS) })
  }
  if (delta < DAY_MS) {
    return formatTemplate(BOARD_STRINGS.time.hoursAgo, { n: Math.floor(delta / HOUR_MS) })
  }
  return formatTemplate(BOARD_STRINGS.time.daysAgo, { n: Math.floor(delta / DAY_MS) })
}

/** Label for a time-window option: whole days as 天, otherwise 小时. */
export function timeWindowLabel(hours: number): string {
  if (hours >= 24 && hours % 24 === 0) {
    return formatTemplate(BOARD_STRINGS.timeWindow.days, { n: hours / 24 })
  }
  return formatTemplate(BOARD_STRINGS.timeWindow.hours, { n: hours })
}

// ---------------------------------------------------------------------------
// Top bar / banner / empty-state derivation.
// ---------------------------------------------------------------------------

const DAEMON_TONE: Record<DaemonStateToken, BadgeTone> = {
  probe: 'neutral',
  adopted: 'success',
  defer: 'warn',
  reprobe: 'neutral',
  hosting: 'neutral',
  hosted: 'success',
  backoff: 'warn',
  failed: 'danger',
}

/** Daemon state → top-bar badge (tone + label). */
export function deriveDaemonBadge(state: DaemonStateToken): DaemonBadgeVM {
  return { tone: DAEMON_TONE[state], label: BOARD_STRINGS.daemon[state] }
}

/** Stream health → indicator tone. */
export function streamHealthTone(health: StreamHealthToken): BadgeTone {
  if (health === 'ok') return 'success'
  if (health === 'degraded') return 'warn'
  return 'neutral'
}

/**
 * Top banner: daemon FAILED (red, last-snapshot notice) outranks a
 * degraded stream (yellow, may-lag notice). 'unknown' stream health alone
 * raises no banner — it is the pre-first-connect startup state and a
 * warning bar on every mount would be noise.
 */
export function deriveBanner(
  daemonState: DaemonStateToken,
  streamHealth: StreamHealthToken,
): BoardBannerVM | null {
  if (daemonState === 'failed') {
    return { tone: 'danger', text: BOARD_STRINGS.banner.daemonFailed }
  }
  if (streamHealth === 'degraded') {
    return { tone: 'warn', text: BOARD_STRINGS.banner.streamDegraded }
  }
  return null
}

/**
 * Empty-state guidance, only when nothing is visible. Daemon trouble
 * (failed > defer) explains an empty board better than filter settings;
 * otherwise distinguish "all filtered out" from "nothing observed yet".
 */
export function deriveEmptyState(
  daemonState: DaemonStateToken,
  visibleCount: number,
  totalCount: number,
): BoardEmptyStateVM | null {
  if (visibleCount > 0) return null
  if (daemonState === 'failed') {
    return {
      kind: 'daemon-failed',
      title: BOARD_STRINGS.empty.daemonFailedTitle,
      hint: BOARD_STRINGS.empty.daemonFailedHint,
    }
  }
  if (daemonState === 'defer') {
    return {
      kind: 'daemon-defer',
      title: BOARD_STRINGS.empty.daemonDeferTitle,
      hint: BOARD_STRINGS.empty.daemonDeferHint,
    }
  }
  if (totalCount > 0) {
    return {
      kind: 'filtered',
      title: BOARD_STRINGS.empty.filteredTitle,
      hint: BOARD_STRINGS.empty.filteredHint,
    }
  }
  return {
    kind: 'no-sessions',
    title: BOARD_STRINGS.empty.noSessionsTitle,
    hint: BOARD_STRINGS.empty.noSessionsHint,
  }
}

// ---------------------------------------------------------------------------
// Card presentation helpers.
// ---------------------------------------------------------------------------

const AGENT_GLYPHS: Record<string, string> = {
  dsh: '◆',
  claude: '✳',
  codex: '▣',
  cursor: '▮',
  'cursor-cli': '▮',
  'cursor-ide': '▮',
  copilot: '◉',
  kimi: '◐',
}

/** Single-character agent marker; unknown agents get a neutral dot. */
export function agentGlyph(agent: string): string {
  return AGENT_GLYPHS[agent.trim().toLowerCase()] ?? '●'
}

/** Head…tail abbreviation for long session ids (full id goes in `title`). */
export function abbreviateSessionId(id: string, max = 20): string {
  if (id.length <= max) return id
  return `${id.slice(0, 12)}…${id.slice(-6)}`
}

// ---------------------------------------------------------------------------
// Footer widget derivation.
// ---------------------------------------------------------------------------

/**
 * Connection dot: green only when the daemon is connected (adopted/hosted)
 * AND the event stream is healthy; FAILED is the only hard-off state;
 * every transitional state shows the cautious yellow.
 */
export function deriveWidgetConnection(
  daemonState: DaemonStateToken,
  streamHealth: StreamHealthToken,
): WidgetConnection {
  if (daemonState === 'failed') return 'off'
  if ((daemonState === 'adopted' || daemonState === 'hosted') && streamHealth === 'ok') {
    return 'ok'
  }
  return 'degraded'
}

/** Count of sessions currently observed as working. */
export function countWorking(sessions: ReadonlyArray<{ status: string }>): number {
  let count = 0
  for (const session of sessions) {
    if (normalizeStatus(session.status) === 'working') count += 1
  }
  return count
}

/** Widget hover/aria text: connection state, plus the count when nonzero. */
export function widgetTitle(connection: WidgetConnection, workingCount: number): string {
  const base = `${BOARD_STRINGS.widget.label}: ${BOARD_STRINGS.widget.connection[connection]}`
  if (workingCount <= 0) return base
  return `${base} · ${formatTemplate(BOARD_STRINGS.widget.working, { n: workingCount })}`
}

// ---------------------------------------------------------------------------
// Full pipeline.
// ---------------------------------------------------------------------------

/** filter → group → per-card derive; the one call Board.tsx renders from. */
export function buildBoardViewModel(input: BoardComputeInput): BoardViewModel {
  const { sessions, filters, daemonState, streamHealth, lastReconcileAtMs, nowMs } = input
  const visible = filterSessions(sessions, filters, nowMs)
  const derived: DerivedSessionCardVM[] = visible.map((session) => ({
    ...session,
    badge: deriveBadge(session.status, session.gap, streamHealth),
    glyph: agentGlyph(session.agent),
    shortId: abbreviateSessionId(session.sessionId),
    relativeTime: formatRelativeTime(session.updatedAtMs, nowMs),
    hoverTitle: badgeHoverTitle(session.status, lastReconcileAtMs, nowMs),
  }))
  return {
    groups: groupSessions(derived),
    banner: deriveBanner(daemonState, streamHealth),
    emptyState: deriveEmptyState(daemonState, visible.length, sessions.length),
    daemonBadge: deriveDaemonBadge(daemonState),
    streamLabel: BOARD_STRINGS.stream[streamHealth],
    streamTone: streamHealthTone(streamHealth),
    visibleCount: visible.length,
    totalCount: sessions.length,
    workingCount: countWorking(sessions),
  }
}
