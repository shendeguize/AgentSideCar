/**
 * Post-hoc verification of an injection whose delivery is unknown.
 *
 * An `unknown` outcome means the execute call died AFTER the message may
 * already have been dispatched (timeout, network drop, a parse failure on
 * the way back). That is terminal by design: retrying could double-send,
 * so the machine refuses to go back to the editor (S6). What the operator
 * is left with is a question — did it land? — and the answer is observable:
 * a delivered message becomes an event in the target session's transcript.
 *
 * So instead of leaving that lookup as homework, this module does it: pull
 * the target's newest timeline window a bounded number of times and look
 * for the message the plan described. The result only ever REPORTS; nothing
 * here can re-send anything, which keeps the no-retry rule intact while
 * removing the ambiguity in the common case.
 *
 * Matching uses `plan.messagePreview.head` — the prefix the host echoed
 * back at prepare time — not the raw body: the panel does not retain the
 * body past execute, and a prefix is enough to tell "my message is there"
 * from "nothing arrived". A head shared with an earlier identical
 * injection cannot be told apart, which is why a confirmation is reported
 * as observed rather than proven.
 *
 * Pure and injectable: the caller supplies the probe and the sleep, so this
 * is fully testable with fake timers and no transport.
 *
 * @module
 */

import type { SidecarLocaleKey } from '../locales/index.ts'
import type { PanelState } from './logic.ts'

/** What the automatic delivery check is doing right now. */
export type VerifyView =
  | { phase: 'off' }
  | { phase: 'running' }
  | { phase: 'done'; outcome: VerifyOutcome }

/**
 * Clock-skew allowance when deciding which transcript entries are recent
 * enough to be this injection. The daemon's timestamps come from files
 * written by other processes, so a small backward slack avoids missing a
 * message that really is ours; keeping it small avoids crediting an
 * identical message the operator sent moments earlier.
 */
export const VERIFY_SKEW_MS = 30_000

const VERIFY_COPY = {
  confirmed: 'inject.verifyConfirmed',
  absent: 'inject.verifyAbsent',
  unavailable: 'inject.verifyUnavailable',
} as const satisfies Record<VerifyOutcome, SidecarLocaleKey>

/** Copy for the automatic check, or null when there is nothing to say. */
export function verifyCopyKey(view: VerifyView): SidecarLocaleKey | null {
  if (view.phase === 'off') return null
  return view.phase === 'running' ? 'inject.verifying' : VERIFY_COPY[view.outcome]
}

/**
 * Whether the automatic check applies to the current panel state: only a
 * terminal `unknown` outcome, only with a probe bound, and only with a
 * usable needle. Anything else is either already answered (delivered /
 * failed) or unanswerable, and must be left to the operator.
 */
export function shouldVerifyDelivery(state: PanelState, hasProbe: boolean): boolean {
  if (!hasProbe || state.phase !== 'result') return false
  if (state.result.outcome !== 'unknown') return false
  return injectedHead(state).trim() !== ''
}

/** The previewed message head of a terminal result, or `''`. */
export function injectedHead(state: PanelState): string {
  return state.phase === 'result' ? state.plan?.messagePreview.head ?? '' : ''
}

/** One timeline entry, narrowed to what matching needs. */
export interface VerifyEntry {
  kind: string
  text: string
  ts: number
}

/**
 * Verdict of one verification run.
 *
 * - `confirmed`: the message was observed in the target transcript.
 * - `absent`: every attempt answered, and none of them showed it. The
 *   injection probably did not land — but "probably" is the honest word,
 *   since an agent that never echoes user turns would look the same.
 * - `unavailable`: no attempt could read the transcript (daemon down, the
 *   agent has no replayable history). Nothing was learned either way.
 */
export type VerifyOutcome = 'confirmed' | 'absent' | 'unavailable'

/**
 * Reads one newest-window page of the target session, or null when the
 * source could not answer at all (as opposed to answering "empty").
 */
export type VerifyProbe = () => Promise<readonly VerifyEntry[] | null>

/** Attempts per verification run, and the wait between them. */
export const VERIFY_ATTEMPTS = 3
export const VERIFY_DELAY_MS = 1500

/**
 * Kinds that can carry an injected message. The injected text arrives as a
 * user turn; a `prompt`/`input` kind is the same thing under adapters with
 * their own vocabulary. Assistant output is excluded: an agent quoting the
 * prompt back is not evidence that the prompt was delivered by us.
 */
const INJECTED_KINDS = new Set(['user', 'prompt', 'input', 'user_message'])

/** Collapse whitespace so re-wrapped transcript text still matches. */
function normalize(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

/**
 * Whether these entries contain the injected message.
 *
 * Containment either way: the transcript may hold more than the previewed
 * head (the full body), and it may also hold less (an adapter that
 * truncates). A blank or whitespace-only head matches nothing — that is an
 * unusable needle, not a wildcard.
 */
export function matchesInjectedMessage(
  entries: readonly VerifyEntry[],
  head: string,
  sinceMs?: number,
): boolean {
  const needle = normalize(head)
  if (needle === '') return false
  return entries.some((entry) => {
    if (!INJECTED_KINDS.has(entry.kind.trim().toLowerCase())) return false
    // Only events at or after the injection count: an identical message
    // sent an hour ago must not confirm this one.
    if (sinceMs !== undefined && Number.isFinite(entry.ts) && entry.ts < sinceMs) {
      return false
    }
    const text = normalize(entry.text)
    if (text === '') return false
    return text.includes(needle) || needle.includes(text)
  })
}

export interface VerifyOptions {
  probe: VerifyProbe
  /** `plan.messagePreview.head`: the prefix the host echoed at prepare time. */
  head: string
  /** Ignore entries older than this (epoch ms); usually the execute time. */
  sinceMs?: number
  attempts?: number
  delayMs?: number
  /** Injected wait, so tests drive this with fake timers. */
  sleep?: (ms: number) => Promise<void>
  /** Cooperative cancellation: true abandons the run between attempts. */
  cancelled?: () => boolean
}

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => { setTimeout(resolve, ms) })

/**
 * Look for the injected message in the target transcript, a bounded number
 * of times. Returns as soon as it is found; retries exist because a
 * queued injection reaches the transcript slightly after the execute call
 * gives up on us.
 */
export async function verifyInjection(opts: VerifyOptions): Promise<VerifyOutcome> {
  const attempts = Math.max(1, opts.attempts ?? VERIFY_ATTEMPTS)
  const delayMs = Math.max(0, opts.delayMs ?? VERIFY_DELAY_MS)
  const sleep = opts.sleep ?? defaultSleep
  let answered = false

  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (opts.cancelled?.() === true) return 'unavailable'
    if (attempt > 0) {
      await sleep(delayMs)
      if (opts.cancelled?.() === true) return 'unavailable'
    }
    let entries: readonly VerifyEntry[] | null
    try {
      entries = await opts.probe()
    } catch {
      // A probe that throws is a source that could not answer, same as a
      // null: it must never be read as "the message is not there".
      entries = null
    }
    if (entries === null) continue
    answered = true
    if (matchesInjectedMessage(entries, opts.head, opts.sinceMs)) return 'confirmed'
  }
  // Nothing observed. Distinguishing "the transcript says no" from "no
  // transcript answered" is the whole point: only the first is evidence.
  return answered ? 'absent' : 'unavailable'
}
