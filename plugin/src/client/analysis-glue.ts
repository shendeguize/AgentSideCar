/**
 * AI bypass-analysis data glue (T5.10b, design §4.e.3 / §5.1): a
 * framework-free store per analysis conversation, driving the controlled
 * AnalysisPanel over the `POST action` `analysis.*` trio (host contract:
 * routes.ts handleAnalysis* over analysis.ts AnalysisEngine).
 *
 * Wire semantics this store encodes:
 * - a settled engine result comes back with the HTTP status derived from
 *   its `errorCode` (timeout→504, too_many_active→429, create_failed→502,
 *   cancelled→200); api.ts postAction folds non-200 bodies into ApiError
 *   with `reason` = that code — so failures arrive here as ApiError and
 *   the cancelled-terminal arrives as a 200 body;
 * - the write gate answers 403 `analysis_disabled` (fail-closed), an
 *   agents-less composition answers 501 `analysis_unavailable`, and a
 *   pre-analysis host answers 400 `unknown_action` (mapped to the same
 *   honest "unavailable" terminal);
 * - a request timeout DISPOSES the engine session (terminal), a followup
 *   timeout KEEPS it (non-terminal notice; analysis.ts contract).
 *
 * Token honesty: this store dials analysis turns only on explicit user
 * intent (start / followup / stop) — never automatically. The one
 * automatic call is dispose()'s fire-and-forget cancel, which spends no
 * tokens and only releases the engine session slot (F2).
 *
 * @module
 */

import {
  isApiError,
  postAction,
  type AnalysisTargetKind,
  type RequestOptions,
} from './api.ts'

// ---------------------------------------------------------------------------
// Wire mirror (host: analysis.ts AnalysisResult; hand-written, client
// program cannot import host modules).
// ---------------------------------------------------------------------------

/** Terminal fact about one analysis turn (host: analysis.ts). */
export type AnalysisOutcomeWire = 'completed' | 'failed' | 'cancelled' | 'timeout'

/** Body of a settled `analysis.request` / `analysis.followup` action. */
export interface AnalysisResultWire {
  outcome: AnalysisOutcomeWire
  analysisSessionId?: string
  summary?: string
  truncated: boolean
  tokensHint?: number
  errorCode?: string
  /** Which bounded step ran out of time (host: AnalysisTimeoutStage). */
  timeoutStage?: string
  detail?: string
  disclaimer: string
}

/** Analysis target selector (mirrors routes.ts AnalysisTargetRequest). */
export interface AnalysisTarget {
  targetKind: AnalysisTargetKind
  targetId?: string
  question?: string
}

// ---------------------------------------------------------------------------
// Envelope builders (exported pure for tests).
// ---------------------------------------------------------------------------

export function analysisRequestEnvelope(target: AnalysisTarget): {
  type: 'analysis.request'
  targetKind: AnalysisTargetKind
  targetId?: string
  question?: string
} {
  return {
    type: 'analysis.request',
    targetKind: target.targetKind,
    ...(target.targetId !== undefined ? { targetId: target.targetId } : {}),
    ...(target.question !== undefined && target.question.trim() !== ''
      ? { question: target.question }
      : {}),
  }
}

export function analysisFollowupEnvelope(analysisSessionId: string, question: string): {
  type: 'analysis.followup'
  analysisSessionId: string
  question: string
} {
  return { type: 'analysis.followup', analysisSessionId, question }
}

export function analysisCancelEnvelope(analysisSessionId: string): {
  type: 'analysis.cancel'
  analysisSessionId: string
} {
  return { type: 'analysis.cancel', analysisSessionId }
}

// ---------------------------------------------------------------------------
// State shapes.
// ---------------------------------------------------------------------------

/**
 * Conversation phases. `failed` is terminal for the conversation (restart
 * mints a new engine session); `stopped` is the user-cancelled terminal.
 */
export type AnalysisPhase =
  | 'idle'
  | 'requesting'
  | 'ready'
  | 'answering'
  | 'stopped'
  | 'failed'

