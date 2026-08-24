/**
 * Pure view-model logic for the cross-agent project correlation view
 * (design §4.e.2 / §5.1 M3, T5.5). No React, no I/O, no data-layer
 * imports — plain values in, plain values out, unit-testable in a bare
 * node environment (same discipline as `logic.ts`).
 *
 * Wire contract (hand-written mirror, per module-ownership rules): the
 * input types below mirror the host's `GET <prefix>/projects` response —
 * `{groups: ProjectGroup[]}` where `ProjectGroup` is fusion.ts
 * `getProjectGroups()` output: `project` (normalized path, '' when
 * unknown), `agents` (distinct, sorted), `sessions` (UnifiedSession[],
 * camelCase, **epoch milliseconds** `lastActivityAt`, most recent first)
 * and the group-level `lastActivityAt`. Only the fields this view renders
 * are mirrored; extra wire fields are structurally ignored.
 *
 * Reuse contract (T2.2, read-only): tone/glyph/relative-time/label tools
 * are imported from `./logic.ts` without touching its exports, so both
 * board views speak the same visual language. Strings NEW to this view
 * live in the module-local {@link PROJECT_VIEW_STRINGS} table (S7 unifies
 * locales later); strings emitted by reused board tools (status labels,
 * relative time, the unknown-project label) intentionally stay with the
 * board table so the two views can never drift apart.
 *
 * @module
 */

import {
  abbreviateSessionId,
  agentGlyph,
  deriveBadge,
  formatRelativeTime,
  formatTemplate,
  normalizeStatus,
  projectDisplayName,
  statusRank,
  type StatusBadgeVM,
} from './logic.ts'

// ---------------------------------------------------------------------------
// Module-local strings (project-view-only vocabulary; Chinese primary).
// ---------------------------------------------------------------------------

export const PROJECT_VIEW_STRINGS = {
  /** View heading. */
  title: '项目关联',
  /** Header count summary. */
  summary: '{projects} 个项目 · {sessions} 个会话',
  /** Cross-agent badge, shown when a project hosts 2+ agent kinds. */
  crossAgent: '{n} 种 agent',
  /** Group header session count. */
  sessionCount: '{n} 个会话',
  /** Group header recency line. */
  lastActive: '最近活跃 {time}',
  /** Marker on sessions live in this dsh process (UnifiedSession.live). */
  liveChip: '实时',
  /** Untitled-session fallback. */
  untitled: '(无标题)',
  /** Lane truncation fold (UX-20; mirrors the board's group fold). */
  showAllSessions: '展开全部 {n} 个会话',
  showLessSessions: '只看前 {n} 个',
  /** Empty state (no groups at all). */
  empty: {
    title: '暂无项目关联',
    hint: '时间窗内没有跨 agent 的项目活动;agent 在某个项目目录下开始工作后会出现在这里。',
  },
  /** Loading placeholder (first fetch, nothing to show yet). */
  loading: '正在加载项目关联…',
  /** Error banner prefix; the raw error detail is appended by the view. */
  errorTitle: '项目关联加载失败',
} as const

export type ProjectViewStrings = typeof PROJECT_VIEW_STRINGS

/** Rows a lane renders before the 「展开全部」 fold (UX-20). */
export const LANE_SESSION_LIMIT = 10

// ---------------------------------------------------------------------------
// Input view models (hand-written wire mirror, see module doc).
// ---------------------------------------------------------------------------

/** One session row of a project group (mirror of `UnifiedSession` subset). */
export interface ProjectSessionVM {
  agent: string
  sessionId: string
  /** Raw observed status (open vocabulary; sidecar-inferred). */
  status: string
  title: string
  /** Unix epoch MILLISECONDS (fusion already normalized the sidecar seconds). */
  lastActivityAt: number
  /** True when the session is live in this dsh process. */
  live?: boolean
  /** Sidecar-observed seq discontinuity flag. */
  gap?: boolean
}

/** One wire project group (mirror of fusion.ts `ProjectGroup`). */
export interface ProjectGroupVM {
  /** Project path; '' for the unknown-project bucket. */
  project: string
  /** Distinct agents as reported by the host (re-derived defensively here). */
  agents: string[]
  sessions: ProjectSessionVM[]
  /** Unix epoch MILLISECONDS of the freshest member session. */
  lastActivityAt: number
}

// ---------------------------------------------------------------------------
// Derived view models (what project-view.tsx renders).
// ---------------------------------------------------------------------------

