/**
 * Session state cache for the host half (design §4.b / ADR-2).
 *
 * Ownership rules:
 * - `status` snapshots are authoritative: `applySnapshot` fully replaces
 *   the session set and clears per-session gap flags (the snapshot IS the
 *   reconciliation).
 * - subscribe events are incremental hints only: they refresh the
 *   last-event summary and feed seq continuity tracking, but never mutate
 *   session status (the daemon queue is lossy, drop-oldest, unsignalled).
 * - dsh seq gap detection: only events whose `extra.seq` is a valid
 *   integer participate in continuity checks; events without a seq are
 *   skipped entirely so they can never produce a false gap. A detected
 *   gap sticks until the next snapshot reconciliation clears it.
 *
 * Pure state container: no I/O, no cordis/dsh imports.
 *
 * @module
 */

import type {
  ArchivedSessionRow,
  ArchivePolicy,
  SessionRow,
  SidecarEvent,
  StreamHealth,
} from './bridge.ts'
import {
  deriveInjectEligibility,
  type InjectEligibility,
} from './inject-eligibility.ts'

/** Compact summary of the most recent event seen for a session. */
export interface SessionEventSummary {
  ts: string
  kind: string
  text: string
}

/** One board row: authoritative snapshot fields plus stream-derived hints. */
export interface SessionView {
  agent: string
  session_id: string
  status: string
  title: string
  project: string
  updated_at: number
  /** Epoch seconds the adapter reported as session birth; absent when unknown. */
  created_at?: number
  /** Absolute transcript path; absent for adapters with no file on disk. */
  transcript?: string
  model?: string
  model_provider?: string
  last_event: SessionEventSummary | null
  /** True when a dsh seq discontinuity was observed since the last snapshot. */
  gap: boolean
  /** Host-derived verdict only; raw topology/remote markers are never exposed. */
  inject_eligibility: InjectEligibility
}

/** One row the daemon is hiding, with the decision that hid it. */
export interface ArchivedSessionView extends SessionView {
  /** Epoch seconds of the archive decision. */
  archived_at: number
  archive_reason: string
}

/** Full board state handed to routes/UI. */
export interface BoardState {
  sessions: SessionView[]
  /** Archived rows, newest decision first; empty on pre-archive daemons. */
  archived: ArchivedSessionView[]
  /** Null until a daemon that advertises the archive policy is reached. */
  archivePolicy: ArchivePolicy | null
  streamHealth: StreamHealth
  lastReconcileAt: number | null
}

/** Bound for the last-event text summary kept per session. */
const EVENT_TEXT_LIMIT = 160

interface EventSideState {
  lastEvent: SessionEventSummary | null
  lastSeq: number | null
  gap: boolean
}

/** Snapshot projection retained after eligibility consumes the full raw row. */
interface StoredSession {
  agent: string
  session_id: string
  status: string
  title: string
  project: string
  updated_at: number
  created_at?: number
  transcript?: string
  model?: string
  model_provider?: string
  inject_eligibility: InjectEligibility
}

const INVALID_PROPERTY = Symbol('invalid-property')
const ANALYSIS_SESSION_PREFIX = 'agent-sidecar-analysis-'

function ownValue(record: object, key: string): unknown | typeof INVALID_PROPERTY {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(record, key)
    return descriptor !== undefined && 'value' in descriptor
      ? descriptor.value
      : INVALID_PROPERTY
  } catch {
    return INVALID_PROPERTY
  }
}

/** Dedicated analysis agents are internal tooling, never board sessions. */
function isAnalysisSession(sessionId: string, extra: unknown): boolean {
  if (sessionId.startsWith(ANALYSIS_SESSION_PREFIX)) return true
  return typeof extra === 'object' && extra !== null
    && (extra as Record<string, unknown>)['agentSidecarAnalysis'] === true
}

/**
 * Copy only accessor-free board fields. Runtime callers are not allowed to
 * smuggle a forged prototype/getter into the long-lived board cache.
 */
