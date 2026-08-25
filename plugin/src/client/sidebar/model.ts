/** Pure view-model derivation for the optional compact sidebar. */

import type { SidecarViewState } from '../controller.ts'
import {
  countWorking,
  deriveWidgetConnection,
  normalizeStatus,
  type SessionCardVM,
  type WidgetConnection,
  widgetTitle,
} from '../board/logic.ts'

/** Compact view caps the session list at this many rows. */
export const MAX_RECENT_SESSIONS = 5

/** Count of sessions currently observed as waiting. */
export function countWaiting(sessions: ReadonlyArray<{ status: string }>): number {
  let count = 0
  for (const session of sessions) {
    if (normalizeStatus(session.status) === 'waiting') count += 1
  }
  return count
}

/**
 * Return non-dead sessions by descending update time, capped for the compact
 * view. Ties break by session id for stability.
 */
export function recentActiveSessions<T extends SessionCardVM>(
  sessions: readonly T[],
  limit: number = MAX_RECENT_SESSIONS,
): T[] {
  return sessions
    .filter((session) => normalizeStatus(session.status) !== 'dead')
    .sort((a, b) =>
      b.updatedAtMs !== a.updatedAtMs
        ? b.updatedAtMs - a.updatedAtMs
        : a.sessionId.localeCompare(b.sessionId))
    .slice(0, limit)
}

/** Everything the mini tab renders. */
export interface SidebarMiniVM {
  connection: WidgetConnection
  /** Hover text for the header dot (connection + working count). */
  connectionTitle: string
  workingCount: number
  waitingCount: number
  recent: SessionCardVM[]
  hasSnapshot: boolean
}

/** Fold the shared view state into the mini tab's view model. */
export function deriveMiniVM(state: SidecarViewState): SidebarMiniVM {
  const connection = deriveWidgetConnection(state.daemonState, state.streamHealth)
  const workingCount = countWorking(state.sessions)
  return {
    connection,
    connectionTitle: widgetTitle(connection, workingCount),
    workingCount,
    waitingCount: countWaiting(state.sessions),
    recent: recentActiveSessions(state.sessions),
    hasSnapshot: state.hasSnapshot,
  }
}