/** A session row with every render-ready derivation attached. */
export interface DerivedProjectSessionVM {
  agent: string
  sessionId: string
  status: string
  title: string
  lastActivityAt: number
  live: boolean
  gap: boolean
  badge: StatusBadgeVM
  glyph: string
  shortId: string
  relativeTime: string
  /** Title with the untitled fallback already resolved. */
  displayTitle: string
}

/** One per-agent column/section inside a project group. */
export interface AgentLaneVM {
  agent: string
  glyph: string
  sessions: DerivedProjectSessionVM[]
}

/** One agent participation marker of the group header. */
export interface AgentBadgeVM {
  agent: string
  glyph: string
}

/** One fully derived project section. */
export interface DerivedProjectGroupVM {
  /** Normalized project path; '' for the unknown-project bucket. */
  key: string
  /** Display name (path basename; board's 未知项目 label for ''). */
  label: string
  /** Full path for the hover title; '' when unknown. */
  fullPath: string
  /** Distinct participating agents, sorted (derived from sessions). */
  agentBadges: AgentBadgeVM[]
  /** Cross-agent marker text; null when a single agent kind participates. */
  crossAgentLabel: string | null
  sessionCount: number
  sessionCountLabel: string
  /** Unix epoch ms of the freshest member session. */
  lastActivityAt: number
  /** '最近活跃 {relative time}'; empty when the group has no finite time. */
  lastActiveLabel: string
  /** Per-agent lanes, sorted by agent name (same order as agentBadges). */
  lanes: AgentLaneVM[]
}

/** Full render model for the project view. */
export interface ProjectViewModel {
  groups: DerivedProjectGroupVM[]
  /** Non-null exactly when there is no group to render. */
  emptyState: { title: string; hint: string } | null
  projectCount: number
  sessionCount: number
  /** Header summary line ('{projects} 个项目 · {sessions} 个会话'). */
  summaryLabel: string
}

// ---------------------------------------------------------------------------
// Normalization helpers.
// ---------------------------------------------------------------------------

const KEY_SEP = '\u0000'

/**
 * Group-key normalization, aligned with fusion's correlation key: trim,
 * strip trailing slashes (keeping root '/'), whitespace-only → '' (the
 * unknown bucket). The host already normalizes; this re-run makes the
 * view robust to hand-fed or merged inputs.
 */
export function normalizeProjectKey(project: string): string {
  const trimmed = project.trim()
  if (trimmed === '') return ''
  if (trimmed.length > 1 && trimmed.endsWith('/')) {
    const stripped = trimmed.replace(/\/+$/, '')
    return stripped === '' ? '/' : stripped
  }
  return trimmed
}

/**
 * Distinct participating agents of a session list, sorted by name. This
 * is derived from the sessions themselves (not the wire `agents` field)
 * so the badge row can never disagree with the lanes actually rendered —
 * notably after two wire groups merge under one normalized key.
 */
export function deriveAgentBadges(
  sessions: ReadonlyArray<{ agent: string }>,
): AgentBadgeVM[] {
  const names = new Set<string>()
  for (const session of sessions) names.add(session.agent)
  return [...names].sort().map((agent) => ({ agent, glyph: agentGlyph(agent) }))
}

/**
 * Session ordering inside a lane, mirroring the board's card order so
 * both views read the same way: status rank (working first, dead last),
 * then recency, then id as the deterministic tiebreak.
 */
export function compareProjectSessions(a: ProjectSessionVM, b: ProjectSessionVM): number {
  const rankDelta = statusRank(normalizeStatus(a.status)) - statusRank(normalizeStatus(b.status))
  if (rankDelta !== 0) return rankDelta
  if (a.lastActivityAt !== b.lastActivityAt) return b.lastActivityAt - a.lastActivityAt
  return a.sessionId.localeCompare(b.sessionId)
}

function deriveSession(session: ProjectSessionVM, nowMs: number): DerivedProjectSessionVM {
  const live = session.live === true
  const gap = session.gap === true
  return {
    agent: session.agent,
    sessionId: session.sessionId,
    status: session.status,
    title: session.title,
    lastActivityAt: session.lastActivityAt,
    live,
    gap,
    // Badge attention reflects only the per-session gap marker (the badge
    // vocabulary has no global-stream mirror at card level, UX-18).
    badge: deriveBadge(session.status, gap),
    glyph: agentGlyph(session.agent),
    shortId: abbreviateSessionId(session.sessionId),
    relativeTime: formatRelativeTime(session.lastActivityAt, nowMs),
    displayTitle: session.title.trim() === '' ? PROJECT_VIEW_STRINGS.untitled : session.title,
  }
}

/**
 * Split a group's sessions into per-agent lanes. Lanes are sorted by
 * agent name (matching the header badge order); sessions inside a lane
 * follow {@link compareProjectSessions}.
 */