function snapshotProjection(row: SessionRow): Omit<StoredSession, 'inject_eligibility'> | null {
  try {
    const prototype = Object.getPrototypeOf(row)
    if (prototype !== Object.prototype && prototype !== null) return null
  } catch {
    return null
  }
  const agent = ownValue(row, 'agent')
  const sessionId = ownValue(row, 'session_id')
  const status = ownValue(row, 'status')
  const title = ownValue(row, 'title')
  const project = ownValue(row, 'project')
  const updatedAt = ownValue(row, 'updated_at')
  if (
    typeof agent !== 'string' ||
    agent === '' ||
    typeof sessionId !== 'string' ||
    sessionId === '' ||
    typeof status !== 'string' ||
    typeof title !== 'string' ||
    typeof project !== 'string' ||
    typeof updatedAt !== 'number' ||
    !Number.isFinite(updatedAt)
  ) {
    return null
  }
  const projected: Omit<StoredSession, 'inject_eligibility'> = {
    agent,
    session_id: sessionId,
    status,
    title,
    project,
    updated_at: updatedAt,
  }
  // The transcript path is header metadata the detail view shows and copies
  // (an operator needs it to grep the raw file), so unlike raw `extra` it is
  // kept in cache — but it leaves the store only through
  // `getSessionDetail`, never in a board frame.
  const transcript = ownValue(row, 'transcript')
  if (typeof transcript === 'string' && transcript !== '') projected.transcript = transcript
  const extra = ownValue(row, 'extra')
  if (typeof extra === 'object' && extra !== null) {
    const model = (extra as Record<string, unknown>)['model']
    const modelProvider = (extra as Record<string, unknown>)['model_provider']
    const createdAt = (extra as Record<string, unknown>)['created_at_epoch']
    if (typeof model === 'string' && model.trim() !== '') projected.model = model
    if (typeof modelProvider === 'string' && modelProvider.trim() !== '') {
      projected.model_provider = modelProvider
    }
    // Adapters normalize every native birth-time shape into one epoch-seconds
    // key (`created_at_extra` in sidecar/adapters/base.py); anything else is
    // a legacy or forged row and is dropped rather than half-trusted.
    if (
      typeof createdAt === 'number' &&
      Number.isFinite(createdAt) &&
      createdAt > 0 &&
      createdAt <= updatedAt
    ) {
      projected.created_at = createdAt
    }
  }
  return projected
}

function sessionKey(agent: string, sessionId: string): string {
  return `${agent}\u0000${sessionId}`
}

