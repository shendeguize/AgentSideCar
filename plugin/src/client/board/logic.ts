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

/** Mirror of the host's SupervisorFailure (kept local by design). */
export interface DaemonFailure {
  /** Open at this boundary: an unknown token falls back to the plain wording. */
  reason: string
  exitCode: number | null
  detail: string | null
}

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
  /** Epoch milliseconds of session birth; absent when the adapter cannot tell. */
  createdAtMs?: number
  /** Most recent normalized event summary, if any. */
  lastEvent: { kind: string; text: string } | null
  /** True when a dsh seq discontinuity was observed since the last reconcile. */
  gap: boolean
  /** Optional model metadata; absent means the adapter did not expose it. */
  model?: string
  modelProvider?: string
}

/**
 * One archived session as the board renders it. Archiving is observational:
 * the row is hidden from the active board but the underlying agent session
 * file and process are untouched, so an archived card can come back on its
 * own the moment the session shows activity again.
 */
export interface ArchivedCardVM extends SessionCardVM {
  /** Epoch milliseconds of the archive decision. */
  archivedAtMs: number
  /** Free-form provenance token; 'manual' | 'batch' | 'auto' get a label. */
  archiveReason: string
}

/** Statuses the top-bar count badges can filter down to (UX-01). */
export type BoardStatusFilter = 'working' | 'waiting'

/** Board filter controls (wired to ui.time-window-hours / ui.show-dead). */
export interface BoardFilterState {
  timeWindowHours: number
  showDead: boolean
  /** Fold idle sessions into one expandable row per project group. */
  collapseIdle?: boolean
  /**
   * Canonical agent token selected in the toolbar. Absent means all agents.
   * Kept as a string at this boundary so stale persisted values can be
   * rejected safely instead of being asserted into the supported union.
   */
  agentFilter?: string
  /**
   * Status-only view toggled by the top-bar count badges (UX-01). While
   * set, ONLY sessions of this status are visible — the time window and
   * showDead do not apply (the user explicitly asked for the全板 answer
   * to "who is working/waiting"). Absent = no status filter.
   */
  statusFilter?: BoardStatusFilter
}