/** One settled turn: the initial summary (question null) or a follow-up. */
export interface AnalysisExchange {
  question: string | null
  summary: string
  truncated: boolean
  tokensHint: number | null
}

/** A UI-safe conversation message; pending assistant text is an honest
 * segmented-update fallback when the host exposes no token stream. */
export interface AnalysisMessage {
  role: 'user' | 'assistant'
  content: string
  pending?: boolean
  truncated?: boolean
}

export interface AnalysisGlueState {
  phase: AnalysisPhase
  analysisSessionId: string | null
  exchanges: AnalysisExchange[]
  messages: AnalysisMessage[]
  /** Engine disclaimer verbatim; null before the first settle. */
  disclaimer: string | null
  /** Terminal failure code (phase 'failed'), else null. */
  errorCode: string | null
  /**
   * Which step ran out of time on a timeout, else null. "The session never
   * came up" and "the model never finished" need different words and
   * different fixes; one `timeout` code could say neither.
   */
  timeoutStage: string | null
  /** Non-terminal notice code (kept session; retryable), else null. */
  noticeCode: string | null
  /** Monotonic progress segment for the in-flight fallback indicator. */
  progressStep: number
}

const INITIAL_STATE: AnalysisGlueState = {
  phase: 'idle',
  analysisSessionId: null,
  exchanges: [],
  messages: [],
  disclaimer: null,
  errorCode: null,
  timeoutStage: null,
  noticeCode: null,
  progressStep: 0,
}

/**
 * The action deadline. It must outlive the engine's own worst case — create
 * (~20s) plus one turn (~60s), i.e. `ANALYSIS_REQUEST_BUDGET_MS` in
 * analysis.ts — or the transport aborts first and the client loses both the
 * engine's verdict and any text the engine salvaged from a slow turn. The
 * relationship is pinned by a test, since a client program cannot import the
 * host constant to derive it.
 */
export const ANALYSIS_POST_TIMEOUT_MS = 95_000

/** ApiError reason codes that keep a live followup session usable. */
const RETRYABLE_FOLLOWUP_CODES = new Set([
  'timeout', // engine turn timeout: session kept by contract
  'request_timeout',
  'network_error',
])

function failureCode(err: unknown): string {
  if (isApiError(err)) {
    // A pre-analysis host rejects the envelope as unknown_action — same
    // honest terminal as an assembled-but-agents-less host.
    return err.reason === 'unknown_action' ? 'analysis_unavailable' : err.reason
  }
  return 'network_error'
}

// ---------------------------------------------------------------------------
// The store.
// ---------------------------------------------------------------------------

export interface AnalysisStoreOptions {
  postActionFn?: typeof postAction
  timeoutMs?: number
}

export class AnalysisStore {
  private state: AnalysisGlueState = INITIAL_STATE
  private readonly listeners = new Set<() => void>()
  private readonly postActionFn: typeof postAction
  private readonly timeoutMs: number
  private disposed = false
  private progressTimer: ReturnType<typeof setInterval> | null = null

  constructor(options: AnalysisStoreOptions = {}) {
    this.postActionFn = options.postActionFn ?? postAction
    this.timeoutMs = options.timeoutMs ?? ANALYSIS_POST_TIMEOUT_MS
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => { this.listeners.delete(fn) }
  }

  getState = (): AnalysisGlueState => this.state

  private setState(patch: Partial<AnalysisGlueState>): void {
    if (this.disposed) return
    this.state = { ...this.state, ...patch }
    for (const fn of [...this.listeners]) fn()
  }

  private post(body: Parameters<typeof postAction>[0]): Promise<unknown> {
    const opts: RequestOptions = { timeoutMs: this.timeoutMs }
    return this.postActionFn(body, opts)
  }

  private beginProgress(): void {
    this.progressTimer = setInterval(() => {
      if (this.disposed) return
      this.setState({ progressStep: this.state.progressStep + 1 })
    }, 800)
  }

