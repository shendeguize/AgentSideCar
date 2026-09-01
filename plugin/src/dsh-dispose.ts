/**
 * dsh in-process session dispose — the one destructive action this plugin
 * offers, kept deliberately narrow.
 *
 * Archiving (`sidecar/archive.py`) hides a session from this board and
 * changes nothing else; it is the right answer for almost every idle
 * session. Dispose is the other kind of answer: it ends a live dsh session
 * for real, the same operation the host serves over its `session.dispose`
 * RPC (`Remote_DSH_Center/scripts/agent-matrix.mjs` uses that route for
 * exactly this purpose). Because the plugin runs inside the host process,
 * it calls the service directly rather than looping back through HTTP.
 *
 * Scope guards, all intentional:
 * - dsh sessions only. No other agent has a supervised session object to
 *   end, and killing a process is explicitly out of scope.
 * - Never invented. The `dispose` member is optional on the face: a host
 *   whose sessions service does not expose it reports `unsupported`, and
 *   the UI hides the control rather than offering an action that will fail.
 * - Content-free outcomes. Host error text can carry paths, prompts, and
 *   ids, so it never leaves this module (S8) — callers get a fixed
 *   vocabulary and the log gets the code only.
 *
 * Pure DI: no cordis/dsh imports, structural faces only, matching
 * dsh-inject.ts.
 *
 * @module
 */

/**
 * The optional dispose member of `ctx.sessions`. Structural and optional on
 * purpose: this is the one capability the plugin cannot fake, so its
 * absence must be a first-class state rather than a runtime surprise.
 */
export interface SessionsDisposeFace {
  dispose?(sessionId: string): void | Promise<void>
  get?(sessionId: string): unknown
}

/** Fixed, content-free result vocabulary of one dispose attempt. */
export type DisposeOutcome = 'disposed' | 'not_found' | 'unsupported' | 'timeout' | 'failed'

export interface DisposeResult {
  outcome: DisposeOutcome
  sessionId: string
}

export type DshDisposeLog = (
  level: 'debug' | 'info' | 'warn' | 'error',
  msg: string,
  meta?: Record<string, unknown>,
) => void

/** The api the route layer drives. */
export interface DshDisposeApi {
  /** Whether a bound sessions service currently exposes dispose. */
  available(): boolean
  dispose(sessionId: string): Promise<DisposeResult>
}

/** Bound on one dispose call; the host drains the session loop first. */
export const DEFAULT_DISPOSE_TIMEOUT_MS = 30_000

export interface DshDisposeOptions {
  /**
   * Re-resolved on every call, never cached: the sessions service can
   * mount, swap, or unmount between two clicks, and a captured stale
   * reference would dispose against a departed generation.
   */
  resolve: () => SessionsDisposeFace | null | undefined
  timeoutMs?: number
  log?: DshDisposeLog
}

function disposeMember(
  service: SessionsDisposeFace | null | undefined,
): ((sessionId: string) => void | Promise<void>) | null {
  if (service === null || service === undefined) return null
  const member = service.dispose
  return typeof member === 'function' ? member.bind(service) : null
}

/**
 * Whether the host still knows this session. Only consulted to tell
 * `not_found` from `failed` — a host without `get` skips the check and lets
 * the dispose call itself be the answer.
 */
function knownSession(
  service: SessionsDisposeFace,
  sessionId: string,
): boolean | null {
  if (typeof service.get !== 'function') return null
  try {
    return service.get(sessionId) !== undefined && service.get(sessionId) !== null
  } catch {
    return null
  }
}

export function createDshDisposer(opts: DshDisposeOptions): DshDisposeApi {
  const timeoutMs = opts.timeoutMs ?? DEFAULT_DISPOSE_TIMEOUT_MS
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError('dispose timeoutMs must be a positive number')
  }
  const log: DshDisposeLog = opts.log ?? (() => {})

  const resolveService = (): SessionsDisposeFace | null => {
    try {
      return opts.resolve() ?? null
    } catch {
      return null
    }
  }

  return {
    available: () => disposeMember(resolveService()) !== null,

    dispose: async (sessionId: string): Promise<DisposeResult> => {
      if (typeof sessionId !== 'string' || sessionId === '') {
        throw new RangeError('sessionId must be a nonempty string')
      }
      const service = resolveService()
      const call = disposeMember(service)
      if (service === null || call === null) {
        return { outcome: 'unsupported', sessionId }
      }
      if (knownSession(service, sessionId) === false) {
        // Already gone is the state the operator wanted; saying so beats a
        // failure they would waste a retry on.
        return { outcome: 'not_found', sessionId }
      }

      let timer: ReturnType<typeof setTimeout> | undefined
      try {
        const timeout = new Promise<'timeout'>((resolve) => {
          timer = setTimeout(() => { resolve('timeout') }, timeoutMs)
        })
        const settled = await Promise.race([
          Promise.resolve(call(sessionId)).then(() => 'disposed' as const),
          timeout,
        ])
        if (settled === 'timeout') {
          log('warn', 'dsh dispose timed out', { timeoutMs })
          return { outcome: 'timeout', sessionId }
        }
        log('info', 'dsh session disposed')
        return { outcome: 'disposed', sessionId }
      } catch (error) {
        // The host message may name paths or prompts; only its class is
        // safe to record, and even that stays out of the response.
        log('warn', 'dsh dispose failed', {
          error: error instanceof Error ? error.name : 'unknown',
        })
        return { outcome: 'failed', sessionId }
      } finally {
        if (timer !== undefined) clearTimeout(timer)
      }
    },
  }
}