function truncate(text: string, limit: number): string {
  return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`
}

/**
 * Extract a usable seq cursor. Mirrors `_sequence` in
 * `sidecar/adapters/dsh.py` (integer only); JSON cannot distinguish
 * `3.0` from `3` on the JS side, so `Number.isInteger` is the closest
 * faithful check.
 */
function extractSeq(extra: Record<string, unknown>): number | null {
  const value = extra['seq']
  return typeof value === 'number' && Number.isInteger(value) ? value : null
}

/** In-memory session cache reconciled by snapshots, hinted by events. */
export class SessionStore {
  private rows = new Map<string, StoredSession>()
  private archivedRows: ArchivedSessionView[] = []
  private archivePolicy: ArchivePolicy | null = null
  private eventState = new Map<string, EventSideState>()
  private streamHealth: StreamHealth = 'unknown'
  private lastReconcileAt: number | null = null
  private readonly listeners = new Set<() => void>()

  /**
   * Replace the archive projection. Archived rows go through the same
   * sanitizing projection as board rows; a row the projection rejects is
   * dropped rather than surfaced with partial fields.
   */
  setArchived(rows: readonly ArchivedSessionRow[], policy: ArchivePolicy | null): void {
    const next: ArchivedSessionView[] = []
    for (const row of rows) {
      const projection = snapshotProjection(row)
      if (projection === null) continue
      next.push({
        ...projection,
        last_event: null,
        gap: false,
        inject_eligibility: deriveInjectEligibility(row),
        archived_at: row.archived_at,
        archive_reason: row.archive_reason,
      })
    }
    next.sort((a, b) => b.archived_at - a.archived_at
      || a.session_id.localeCompare(b.session_id))
    this.archivedRows = next
    this.archivePolicy = policy
    this.notify()
  }

  /** Replace the full session set with an authoritative snapshot. */
  applySnapshot(rows: SessionRow[]): void {
    const next = new Map<string, StoredSession>()
    for (const row of rows) {
      const projection = snapshotProjection(row)
      if (projection === null) continue
      const rawExtra = ownValue(row, 'extra')
      if (isAnalysisSession(
        projection.session_id,
        rawExtra === INVALID_PROPERTY ? null : rawExtra,
      )) continue
      // Derive while the full row is still present, then discard raw
      // `extra`, topology, host provenance, and transcript data from cache.
      const injectEligibility = deriveInjectEligibility(row)
      next.set(sessionKey(projection.agent, projection.session_id), {
        ...projection,
        inject_eligibility: injectEligibility,
      })
    }
    this.rows = next
    const staleKeys: string[] = []
    for (const [key, state] of this.eventState) {
      if (next.has(key)) {
        // The snapshot reconciled this session: the gap flag is served.
        state.gap = false
      } else {
        staleKeys.push(key)
      }
    }
    for (const key of staleKeys) this.eventState.delete(key)
    this.lastReconcileAt = Date.now()
    this.notify()
  }

  /** Fold one stream event in as a hint (summary + seq continuity only). */
  applyEvent(ev: SidecarEvent): void {
    const key = sessionKey(ev.agent, ev.session_id)
    let state = this.eventState.get(key)
    if (state === undefined) {
      state = { lastEvent: null, lastSeq: null, gap: false }
      this.eventState.set(key, state)
    }
    state.lastEvent = {
      ts: ev.ts,
      kind: ev.kind,
      text: truncate(ev.text, EVENT_TEXT_LIMIT),
    }
    const seq = extractSeq(ev.extra)
    if (seq !== null) {
      // Only a forward hole is a gap; a backward jump means the source
      // stream restarted, so the cursor resets without flagging.
      if (state.lastSeq !== null && seq > state.lastSeq + 1) {
        state.gap = true
      }
      state.lastSeq = seq
    }
    this.notify()
  }

  /** Stream health is owned by the Reconciler; the store just exposes it. */
  setStreamHealth(health: StreamHealth): void {
    if (this.streamHealth === health) return
    this.streamHealth = health
    this.notify()
  }

  /** True when any snapshot session is currently `working` (active cadence). */
  hasWorkingSessions(): boolean {
    for (const row of this.rows.values()) {
      if (row.status === 'working') return true
    }
    return false
  }

  /** Return one sanitized live target projection, or null when absent. */
  getSession(agent: string, sessionId: string): SessionView | null {
    const key = sessionKey(agent, sessionId)
    const row = this.rows.get(key)
    if (row === undefined) return null
    return this.toView(key, row)
  }

  /**
   * One session by id, with the detail-only fields attached. The board list
   * deliberately omits the transcript path: every reconcile re-sends the whole
   * board over SSE, and a per-row absolute path is payload the list never
   * renders. The detail view asks for exactly one session, so that is where
   * the path is worth its bytes.
   */
  getSessionDetail(sessionId: string): SessionView | null {
    for (const [key, row] of this.rows) {
      if (row.session_id !== sessionId) continue
      const view = this.toView(key, row)
      return row.transcript !== undefined ? { ...view, transcript: row.transcript } : view
    }
    return null
  }

  getBoardState(): BoardState {
    const sessions: SessionView[] = []
    for (const [key, row] of this.rows) {
      sessions.push(this.toView(key, row))
    }
    sessions.sort(
      (a, b) => b.updated_at - a.updated_at || a.session_id.localeCompare(b.session_id),
    )
    return {
      sessions,
      archived: this.archivedRows.map((row) => ({ ...row })),
      archivePolicy: this.archivePolicy === null ? null : { ...this.archivePolicy },
      streamHealth: this.streamHealth,
      lastReconcileAt: this.lastReconcileAt,
    }
  }

  /** Subscribe to store mutations; returns the unsubscribe function. */
  onChange(cb: () => void): () => void {
    this.listeners.add(cb)
    return () => {
      this.listeners.delete(cb)
    }
  }

  private toView(key: string, row: StoredSession): SessionView {
    const state = this.eventState.get(key)
    return {
      agent: row.agent,
      session_id: row.session_id,
      status: row.status,
      title: row.title,
      project: row.project,
      updated_at: row.updated_at,
      ...(row.created_at !== undefined ? { created_at: row.created_at } : {}),
      ...(row.model !== undefined ? { model: row.model } : {}),
      ...(row.model_provider !== undefined ? { model_provider: row.model_provider } : {}),
      last_event: state?.lastEvent ?? null,
      gap: state?.gap ?? false,
      inject_eligibility: row.inject_eligibility,
    }
  }

  private notify(): void {
    for (const listener of this.listeners) listener()
  }
}
