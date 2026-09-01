/**
 * Inject integration glue (S5 wiring, T4.9): everything that stands between
 * the presentational {@link InjectPanel} (T4.5, src/client/inject/) and the
 * rest of the client half, kept pure/injectable so it unit-tests under
 * plain node (same posture as controller.ts / settings-glue.ts).
 *
 * - {@link prepareEnvelope} / {@link executeEnvelope}: build the M2 action
 *   wire bodies of the host dispatcher (src/routes.ts `handleAction`:
 *   `{type:'inject.prepare'|'inject.execute', ...}` with the phase fields
 *   at the top level). The body types are api.ts's `ActionEnvelope` union
 *   members — the data layer owns the wire mirror, so `postAction` is
 *   typed end-to-end and this module casts nothing on the way out.
 * - {@link createInjectActions}: the panel's `onPrepare`/`onExecute` props.
 *   Execute posts with {@link EXECUTE_TIMEOUT_MS} (path two's server-side
 *   budget outlives the 15s data-layer default; prepare keeps the default,
 *   it has no delivery side effect).
 *   Per the panel contract, transport failures resolve AS VALUES: the data
 *   layer's normalized ApiError satisfies the panel's structural
 *   {@link ApiErrorLike} and is returned, never thrown (anything that is
 *   not an ApiError is re-thrown and lands in the panel's defensive catch:
 *   retryable notice for prepare, terminal unknown for execute — S6).
 *   A delivered execute fires the optional `onDelivered` hook so the owner
 *   can pull one fresh snapshot; failed/unknown never do.
 * - {@link createVerifyProbe}: the read-only timeline probe behind the
 *   panel's automatic check of an unknown delivery (inject/verify.ts).
 * - {@link findInjectTarget}: board card selection → panel target (the
 *   design §5.1 view-3 target summary comes from the selected SessionView
 *   as mapped into the controller's card VMs). A session that has left the
 *   snapshot resolves to null — the panel then shows its no-target hint
 *   instead of injecting into a stale target.
 *
 * @module
 */

import { isApiError, postAction } from './api.ts'
import type { ExecuteActionBody, PrepareActionBody, RequestOptions } from './api.ts'
import type { SessionCardVM } from './board/logic.ts'
import { fetchTimelinePage } from './detail/transport.ts'
import type { InjectPanelTarget } from './inject/InjectPanel.tsx'
import type {
  ApiErrorLike,
  InjectResultView,
  PanelExecuteRequest,
  PanelPrepareRequest,
  PrepareSuccess,
} from './inject/logic.ts'

// ---------------------------------------------------------------------------
// Wire bodies (canonical mirror lives in api.ts; re-exported for consumers).
// ---------------------------------------------------------------------------

export type { ExecuteActionBody, PrepareActionBody } from './api.ts'

/**
 * Execute-path HTTP deadline. Path two's server-side budget is the 30s CLI
 * timeout + 5s hard-kill buffer (host: send-cli.ts `DEFAULT_SEND_TIMEOUT_MS`
 * + `HARD_TIMEOUT_BUFFER_MS` = 35s worst case); a client deadline below
 * that fabricates a terminal 'unknown' out of every slow-but-honest
 * delivery receipt (M2 review F-3). 45s = server worst case + 10s margin;
 * the mirror relation is pinned by test against the host constants.
 */
export const EXECUTE_TIMEOUT_MS = 45_000

/** Panel prepare request → the host prepare wire body. */
export function prepareEnvelope(req: PanelPrepareRequest): PrepareActionBody {
  return {
    type: 'inject.prepare',
    target: { agent: req.target.agent, sessionId: req.target.sessionId },
    mode: req.mode,
    message: req.message,
  }
}

/** Panel execute request → the host execute wire body. */
export function executeEnvelope(req: PanelExecuteRequest): ExecuteActionBody {
  return {
    type: 'inject.execute',
    requestId: req.requestId,
    confirmToken: req.confirmToken,
    message: req.message,
  }
}

// ---------------------------------------------------------------------------
// Action callbacks.
// ---------------------------------------------------------------------------

/** Transport seam: POST one action body, resolve the parsed JSON reply. */
export type PostActionFn = (
  body: PrepareActionBody | ExecuteActionBody,
  opts?: RequestOptions,
) => Promise<unknown>

