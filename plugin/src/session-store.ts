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

import type { SessionRow, SidecarEvent, StreamHealth } from './bridge.ts'
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
  last_event: SessionEventSummary | null
  /** True when a dsh seq discontinuity was observed since the last snapshot. */
  gap: boolean
  /** Host-derived verdict only; raw topology/remote markers are never exposed. */
  inject_eligibility: InjectEligibility
}

/** Full board state handed to routes/UI. */
export interface BoardState {
  sessions: SessionView[]
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
  inject_eligibility: InjectEligibility
}

const INVALID_PROPERTY = Symbol('invalid-property')

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
  return {
    agent,
    session_id: sessionId,
    status,
    title,
    project,
    updated_at: updatedAt,
  }
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
  private eventState = new Map<string, EventSideState>()
  private streamHealth: StreamHealth = 'unknown'
  private lastReconcileAt: number | null = null
  private readonly listeners = new Set<() => void>()

  /** Replace the full session set with an authoritative snapshot. */
  applySnapshot(rows: SessionRow[]): void {
    const next = new Map<string, StoredSession>()
    for (const row of rows) {
      const projection = snapshotProjection(row)
      if (projection === null) continue
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
      last_event: state?.lastEvent ?? null,
      gap: state?.gap ?? false,
      inject_eligibility: row.inject_eligibility,
    }
  }

  private notify(): void {
    for (const listener of this.listeners) listener()
  }
}
