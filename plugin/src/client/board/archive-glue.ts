/**
 * Binds the browser archive api to the board's presentation-level
 * {@link BoardArchiveApi}: wire rows become board card view models (epoch
 * seconds → milliseconds, snake_case → camelCase) and `ApiError` reasons
 * become the short tokens the dialog interpolates into its failure line.
 *
 * Kept out of Board.tsx on purpose — the board component stays free of
 * api/sse imports, exactly like the rest of the presentation layer.
 *
 * @module
 */

import {
  archiveApply,
  archivePreview,
  archiveUnarchive,
  dshDispose,
  isApiError,
  type RequestOptions,
} from '../api.ts'
import { mapSessions } from '../controller.ts'
import type {
  ArchiveApplyOutcome,
  ArchiveTargetVM,
  BoardArchiveApi,
  BoardArchivePreview,
} from './ArchivePanel.tsx'

/**
 * Statuses a batch archive may touch. Working/waiting sessions are never
 * candidates regardless of how long the daemon last saw an event: a long
 * silent tool call is not an abandoned session.
 */
export const ARCHIVE_STATUSES: readonly string[] = ['idle', 'dead']

/** The only agent with a supervised session the host can actually end. */
const DISPOSABLE_AGENT = 'dsh'

/** Rethrow with the api reason as the message, so the dialog can show it. */
function rethrowReason(err: unknown): never {
  if (isApiError(err)) throw new Error(err.reason)
  throw err
}

/**
 * End each dsh session in turn, counting outcomes instead of failing the
 * batch. Sequential on purpose: dispose ends a live session and the host
 * serializes session-service work anyway, so a burst buys nothing and
 * makes the failure attribution harder to read.
 *
 * A session already gone (`not_found`) counts as disposed — it is the state
 * the operator asked for. Everything else counts as a failure and stays
 * archived, which is the honest resting place for "hidden but maybe alive".
 */
async function disposeAll(
  targets: readonly ArchiveTargetVM[],
  opts: RequestOptions,
): Promise<{ disposed: number; disposeFailed: number }> {
  let disposed = 0
  let disposeFailed = 0
  for (const target of targets) {
    if (target.agent !== DISPOSABLE_AGENT) continue
    try {
      const result = await dshDispose(target.sessionId, opts)
      if (result.outcome === 'disposed' || result.outcome === 'not_found') disposed += 1
      else disposeFailed += 1
    } catch {
      // A refused gate or dead transport is one failed session, not a
      // failed batch: the archive half already succeeded.
      disposeFailed += 1
    }
  }
  return { disposed, disposeFailed }
}

/** Bind the default (fetch-backed) archive round-trips. */
export function createArchiveApi(opts: RequestOptions = {}): BoardArchiveApi {
  return {
    preview: async (idleSeconds: number): Promise<BoardArchivePreview> => {
      try {
        const result = await archivePreview(idleSeconds, ARCHIVE_STATUSES, opts)
        return {
          token: result.token,
          idleSeconds: result.idle_seconds,
          candidates: mapSessions(result.candidates),
        }
      } catch (err) {
        return rethrowReason(err)
      }
    },
    apply: async (
      targets: readonly ArchiveTargetVM[],
      token: string,
      options: { dispose: boolean } = { dispose: false },
    ): Promise<ArchiveApplyOutcome> => {
      let archived: number
      try {
        // Archive first: the preview token is single-use and short-lived,
        // so the reversible half is spent while it is still valid. If a
        // dispose then fails, the session is merely hidden — the reverse
        // order could end a session the board never managed to hide.
        const result = await archiveApply(targets, token, opts)
        archived = result.count
      } catch (err) {
        return rethrowReason(err)
      }
      if (!options.dispose) return { archived, disposed: 0, disposeFailed: 0 }
      return { archived, ...(await disposeAll(targets, opts)) }
    },
    unarchive: async (targets: readonly ArchiveTargetVM[] | 'all'): Promise<number> => {
      try {
        const result = await archiveUnarchive(targets, opts)
        return result.count
      } catch (err) {
        return rethrowReason(err)
      }
    },
  }
}