  private endProgress(): void {
    if (this.progressTimer !== null) {
      clearInterval(this.progressTimer)
      this.progressTimer = null
    }
    if (!this.disposed) this.setState({ progressStep: 0 })
  }

  private withoutPendingMessages(): AnalysisMessage[] {
    return this.state.messages.filter((message) => message.pending !== true)
  }

  private settledMessages(summary: string, truncated: boolean): AnalysisMessage[] {
    return [
      ...this.state.messages.filter((message) => message.pending !== true),
      { role: 'assistant', content: summary, ...(truncated ? { truncated: true } : {}) },
    ]
  }

  /** Start one analysis (allowed from idle and from the terminal phases). */
  async start(target: AnalysisTarget): Promise<void> {
    const { phase } = this.state
    if (this.disposed || phase === 'requesting' || phase === 'ready' || phase === 'answering') {
      return
    }
    const question = target.question?.trim() ?? ''
    this.setState({
      ...INITIAL_STATE,
      phase: 'requesting',
      messages: [
        ...(question === '' ? [] : [{ role: 'user' as const, content: question }]),
        { role: 'assistant', content: '', pending: true },
      ],
    })
    this.beginProgress()
    try {
      const result = (await this.post(analysisRequestEnvelope(target))) as AnalysisResultWire
      if (this.disposed) {
        // The view unmounted while the request was in flight: the engine
        // session it just created has no owner left — release it so it
        // cannot strand one of the engine's bounded session slots (F2).
        const id = result?.analysisSessionId
        if (result?.outcome === 'completed' && typeof id === 'string' && id !== '') {
          this.fireCancel(id)
        }
        return
      }
      if (this.state.phase !== 'requesting') {
        // A user can stop while creation/priming is in flight. The request
        // transport cannot be aborted safely across all host versions, so
        // release any late-created engine session exactly once.
        const id = result?.analysisSessionId
        if (
          result?.outcome === 'completed' &&
          typeof id === 'string' &&
          id !== ''
        ) {
          this.fireCancel(id)
        }
        return
      }
      this.adoptResult(null, result)
    } catch (err) {
      if (this.disposed || this.state.phase !== 'requesting') return
      // Request-path failures are all terminal: either the engine session
      // was never created, or (engine timeout) it was already disposed.
      this.setState({
        phase: 'failed',
        errorCode: failureCode(err),
        timeoutStage: null,
        messages: this.withoutPendingMessages(),
      })
    } finally {
      this.endProgress()
    }
  }

  /** Ask a follow-up in the live analysis session. */
  async followup(question: string): Promise<void> {
    const id = this.state.analysisSessionId
    const q = question.trim()
    if (this.disposed || this.state.phase !== 'ready' || id === null || q === '') return
    this.setState({
      phase: 'answering',
      noticeCode: null,
      messages: [
        ...this.state.messages.filter((message) => message.pending !== true),
        { role: 'user', content: q },
        { role: 'assistant', content: '', pending: true },
      ],
    })
    this.beginProgress()
    // Widened re-reads: setState/stop() move the phase between awaits, and
    // TS's property narrowing from the entry guard would misjudge that.
    const phaseNow = (): AnalysisPhase => this.state.phase
    try {
      const result = (await this.post(analysisFollowupEnvelope(id, q))) as AnalysisResultWire
      if (this.disposed || phaseNow() !== 'answering') return
      this.adoptResult(q, result)
    } catch (err) {
      if (this.disposed || phaseNow() !== 'answering') return
      const code = failureCode(err)
      if (RETRYABLE_FOLLOWUP_CODES.has(code)) {
        // The engine keeps the session on a turn timeout; transport-level
        // failures are unknowable, so stay usable and let the user retry.
        this.setState({
          phase: 'ready',
          noticeCode: code,
          messages: this.withoutPendingMessages(),
        })
      } else {
        this.setState({
          phase: 'failed',
          errorCode: code,
          messages: this.withoutPendingMessages(),
        })
      }
    } finally {
      this.endProgress()
    }
  }