export function buildAgentLanes(
  sessions: readonly ProjectSessionVM[],
  nowMs: number,
): AgentLaneVM[] {
  const byAgent = new Map<string, ProjectSessionVM[]>()
  for (const session of sessions) {
    const lane = byAgent.get(session.agent)
    if (lane === undefined) byAgent.set(session.agent, [session])
    else lane.push(session)
  }
  return [...byAgent.keys()].sort().map((agent) => {
    const members = byAgent.get(agent) ?? []
    members.sort(compareProjectSessions)
    return {
      agent,
      glyph: agentGlyph(agent),
      sessions: members.map((session) => deriveSession(session, nowMs)),
    }
  })
}

// ---------------------------------------------------------------------------
// Full pipeline.
// ---------------------------------------------------------------------------

export interface ProjectViewComputeInput {
  groups: readonly ProjectGroupVM[]
  /** Clock injection (epoch ms) for deterministic derivation. */
  nowMs: number
}

/**
 * normalize/merge → derive → sort; the one call project-view.tsx renders
 * from.
 *
 * Normalization: groups collapsing onto the same normalized key are
 * merged; duplicate sessions (same agent + sessionId) within a merged
 * group keep the freshest copy. Group order is `lastActivityAt`
 * descending (recomputed from member sessions, so a stale wire value
 * cannot misplace a group), with the unknown-project bucket ('') always
 * last — same rule as the board.
 */
export function buildProjectViewModel(input: ProjectViewComputeInput): ProjectViewModel {
  const { groups, nowMs } = input

  interface Bucket {
    sessions: Map<string, ProjectSessionVM>
    wireLastActivityAt: number
  }
  const buckets = new Map<string, Bucket>()
  for (const group of groups) {
    const key = normalizeProjectKey(group.project)
    let bucket = buckets.get(key)
    if (bucket === undefined) {
      bucket = { sessions: new Map(), wireLastActivityAt: Number.NEGATIVE_INFINITY }
      buckets.set(key, bucket)
    }
    if (Number.isFinite(group.lastActivityAt)) {
      bucket.wireLastActivityAt = Math.max(bucket.wireLastActivityAt, group.lastActivityAt)
    }
    for (const session of group.sessions) {
      const id = `${session.agent}${KEY_SEP}${session.sessionId}`
      const existing = bucket.sessions.get(id)
      if (existing === undefined || session.lastActivityAt > existing.lastActivityAt) {
        bucket.sessions.set(id, session)
      }
    }
  }

  const derived: DerivedProjectGroupVM[] = []
  let sessionCount = 0
  for (const [key, bucket] of buckets) {
    const sessions = [...bucket.sessions.values()]
    sessionCount += sessions.length
    const memberMax = sessions.reduce(
      (max, s) => (Number.isFinite(s.lastActivityAt) ? Math.max(max, s.lastActivityAt) : max),
      Number.NEGATIVE_INFINITY,
    )
    const lastActivityAt = Math.max(memberMax, bucket.wireLastActivityAt)
    const agentBadges = deriveAgentBadges(sessions)
    derived.push({
      key,
      label: projectDisplayName(key),
      fullPath: key,
      agentBadges,
      crossAgentLabel:
        agentBadges.length > 1
          ? formatTemplate(PROJECT_VIEW_STRINGS.crossAgent, { n: agentBadges.length })
          : null,
      sessionCount: sessions.length,
      sessionCountLabel: formatTemplate(PROJECT_VIEW_STRINGS.sessionCount, {
        n: sessions.length,
      }),
      lastActivityAt: Number.isFinite(lastActivityAt) ? lastActivityAt : 0,
      lastActiveLabel: Number.isFinite(lastActivityAt)
        ? formatTemplate(PROJECT_VIEW_STRINGS.lastActive, {
            time: formatRelativeTime(lastActivityAt, nowMs),
          })
        : '',
      lanes: buildAgentLanes(sessions, nowMs),
    })
  }

  derived.sort((a, b) => {
    if (a.key === '') return b.key === '' ? 0 : 1
    if (b.key === '') return -1
    return b.lastActivityAt - a.lastActivityAt || a.key.localeCompare(b.key)
  })

  return {
    groups: derived,
    emptyState: derived.length === 0 ? { ...PROJECT_VIEW_STRINGS.empty } : null,
    projectCount: derived.length,
    sessionCount,
    summaryLabel: formatTemplate(PROJECT_VIEW_STRINGS.summary, {
      projects: derived.length,
      sessions: sessionCount,
    }),
  }
}