/** Default transport: api.ts postAction (typed directly; the wire bodies
 * are ActionEnvelope union members, so no cast stands between the panel
 * and the dispatcher). */
const defaultPost: PostActionFn = (body, opts) => postAction(body, opts)

/** The two integration callbacks the InjectPanel consumes. */
export interface InjectActions {
  onPrepare(req: PanelPrepareRequest): Promise<PrepareSuccess | ApiErrorLike>
  onExecute(req: PanelExecuteRequest): Promise<InjectResultView | ApiErrorLike>
}

export interface InjectActionDeps {
  /** Transport override (tests); defaults to api.ts postAction. */
  post?: PostActionFn
  /** Fired once per delivered execute (e.g. a controller snapshot refresh). */
  onDelivered?: () => void
}

/**
 * Build the panel's `onPrepare`/`onExecute` callbacks over the action
 * transport. Error posture per the panel contract: ApiError resolves as a
 * value (the panel classifies http-kind as a server vocabulary verdict and
 * everything else as a transport failure); non-ApiError throws propagate.
 * @param deps - transport and delivery-hook seams.
 * @returns the callbacks, ready to spread onto the panel props.
 */
export function createInjectActions(deps: InjectActionDeps = {}): InjectActions {
  const post = deps.post ?? defaultPost
  return {
    async onPrepare(req) {
      try {
        return (await post(prepareEnvelope(req))) as PrepareSuccess
      } catch (err) {
        if (isApiError(err)) return err
        throw err
      }
    },
    async onExecute(req) {
      let result: InjectResultView
      try {
        // Longer deadline than prepare: the send-cli path legitimately runs
        // up to 35s server-side before an honest receipt arrives (F-3).
        result = (await post(executeEnvelope(req), {
          timeoutMs: EXECUTE_TIMEOUT_MS,
        })) as InjectResultView
      } catch (err) {
        if (isApiError(err)) return err
        throw err
      }
      // Only a positive delivery refreshes; 'unknown' stays terminal and
      // untouched (S6), 'failed' changes nothing worth re-pulling.
      if (result.outcome === 'delivered') deps.onDelivered?.()
      return result
    },
  }
}

// ---------------------------------------------------------------------------
// Delivery verification probe (§G).
// ---------------------------------------------------------------------------

/** Timeline window pulled per verification attempt. */
export const VERIFY_PAGE_LIMIT = 40

/** Transport seam: read the newest timeline window of one session. */
export type FetchTimelinePageFn = (
  sessionId: string,
  opts: RequestOptions & { limit?: number },
) => Promise<{ entries: readonly { kind: string; text: string; ts: number }[] }>

/**
 * Probe the target's newest timeline window, for the panel's automatic
 * check of an unknown delivery. Read-only by construction — nothing here
 * can re-send — so it stays clear of the no-retry rule (S6).
 *
 * Transport failures propagate: {@link verifyInjection} reads a rejection
 * as "the source could not answer", which must not be confused with an
 * answered-but-empty page.
 *
 * @param sessionId - the injection target.
 * @param deps - transport override (tests); defaults to the detail transport.
 */
export function createVerifyProbe(
  sessionId: string,
  deps: { fetchPage?: FetchTimelinePageFn } = {},
): () => Promise<readonly { kind: string; text: string; ts: number }[]> {
  const fetchPage = deps.fetchPage ?? defaultFetchPage
  return async () => {
    const page = await fetchPage(sessionId, { limit: VERIFY_PAGE_LIMIT })
    return page.entries
  }
}

const defaultFetchPage: FetchTimelinePageFn = (sessionId, opts) =>
  fetchTimelinePage(sessionId, opts)

// ---------------------------------------------------------------------------
// Target mapping.
// ---------------------------------------------------------------------------

/**
 * Resolve the selected board card into the panel's injection target.
 * @param sessions - current card VMs from the controller view state.
 * @param sessionId - the selected session, or null when nothing is selected.
 * @returns the target, or null when unselected or gone from the snapshot.
 */
export function findInjectTarget(
  sessions: readonly SessionCardVM[],
  sessionId: string | null,
): InjectPanelTarget | null {
  if (sessionId === null) return null
  const card = sessions.find((session) => session.sessionId === sessionId)
  if (card === undefined) return null
  const title = card.title.trim()
  return {
    agent: card.agent,
    sessionId: card.sessionId,
    ...(title !== '' ? { title } : {}),
  }
}