  /** Release the analysis session (idempotent on the engine side). */
  async stop(): Promise<void> {
    const id = this.state.analysisSessionId
    const { phase } = this.state
    if (this.disposed) return
    if (phase === 'requesting') {
      this.setState({
        phase: 'stopped',
        noticeCode: null,
        messages: this.withoutPendingMessages(),
      })
      this.endProgress()
      return
    }
    if (id === null || (phase !== 'ready' && phase !== 'answering')) return
    try {
      await this.post(analysisCancelEnvelope(id))
      if (this.disposed) return
      // An in-flight followup now settles against a stopped phase and is
      // dropped by the phase recheck above.
      this.setState({
        phase: 'stopped',
        noticeCode: null,
        messages: this.withoutPendingMessages(),
      })
    } catch {
      if (this.disposed) return
      this.setState({ noticeCode: 'cancel_failed' })
    }
  }

  /** Fold one settled 200 result into the conversation. */
  private adoptResult(question: string | null, result: AnalysisResultWire): void {
    if (result.outcome === 'timeout') {
      // The engine answers 200 for a timeout that salvaged text. Keep that
      // text, then land where the engine's session lifecycle points: a kept
      // session (follow-up) stays answerable with a notice, a disposed one
      // (request) is terminal.
      const summary = result.summary ?? ''
      const kept = typeof result.analysisSessionId === 'string' && result.analysisSessionId !== ''
      this.setState({
        phase: kept ? 'ready' : 'failed',
        ...(kept ? { analysisSessionId: result.analysisSessionId as string } : {}),
        ...(kept ? { noticeCode: 'timeout' } : { errorCode: 'timeout' }),
        timeoutStage: result.timeoutStage ?? null,
        disclaimer: result.disclaimer,
        messages:
          summary === '' ? this.withoutPendingMessages() : this.settledMessages(summary, false),
        progressStep: 0,
      })
      return
    }
    if (result.outcome === 'completed') {
      this.setState({
        phase: 'ready',
        analysisSessionId: result.analysisSessionId ?? this.state.analysisSessionId,
        exchanges: [
          ...this.state.exchanges,
          {
            question,
            summary: result.summary ?? '',
            truncated: result.truncated,
            tokensHint: result.tokensHint ?? null,
          },
        ],
        messages: this.settledMessages(result.summary ?? '', result.truncated),
        disclaimer: result.disclaimer,
        errorCode: null,
        noticeCode: null,
        progressStep: 0,
      })
      return
    }
    // A 200 non-completed result is the engine's `cancelled` verdict (all
    // other failure codes ride non-200 statuses); treat unknown shapes as
    // terminal too — never invent a usable session.
    this.setState({
      phase: result.errorCode === 'cancelled' || result.outcome === 'cancelled'
        ? 'stopped'
        : 'failed',
      errorCode: result.errorCode ?? result.outcome,
      disclaimer: result.disclaimer,
      messages: this.withoutPendingMessages(),
      progressStep: 0,
    })
  }

  /**
   * Late settlements become no-ops; subscribers are dropped. Idempotent.
   * A live engine session is released with a fire-and-forget cancel so
   * unmount / session-switch remounts cannot strand `maxActiveSessions`
   * slots until plugin unload (F2). Cancel is idempotent on the engine
   * side; a transport failure is silently ignored (nothing to surface —
   * the store is gone).
   */
  dispose(): void {
    if (this.disposed) return
    const { analysisSessionId, phase } = this.state
    this.disposed = true
    this.endProgress()
    this.listeners.clear()
    if (analysisSessionId !== null && (phase === 'ready' || phase === 'answering')) {
      this.fireCancel(analysisSessionId)
    }
  }

  /** Fire-and-forget analysis.cancel (failures are deliberately silent). */
  private fireCancel(analysisSessionId: string): void {
    void this.post(analysisCancelEnvelope(analysisSessionId)).catch(() => {})
  }
}