/** Derived status badge: color token + label + attention marker. */
export interface StatusBadgeVM {
  status: SessionStatusToken
  tone: BadgeTone
  label: string
  /**
   * Per-session data-hole marker. The global stream-health notice is NOT
   * mirrored here (UX-18): the top banner already carries it once.
   */
  attention: 'gap' | null
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

/**
 * A daemon still serving code the disk has since replaced. Both numbers
 * come from the daemon itself: what it is running, and what it would load
 * on restart.
 */
export interface DaemonVersionDrift {
  running: string
  installed: string
  /** Set when the two versions match but the code behind them changed. */
  codeChanged?: boolean
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
  /** Set only when the daemon is behind the code now on disk. */
  daemonDrift?: DaemonVersionDrift | null
  /** Cause of a FAILED daemon as the host reported it, else null. */
  daemonFailure?: DaemonFailure | null
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
  /** Whole-board counts (window/filter independent — the honest answer). */
  workingCount: number
  waitingCount: number
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
// Agent filtering.
// ---------------------------------------------------------------------------

/** Agent families currently understood by the installed sidecar adapters. */
export const SUPPORTED_AGENT_FILTERS = [
  'dsh',
  'claude',
  'codex',
  'cursor',
  'cursor-cli',
  'cursor-ide',
  'copilot',
  'kimi',
] as const

export type BoardAgentFilter = (typeof SUPPORTED_AGENT_FILTERS)[number]

const AGENT_DISPLAY_NAMES: Record<BoardAgentFilter, string> = {
  dsh: 'DSH',
  claude: 'Claude',
  codex: 'Codex',
  cursor: 'Cursor',
  'cursor-cli': 'Cursor CLI',
  'cursor-ide': 'Cursor IDE',
  copilot: 'GitHub Copilot',
  kimi: 'Kimi',
}

/** Normalize a supported agent token; unknown/untrusted values become null. */
export function normalizeAgentFilter(raw: string | undefined): BoardAgentFilter | null {
  if (raw === undefined) return null
  const normalized = raw.trim().toLowerCase()
  return (SUPPORTED_AGENT_FILTERS as readonly string[]).includes(normalized)
    ? (normalized as BoardAgentFilter)
    : null
}

/** Stable, recognizable display name for one supported agent token. */
export function agentDisplayName(agent: BoardAgentFilter): string {
  return AGENT_DISPLAY_NAMES[agent]
}

/**
 * Supported agents currently present on the board, in adapter-stable order.
 * A still-selected supported value remains available after its last card
 * disappears so the user can explicitly clear it. Unknown values are never
 * reflected into labels or option values.
 */
export function agentFilterOptions(
  sessions: ReadonlyArray<Pick<SessionCardVM, 'agent'>>,
  selected?: string,
): BoardAgentFilter[] {
  const available = new Set<BoardAgentFilter>()
  for (const session of sessions) {
    const agent = normalizeAgentFilter(session.agent)
    if (agent !== null) available.add(agent)
  }
  const selectedAgent = normalizeAgentFilter(selected)
  if (selectedAgent !== null) available.add(selectedAgent)
  return SUPPORTED_AGENT_FILTERS.filter((agent) => available.has(agent))
}

/**
 * Apply a toolbar agent choice. Empty/unknown values mean "All agents" and
 * clear only the agent condition, preserving every other board filter.
 */
export function withAgentFilter(
  filters: BoardFilterState,
  selected: string,
): BoardFilterState {
  const next = { ...filters }
  const agent = normalizeAgentFilter(selected)
  if (agent === null) delete next.agentFilter
  else next.agentFilter = agent
  return next
}

// ---------------------------------------------------------------------------
// Time-window filtering.
// ---------------------------------------------------------------------------

/**
 * Visibility rules (task spec):
 * - an active `statusFilter` (UX-01) overrides everything: only sessions
 *   of that status are visible, regardless of window or showDead — the
 *   count badge and the filtered board therefore always agree;
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
  const agentFilter = normalizeAgentFilter(filters.agentFilter)
  if (agentFilter !== null && normalizeAgentFilter(session.agent) !== agentFilter) return false
  const status = normalizeStatus(session.status)
  if (filters.statusFilter !== undefined) return status === filters.statusFilter
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

/** Split a project's cards into visible cards and idle cards for the fold. */
export function partitionIdleCards<T extends SessionCardVM>(
  cards: readonly T[],
): { active: T[]; idle: T[] } {
  const active: T[] = []
  const idle: T[] = []
  for (const card of cards) {
    if (normalizeStatus(card.status) === 'idle') idle.push(card)
    else active.push(card)
  }
  return { active, idle }
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

export interface SessionClusterVM {
  key: string
  project: string
  agent: string
  model: string
  modelProvider: string
  count: number
  sessionIds: string[]
  updatedAtMs: number
}

/**
 * Deterministic pod-local grouping for the analysis view. This deliberately
 * stays metadata-only: semantic analysis is an explicit analysis action and
 * never runs just because the board rendered.
 */
export function clusterSessions(
  sessions: readonly SessionCardVM[],
  windowMs = DAY_MS,
): SessionClusterVM[] {
  const bucketMs = Number.isFinite(windowMs) && windowMs > 0 ? windowMs : DAY_MS
  const groups = new Map<string, SessionClusterVM>()
  for (const session of sessions) {
    const project = session.project.trim() || BOARD_STRINGS.unknownProject
    const agent = session.agent.trim() || 'unknown'
    const model = session.model?.trim() || 'unknown'
    const modelProvider = session.modelProvider?.trim() || 'unknown'
    const bucket = Math.floor(session.updatedAtMs / bucketMs)
    const key = [project, agent, modelProvider, model, bucket].join('\u0000')
    const current = groups.get(key)
    if (current === undefined) {
      groups.set(key, {
        key,
        project,
        agent,
        model,
        modelProvider,
        count: 1,
        sessionIds: [session.sessionId],
        updatedAtMs: session.updatedAtMs,
      })
    } else {
      current.count += 1
      current.sessionIds.push(session.sessionId)
      current.updatedAtMs = Math.max(current.updatedAtMs, session.updatedAtMs)
    }
  }
  const result = [...groups.values()]
  result.sort((a, b) =>
    b.updatedAtMs - a.updatedAtMs
    || a.project.localeCompare(b.project)
    || a.agent.localeCompare(b.agent)
    || a.model.localeCompare(b.model)
    || a.key.localeCompare(b.key))
  return result
}

// ---------------------------------------------------------------------------
// Badge derivation.
// ---------------------------------------------------------------------------

/**
 * status + gap → badge tone/label/attention.
 *
 * Card-level attention carries ONLY the per-session `gap` marker (a known
 * data hole for THIS session). The global stream-health state is a
 * board-wide fact and lives in the top banner alone — repeating it on
 * every card was noise, not signal (UX-18). Unknown raw statuses keep
 * their raw text as the label — the board never invents a state.
 */
export function deriveBadge(rawStatus: string, gap: boolean): StatusBadgeVM {
  const status = normalizeStatus(rawStatus)
  const trimmed = rawStatus.trim()
  const label =
    status === 'unknown'
      ? trimmed === ''
        ? BOARD_STRINGS.status.unknown
        : trimmed
      : BOARD_STRINGS.status[status]
  const attention: StatusBadgeVM['attention'] = gap ? 'gap' : null
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
 * Absolute short timestamp in local time: `MM-DD HH:mm`. Used for ages
 * beyond 24h where relative buckets stop discriminating (UX-13).
 */
export function formatAbsoluteShort(thenMs: number): string {
  const date = new Date(thenMs)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}`
}

/**
 * Card time: <60s (including clock skew into the future) is 刚刚, then
 * whole minutes/hours; from 24h on the absolute short date takes over —
 * a column of "3 天前" carries no information, "08-22 14:03" does
 * (UX-13). Non-finite input renders empty.
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
  return formatAbsoluteShort(thenMs)
}

/** Label for a time-window option: whole days as 天, otherwise 小时. */
export function timeWindowLabel(hours: number): string {
  if (hours >= 24 && hours % 24 === 0) {
    return formatTemplate(BOARD_STRINGS.timeWindow.days, { n: hours / 24 })
  }
  return formatTemplate(BOARD_STRINGS.timeWindow.hours, { n: hours })
}

// ---------------------------------------------------------------------------
// Batch archive (threshold → preview → confirm).
// ---------------------------------------------------------------------------

/** Preset inactivity thresholds offered by the batch-archive dialog. */
export type ArchiveThresholdToken = '30m' | '2h' | '24h' | 'custom'

/** Seconds behind each preset; 'custom' reads the minutes input instead. */
export const ARCHIVE_THRESHOLD_SECONDS: Readonly<
  Record<Exclude<ArchiveThresholdToken, 'custom'>, number>
> = { '30m': 1800, '2h': 7200, '24h': 86400 }

/** Manual default: conservative enough that a lunch break survives it. */
export const DEFAULT_ARCHIVE_THRESHOLD: ArchiveThresholdToken = '2h'

/** Clamp for the custom-minutes input (1 minute … 30 days). */
export const MIN_ARCHIVE_SECONDS = 60
export const MAX_ARCHIVE_SECONDS = 30 * 24 * 3600

/**
 * Resolve the dialog controls into the `idleSeconds` the daemon expects.
 * Returns null when a custom value is empty or out of range, which the
 * dialog renders as a disabled preview button rather than a silent clamp.
 */
export function resolveArchiveSeconds(
  token: ArchiveThresholdToken,
  customMinutes: string,
): number | null {
  if (token !== 'custom') return ARCHIVE_THRESHOLD_SECONDS[token]
  const minutes = Number(customMinutes.trim())
  if (!Number.isFinite(minutes)) return null
  const seconds = Math.round(minutes * 60)
  if (seconds < MIN_ARCHIVE_SECONDS || seconds > MAX_ARCHIVE_SECONDS) return null
  return seconds
}

/** Composite key used to address one card across preview/apply round-trips. */
export function cardKey(card: { agent: string; sessionId: string }): string {
  return `${card.agent}\u0000${card.sessionId}`
}

/**
 * The one agent with a supervised session the host can really end. Every
 * other agent is a transcript on disk plus a process nobody here owns.
 */
export const DISPOSABLE_AGENT = 'dsh'

/** How many of these cards a real dispose could touch. */
export function countDisposable(cards: readonly { agent: string }[]): number {
  return cards.filter((card) => card.agent === DISPOSABLE_AGENT).length
}

/**
 * Whether the batch dialog should offer the dispose opt-in. Both halves
 * matter: a host without the capability cannot dispose anything, and a
 * selection without dsh rows has nothing to dispose — in either case the
 * checkbox would promise an action that does nothing.
 */
export function shouldOfferDispose(
  capable: boolean,
  selected: readonly { agent: string }[],
): boolean {
  return capable && countDisposable(selected) > 0
}

/** Provenance token → localized label; unknown tokens render verbatim. */
export function archiveReasonLabel(reason: string): string {
  if (reason === 'manual') return BOARD_STRINGS.archived.reason.manual
  if (reason === 'batch') return BOARD_STRINGS.archived.reason.batch
  if (reason === 'auto') return BOARD_STRINGS.archived.reason.auto
  return reason
}

/** Newest archive decision first; the board shows the recent ones on top. */
export function sortArchived<T extends ArchivedCardVM>(rows: readonly T[]): T[] {
  return [...rows].sort((a, b) => b.archivedAtMs - a.archivedAtMs)
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
 * The cause behind a FAILED daemon, as the host reports it. A host that never
 * sends one (older plugin, or a failure it could not attribute) leaves this
 * null, and the banner then says only what it can stand behind.
 */
export function daemonFailureText(failure: DaemonFailure | null): string {
  if (failure === null) return BOARD_STRINGS.banner.daemonFailed
  switch (failure.reason) {
    case 'spawn-error':
      return failure.detail === null || failure.detail === ''
        ? BOARD_STRINGS.banner.daemonFailed
        : formatTemplate(BOARD_STRINGS.banner.daemonFailedSpawn, { detail: failure.detail })
    case 'daemon-exit':
      return failure.exitCode === null
        ? BOARD_STRINGS.banner.daemonFailed
        : formatTemplate(BOARD_STRINGS.banner.daemonFailedExit, { code: failure.exitCode })
    case 'ready-timeout':
      return BOARD_STRINGS.banner.daemonFailedTimeout
    default:
      return BOARD_STRINGS.banner.daemonFailed
  }
}

/**
 * Top banner: daemon FAILED (red, last-snapshot notice) outranks version
 * drift, which in turn outranks a degraded stream — a lagging stream
 * delivers the truth late, while a stale daemon delivers a different
 * program's answer promptly, which is the harder thing to notice.
 * 'unknown' stream health alone raises no banner — it is the
 * pre-first-connect startup state and a warning bar on every mount would
 * be noise.
 */
export function deriveBanner(
  daemonState: DaemonStateToken,
  streamHealth: StreamHealthToken,
  drift: DaemonVersionDrift | null = null,
  failure: DaemonFailure | null = null,
): BoardBannerVM | null {
  if (daemonState === 'failed') {
    return { tone: 'danger', text: daemonFailureText(failure) }
  }
  if (drift !== null) {
    // Two matching version numbers would make the drift banner read as a
    // contradiction, so that case names the change instead of the numbers.
    const text = drift.running === drift.installed
      ? formatTemplate(BOARD_STRINGS.banner.daemonStaleCode, { running: drift.running })
      : formatTemplate(BOARD_STRINGS.banner.daemonStale, {
          running: drift.running,
          installed: drift.installed,
        })
    return { tone: 'warn', text }
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
// Display truncation (UX-02 board groups / UX-20 project lanes).
// ---------------------------------------------------------------------------

/** Cards a board group renders before the 「展开全部」 fold (UX-02). */
export const GROUP_CARD_LIMIT = 20

/** A truncated card list plus how many items the fold is hiding. */
export interface DisplaySlice<T> {
  shown: T[]
  hiddenCount: number
}

/**
 * Slice a status-sorted card list down to `limit` for display, unless
 * `expanded`. The cut never lands inside the leading working/waiting run:
 * attention-worthy sessions are the reason the board exists, so the
 * effective limit grows to cover all of them (an all-active group renders
 * fully — honest, and rare). Non-positive limits disable truncation.
 */
export function sliceCardsForDisplay<T extends { status: string }>(
  cards: readonly T[],
  limit: number,
  expanded: boolean,
): DisplaySlice<T> {
  if (expanded || limit <= 0 || cards.length <= limit) {
    return { shown: [...cards], hiddenCount: 0 }
  }
  let activeRun = 0
  while (activeRun < cards.length) {
    const status = normalizeStatus(cards[activeRun]!.status)
    if (status !== 'working' && status !== 'waiting') break
    activeRun += 1
  }
  const effectiveLimit = Math.max(limit, activeRun)
  return {
    shown: cards.slice(0, effectiveLimit),
    hiddenCount: cards.length - Math.min(cards.length, effectiveLimit),
  }
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

/** Count of sessions observed in one normalized status. */
export function countByStatus(
  sessions: ReadonlyArray<{ status: string }>,
  status: SessionStatusToken,
): number {
  let count = 0
  for (const session of sessions) {
    if (normalizeStatus(session.status) === status) count += 1
  }
  return count
}

/** Count of sessions currently observed as working. */
export function countWorking(sessions: ReadonlyArray<{ status: string }>): number {
  return countByStatus(sessions, 'working')
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
  const {
    sessions,
    filters,
    daemonState,
    streamHealth,
    daemonDrift,
    daemonFailure,
    lastReconcileAtMs,
    nowMs,
  } = input
  const visible = filterSessions(sessions, filters, nowMs)
  const derived: DerivedSessionCardVM[] = visible.map((session) => ({
    ...session,
    badge: deriveBadge(session.status, session.gap),
    glyph: agentGlyph(session.agent),
    shortId: abbreviateSessionId(session.sessionId),
    relativeTime: formatRelativeTime(session.updatedAtMs, nowMs),
    hoverTitle: badgeHoverTitle(session.status, lastReconcileAtMs, nowMs),
  }))
  return {
    groups: groupSessions(derived),
    banner: deriveBanner(daemonState, streamHealth, daemonDrift ?? null, daemonFailure ?? null),
    emptyState: deriveEmptyState(daemonState, visible.length, sessions.length),
    daemonBadge: deriveDaemonBadge(daemonState),
    streamLabel: BOARD_STRINGS.stream[streamHealth],
    streamTone: streamHealthTone(streamHealth),
    visibleCount: visible.length,
    totalCount: sessions.length,
    workingCount: countWorking(sessions),
    waitingCount: countByStatus(sessions, 'waiting'),
  }
}
