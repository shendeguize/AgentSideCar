/**
 * AI bypass-analysis engine — design §5 pillar 3 / §4.e.3 (M3, T5.6).
 * Creates a DEDICATED dsh analysis session per request via `ctx.agents.create`,
 * feeds it a bounded summary of the observed session/project, supports
 * incremental follow-up questions, and can be cancelled from the UI at any
 * time. Pure engine: routing/index wiring is the integration layer's job, and
 * the analysis INPUT (summary text assembled from fusion timelines/overviews)
 * arrives as a structured parameter — this module never touches fusion.
 *
 * API facts verified against the installed SDK
 * (`@deepseek-ai/dsh-agent@0.1.1-rc.2` d.ts, authoritative over the design
 * sketch):
 *
 * - `ctx.agents.create(options): Promise<AgentHandle>` (lib/types/index.d.ts:288)
 *   with `CreateAgentOptions` requiring a CALLER-SUPPLIED `sessionId`
 *   (index.d.ts:65-118) — the engine mints one per analysis — plus an optional
 *   creation-only `signal` (index.d.ts:98). `AgentHandle = { agent; dispose():
 *   Promise<void> }` (index.d.ts:155-158); `dispose()` stops the loop and
 *   removes the session, which is exactly the UI "stop" semantics.
 * - There is NO system-prompt field on `CreateAgentOptions` (`agentOptions` is
 *   only provider/model/maxTokens, runtime-types.d.ts:21-28; `setup` composes
 *   cordis scopes and would break pure DI), so the read-only-analyst guidance
 *   rides the FIRST user message instead.
 * - `Agent.followup(message: UserMessage): void` is a SYNCHRONOUS inbox splice
 *   (runtime-types.d.ts:115) — same finding as T4.4. There is no per-message
 *   response promise; the result must be read back from the session.
 * - "Getting the analysis result": `Agent.whenIdle(): Promise<void>` resolves
 *   after whole-agent quiescence (runtime-types.d.ts:87), and a waking
 *   followup flips status to running synchronously (runtime-types.d.ts:161-172),
 *   so `followup → whenIdle → read log` observes the completed turn. The text
 *   is read from `agent.session.deriveMessages()` (dsh-session
 *   index.d.ts:259, the cached surface projection); token accounting rides
 *   `assistant/message` events as `data.usage?: TokenUsage`
 *   (dsh-session types.d.ts:279-285, dsh-llm types.d.ts:123-129) — that feeds
 *   `tokensHint`.
 * - `Agent.cancel(cause: AgentCancelCause): void` aborts the active turn
 *   (runtime-types.d.ts:80); `{ kind: 'user' }` is the honest cause for a
 *   user-facing stop/timeout (dsh-session types.d.ts:118-127).
 *
 * Bounds (§7-B token-cost touchpoint, design risk 12): gated by live
 * `analysis.enabled` (default false), input truncated to `maxInputChars`,
 * first response and every follow-up bounded by `analysisTimeoutMs` (timeout
 * cancels the in-flight turn), and at most `maxActiveSessions` concurrent
 * analysis sessions. No periodic/automatic analysis exists here by design.
 *
 * Error vocabulary (contractual):
 * `analysis_disabled | create_failed | timeout | too_many_active | cancelled`.
 * `create_failed` covers the whole establishment phase of `request()`
 * (create + priming followup + first-turn wait); `cancelled` covers follow-ups
 * against a session that is unknown, already stopped, or died mid-turn.
 *
 * Honesty: results are AI-generated inference; every {@link AnalysisResult}
 * carries {@link ANALYSIS_DISCLAIMER} for the UI to display.
 *
 * Log lines NEVER carry the analyzed content: no `summaryText`, no follow-up
 * question, no model reply — only kind/title/analysisSessionId/outcome/token
 * hints and sizes (S8).
 *
 * Pure DI: no cordis/dsh imports; `ctx.agents.create` is injected through the
 * minimal structural face {@link AnalysisAgentFace} (method-syntax members
 * keep parameter checks bivariant, so the SDK's branded `SessionId` / wider
 * `UserMessage` / union `AgentCancelCause` signatures remain assignable).
 *
 * @module
 */

// ---------------------------------------------------------------------------
// Minimal structural faces over the dsh-agent / dsh-session SDK.
// ---------------------------------------------------------------------------

/** Widened user-message face (same shape family as dsh-inject's, F11). */
export interface AnalysisUserMessageFace {
  readonly id: string
  readonly role: 'user'
  readonly content: ReadonlyArray<{ readonly type: string; readonly text?: string }>
  readonly source: { readonly kind: string; readonly plugin?: string }
}

/** Widened derived-message face over dsh-llm `Message` (message.d.ts:120-129). */
export interface AnalysisDerivedMessageFace {
  readonly role: string
  readonly content: ReadonlyArray<{ readonly type: string; readonly text?: string }>
}

/** Widened session-event envelope face (dsh-session types.d.ts:425-443). */
export interface AnalysisSessionEventFace {
  readonly type: string
  /** `assistant/message` events carry `{ usage?: TokenUsage }` here. */
  readonly data?: unknown
}

/** Read face over the live session log (dsh-session index.d.ts:106-267). */
export interface AnalysisSessionLogFace {
  /** Cached surface projection of the derived LLM history (index.d.ts:259). */
  deriveMessages(): ReadonlyArray<AnalysisDerivedMessageFace>
  /** Immutable append-only event snapshot (index.d.ts:174). */
  readonly events: ReadonlyArray<AnalysisSessionEventFace>
}

/** Cancellation cause face; `{kind:'user'}` ∈ `AgentCancelCause` (dsh-session types.d.ts:118-127). */
export interface AnalysisCancelCauseFace {
  readonly kind: 'user'
}

/** Live-agent face: prompt in, quiescence + log read back (runtime-types.d.ts:60-133). */
export interface AnalysisAgentDriverFace {
  readonly session: AnalysisSessionLogFace
  followup(message: AnalysisUserMessageFace): void
  cancel(cause: AnalysisCancelCauseFace): void
  whenIdle(): Promise<void>
}

/**
 * The dedicated analysis session handle — `AgentHandle` face
 * (index.d.ts:155-158): send messages via `agent.followup`, read responses via
 * `agent.whenIdle` + `agent.session`, stop via `dispose()`.
 */
export interface AnalysisSession {
  readonly agent: AnalysisAgentDriverFace
  dispose(): Promise<void>
}

/** Per-agent options face over SDK `AgentOptions` (runtime-types.d.ts:21-28). */
export interface AnalysisAgentOptionsFace {
  /** Provider route (must have a registered adapter at call time). */
  readonly provider?: string
  /** Model id interpreted by the selected provider adapter. */
  readonly model?: string
  /** Maximum output tokens for each conversation-model request. */
  readonly maxTokens?: number
}

/** `CreateAgentOptions` face (index.d.ts:65-118): engine-minted id + creation abort. */
export interface AnalysisCreateOptions {
  readonly sessionId: string
  readonly signal?: AbortSignal
  /**
   * Provider/model routing for the analysis agent (`CreateAgentOptions.
   * agentOptions`). The engine never sets this — the integration layer's
   * create adapter resolves and attaches it (A-1: an agent created without
   * provider/model fails prompt assembly on the `{{model}}` variable and
   * `buildRequest`, completing with an empty summary and zero tokens).
   */
  readonly agentOptions?: AnalysisAgentOptionsFace
  /**
   * Session creation metadata (`CreateAgentOptions.meta` subset). Also
   * attached by the create adapter, never the engine: the deployment
   * persona's `{{cwd}}` prompt variable reads `session.header.cwd`, which
   * only `meta.cwd` populates (same reason dsh-headless passes
   * `meta: { cwd: process.cwd() }` on its own create call).
   */
  readonly meta?: {
    readonly cwd?: string
    /** Explicit marker for board/fusion filters; analysis is not user work. */
    readonly agentSidecarAnalysis?: true
  }
}

/** Minimal `ctx.agents` face: the one factory entry point this engine uses. */
export interface AnalysisAgentFace {
  /** `AgentRegistry.create` (index.d.ts:288). */
  create(options: AnalysisCreateOptions): Promise<AnalysisSession>
}

// ---------------------------------------------------------------------------
// Engine vocabulary.
// ---------------------------------------------------------------------------

/** Structured analysis input, pre-assembled by the integration layer. */
export interface AnalysisInput {
  kind: 'session' | 'project' | 'cross-agent'
  title: string
  /** Bounded text already summarized from timelines/overviews by the caller. */
  summaryText: string
  meta?: Record<string, unknown>
}

export type AnalysisErrorCode =
  | 'analysis_disabled'
  | 'create_failed'
  | 'timeout'
  | 'too_many_active'
  | 'cancelled'

export type AnalysisOutcome = 'completed' | 'timeout' | 'failed'

/**
 * Which bounded step ran out of time. A bounded enum, never free text: it
 * separates "the session never came up" from "the model never finished",
 * which are different problems with different fixes, and the old single
 * `timeout` code could not tell them apart in the result or the log.
 */
export type AnalysisTimeoutStage = 'create' | 'first_turn' | 'followup_turn'

export interface AnalysisResult {
  outcome: AnalysisOutcome
  /**
   * Present while the analysis session is still alive (completed requests,
   * completed/timed-out follow-ups); absent when nothing remains to cancel
   * (pre-create failures and request timeouts, where the session is disposed).
   */
  analysisSessionId?: string
  /**
   * Assistant text produced by this turn. Present on `completed`, and on
   * `timeout` when the model had already emitted text before the deadline —
   * that text was paid for and is usually the useful part of a slow answer.
   */
  summary?: string
  /** Which step timed out; present exactly when `outcome` is `timeout`. */
  timeoutStage?: AnalysisTimeoutStage
  /** Whether the input text of THIS call was truncated to `maxInputChars`. */
  truncated: boolean
  /** Sum of input+output tokens reported for this turn, when the adapter reported any. */
  tokensHint?: number
  errorCode?: AnalysisErrorCode
  /** Diagnostic error text (never analyzed-session content). */
  detail?: string
  /** Honesty banner for the UI; always {@link ANALYSIS_DISCLAIMER}. */
  disclaimer: string
}

/** Body-free structured log entry (S8): never carries analyzed content. */
export interface AnalysisLogEntry {
  op: 'create' | 'followup' | 'cancel' | 'result'
  analysisSessionId?: string
  kind?: AnalysisInput['kind']
  title?: string
  phase?: 'request' | 'followup'
  outcome?: AnalysisOutcome
  errorCode?: AnalysisErrorCode
  /** Which bounded step ran out of time, on a timeout result. */
  timeoutStage?: AnalysisTimeoutStage
  /** Whether a timed-out turn still yielded usable text. */
  partial?: boolean
  truncated?: boolean
  /** Size of the bounded input actually sent (chars), never its content. */
  inputChars?: number
  tokensHint?: number
  /** cancel(): whether the id named a live analysis session. */
  found?: boolean
  elapsedMs?: number
  detail?: string
}

// ---------------------------------------------------------------------------
// Tunables and fixed strings.
// ---------------------------------------------------------------------------

/**
 * Input bound: chars of summary/question text fed to one turn. Matches the
 * assembly's own cap so the two layers agree on how much reaches a model:
 * an engine bound above the assembly's would never be the one that bit,
 * and a bound below it would re-cut a digest already trimmed to fit.
 */
export const DEFAULT_MAX_INPUT_CHARS = 6000
/**
 * Bound on one model turn: the first response, and each follow-up response.
 * Measured from the moment the turn is handed to the agent, so a slow
 * establishment cannot spend it — sharing one budget with `create` meant a
 * model answering well inside its own bound still timed out.
 */
export const DEFAULT_ANALYSIS_TIMEOUT_MS = 60_000
/** Separate bound on `ctx.agents.create` (session establishment only). */
export const DEFAULT_ANALYSIS_CREATE_TIMEOUT_MS = 20_000
/**
 * Worst-case wall time of one `request`, which any transport in front of the
 * engine must outlive to receive the engine's own verdict instead of racing
 * it (see the client's `ANALYSIS_POST_TIMEOUT_MS`).
 */
export const ANALYSIS_REQUEST_BUDGET_MS =
  DEFAULT_ANALYSIS_CREATE_TIMEOUT_MS + DEFAULT_ANALYSIS_TIMEOUT_MS
/** Concurrent dedicated analysis sessions tracked by one engine. */
export const DEFAULT_MAX_ACTIVE_ANALYSES = 4
/** `source.plugin` attribution and analysis session id prefix. */
export const DEFAULT_PLUGIN_NAME = 'agent-sidecar'
/** Prefix marker used to keep dedicated analysis sessions off the board. */
export const ANALYSIS_SESSION_PREFIX = 'agent-sidecar-analysis-'

/** Title chars kept in prompts and logs (titles are untrusted input too). */
const MAX_TITLE_CHARS = 200

/** Appended to the input text when it was cut at `maxInputChars`. */
export const TRUNCATION_MARKER = '\n…[输入已截断 / input truncated]'

/** Honesty banner attached to every result (design §7-B / risk 12). */
export const ANALYSIS_DISCLAIMER =
  'AI 分析仅供参考,由模型基于有界摘要推断生成,可能不完整或有误 / AI-generated analysis for reference only; inferred from a bounded summary and may be incomplete or wrong.'

/**
 * Read-only-analyst guidance. `CreateAgentOptions` has no system-prompt field
 * (d.ts fact above), so this rides the first user message.
 */
export const ANALYSIS_GUIDANCE = [
  '你是只读分析助手:基于下面提供的 agent 会话摘要给出洞察(状态判断、异常与风险、可能的下一步建议)。',
  '不执行任何操作、不调用任何工具、不修改任何东西;不要假设摘要之外的事实,摘要可能不完整或被截断,不确定处请如实说明。',
  'You are a read-only analysis assistant: provide insights (state assessment, anomalies/risks, possible next steps) based solely on the agent-session summary below.',
  'Take no actions, call no tools, change nothing; do not assume facts beyond the summary — it may be incomplete or truncated, so state uncertainty honestly.',
].join('\n')

// ---------------------------------------------------------------------------
// Internals.
// ---------------------------------------------------------------------------

interface ActiveAnalysis {
  analysisSessionId: string
  kind: AnalysisInput['kind']
  title: string
  /** Undefined while `ctx.agents.create` is still pending (slot reservation). */
  handle?: AnalysisSession
  /** One turn at a time per analysis session. */
  busy: boolean
  /** deriveMessages() length already consumed by earlier turns. */
  messageBaseline: number
  /** session.events length already consumed by earlier turns. */
  eventBaseline: number
  /** Per-session message id counter. */
  messageSeq: number
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function boundText(
  text: string,
  maxChars: number,
): { text: string; truncated: boolean } {
  if (text.length <= maxChars) return { text, truncated: false }
  return { text: text.slice(0, maxChars) + TRUNCATION_MARKER, truncated: true }
}

function boundTitle(title: string): string {
  return title.length <= MAX_TITLE_CHARS ? title : title.slice(0, MAX_TITLE_CHARS) + '…'
}

type TimeoutRace<T> = { timedOut: false; value: T } | { timedOut: true }

/** Race a promise against a bounded timer; the timer is always cleared. */
async function withTimeout<T>(promise: Promise<T>, ms: number): Promise<TimeoutRace<T>> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      promise.then((value) => ({ timedOut: false as const, value })),
      new Promise<{ timedOut: true }>((resolve) => {
        timer = setTimeout(() => resolve({ timedOut: true }), Math.max(1, ms))
      }),
    ])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

/** Join the text blocks of assistant messages appended after `baseline`. */
function extractNewAssistantText(
  messages: ReadonlyArray<AnalysisDerivedMessageFace>,
  baseline: number,
): string {
  const parts: string[] = []
  for (const message of messages.slice(baseline)) {
    if (message.role !== 'assistant') continue
    const text = message.content
      .filter((block) => block.type === 'text' && typeof block.text === 'string')
      .map((block) => block.text as string)
      .join('\n')
    if (text.length > 0) parts.push(text)
  }
  return parts.join('\n\n')
}

/** Sum reported input+output tokens on new `assistant/message` events. */
function extractTokensHint(
  events: ReadonlyArray<AnalysisSessionEventFace>,
  baseline: number,
): number | undefined {
  let total = 0
  let reported = false
  for (const event of events.slice(baseline)) {
    if (event.type !== 'assistant/message') continue
    const usage = (event.data as { usage?: { inputTokens?: unknown; outputTokens?: unknown } } | undefined)
      ?.usage
    if (usage === undefined) continue
    reported = true
    if (typeof usage.inputTokens === 'number') total += usage.inputTokens
    if (typeof usage.outputTokens === 'number') total += usage.outputTokens
  }
  return reported ? total : undefined
}

// ---------------------------------------------------------------------------
// Engine.
// ---------------------------------------------------------------------------

export interface AnalysisEngineDeps {
  /** `ctx.agents.create` face (injected by the integration layer). */
  createAgent: AnalysisAgentFace['create']
  /** Live `analysis.enabled` gate, re-read on every call. */
  allowAnalysis(): boolean
  /** Structured body-free log sink. */
  log(entry: AnalysisLogEntry): void
  now?: () => number
  /** Input bound per turn; default {@link DEFAULT_MAX_INPUT_CHARS}. */
  maxInputChars?: number
  /** Per-turn wait bound; default {@link DEFAULT_ANALYSIS_TIMEOUT_MS}. */
  analysisTimeoutMs?: number
  /** Session establishment bound; default {@link DEFAULT_ANALYSIS_CREATE_TIMEOUT_MS}. */
  createTimeoutMs?: number
  /** Concurrent session cap; default {@link DEFAULT_MAX_ACTIVE_ANALYSES}. */
  maxActiveSessions?: number
  /** Attribution + session id prefix; default {@link DEFAULT_PLUGIN_NAME}. */
  pluginName?: string
}

export class AnalysisEngine {
  private readonly deps: AnalysisEngineDeps
  private readonly now: () => number
  private readonly maxInputChars: number
  private readonly analysisTimeoutMs: number
  private readonly createTimeoutMs: number
  private readonly maxActiveSessions: number
  private readonly pluginName: string
  private readonly active = new Map<string, ActiveAnalysis>()
  private mintCounter = 0

  constructor(deps: AnalysisEngineDeps) {
    this.deps = deps
    this.now = deps.now ?? Date.now
    this.maxInputChars = deps.maxInputChars ?? DEFAULT_MAX_INPUT_CHARS
    this.analysisTimeoutMs = deps.analysisTimeoutMs ?? DEFAULT_ANALYSIS_TIMEOUT_MS
    this.createTimeoutMs = deps.createTimeoutMs ?? DEFAULT_ANALYSIS_CREATE_TIMEOUT_MS
    this.maxActiveSessions = deps.maxActiveSessions ?? DEFAULT_MAX_ACTIVE_ANALYSES
    this.pluginName = deps.pluginName ?? DEFAULT_PLUGIN_NAME
  }

  /** Number of live (or being-created) analysis sessions. */
  get activeCount(): number {
    return this.active.size
  }

  /**
   * Start a dedicated analysis session and return its first insight.
   * Establishment is bounded by `createTimeoutMs` and the priming turn gets
   * its own full `analysisTimeoutMs`; on timeout the turn is cancelled and
   * the session disposed, so a timed-out request leaves nothing running —
   * but any text the model already emitted is returned rather than dropped.
   */
  async request(input: AnalysisInput): Promise<AnalysisResult> {
    const startedAt = this.now()
    const title = boundTitle(input.title)

    if (!this.deps.allowAnalysis()) {
      return this.failResult('request', { kind: input.kind, title }, 'analysis_disabled', {
        truncated: false,
        startedAt,
      })
    }
    if (this.active.size >= this.maxActiveSessions) {
      return this.failResult('request', { kind: input.kind, title }, 'too_many_active', {
        truncated: false,
        startedAt,
        detail: `active analyses at cap (${this.maxActiveSessions})`,
      })
    }

    const bounded = boundText(input.summaryText, this.maxInputChars)
    const analysisSessionId = this.mintSessionId()
    const entry: ActiveAnalysis = {
      analysisSessionId,
      kind: input.kind,
      title,
      busy: true,
      messageBaseline: 0,
      eventBaseline: 0,
      messageSeq: 0,
    }
    // Reserve the slot before awaiting create so concurrent requests cannot
    // overshoot the cap.
    this.active.set(analysisSessionId, entry)

    const controller = new AbortController()
    let createTimedOut = false
    const createPromise = this.deps.createAgent({
      sessionId: analysisSessionId,
      signal: controller.signal,
    })
    // A late settle after timeout must neither leak a running agent nor
    // surface an unhandled rejection.
    void createPromise.then(
      (late) => {
        if (createTimedOut) void late.dispose().catch(() => {})
      },
      () => {},
    )
    let handle: AnalysisSession
    try {
      const created = await withTimeout(createPromise, this.createTimeoutMs)
      if (created.timedOut) {
        createTimedOut = true
        this.active.delete(analysisSessionId)
        controller.abort()
        return this.timeoutResult('request', entry, bounded.truncated, startedAt, undefined, {
          stage: 'create',
        })
      }
      handle = created.value
    } catch (error) {
      this.active.delete(analysisSessionId)
      return this.failResult('request', entry, 'create_failed', {
        truncated: bounded.truncated,
        startedAt,
        detail: describeError(error),
      })
    }
    entry.handle = handle

    this.deps.log({
      op: 'create',
      analysisSessionId,
      kind: input.kind,
      title,
      truncated: bounded.truncated,
      inputChars: bounded.text.length,
    })

    const prompt = this.buildInitialPrompt(input.kind, title, bounded)
    const turn = await this.runTurn(entry, prompt, this.now() + this.analysisTimeoutMs)
    entry.busy = false

    if (turn.status === 'threw') {
      // The session never produced a first insight: fold into create_failed
      // and clean up (nothing valuable to keep).
      this.active.delete(analysisSessionId)
      await this.disposeQuietly(entry)
      return this.failResult('request', entry, 'create_failed', {
        truncated: bounded.truncated,
        startedAt,
        detail: turn.detail,
      })
    }
    if (turn.status === 'timeout') {
      // Bound token burn: cancel the in-flight turn AND dispose the session —
      // a cancelled mid-turn holds no context worth a follow-up. Whatever the
      // model did emit still ships: it was paid for, and on a slow answer it
      // is usually the substance.
      this.cancelQuietly(entry)
      this.active.delete(analysisSessionId)
      await this.disposeQuietly(entry)
      return this.timeoutResult('request', entry, bounded.truncated, startedAt, undefined, {
        stage: 'first_turn',
        partial: turn.partial,
      })
    }

    const result: AnalysisResult = {
      outcome: 'completed',
      analysisSessionId,
      summary: turn.summary,
      truncated: bounded.truncated,
      ...(turn.tokensHint !== undefined ? { tokensHint: turn.tokensHint } : {}),
      disclaimer: ANALYSIS_DISCLAIMER,
    }
    this.logResult('request', entry, result, startedAt)
    return result
  }

  /**
   * Ask an incremental follow-up question on an established analysis session.
   * A timeout cancels the in-flight turn but KEEPS the session (its prior
   * context stays valuable; the UI may retry or cancel).
   */
  async followup(analysisSessionId: string, question: string): Promise<AnalysisResult> {
    const startedAt = this.now()
    const entry = this.active.get(analysisSessionId)

    if (!this.deps.allowAnalysis()) {
      return this.failResult('followup', entry ?? { analysisSessionId }, 'analysis_disabled', {
        truncated: false,
        startedAt,
      })
    }
    if (entry === undefined || entry.handle === undefined) {
      return this.failResult('followup', { analysisSessionId }, 'cancelled', {
        truncated: false,
        startedAt,
        detail: 'unknown or already-cancelled analysis session',
      })
    }
    if (entry.busy) {
      return this.failResult('followup', entry, 'too_many_active', {
        truncated: false,
        startedAt,
        detail: 'a turn is already in flight on this analysis session',
      })
    }

    const bounded = boundText(question, this.maxInputChars)
    this.deps.log({
      op: 'followup',
      analysisSessionId,
      kind: entry.kind,
      title: entry.title,
      truncated: bounded.truncated,
      inputChars: bounded.text.length,
    })

    entry.busy = true
    try {
      const turn = await this.runTurn(entry, bounded.text, startedAt + this.analysisTimeoutMs)
      if (turn.status === 'threw') {
        // The live agent rejected the splice/wait — it is gone underneath us.
        this.active.delete(analysisSessionId)
        await this.disposeQuietly(entry)
        return this.failResult('followup', entry, 'cancelled', {
          truncated: bounded.truncated,
          startedAt,
          detail: turn.detail,
        })
      }
      if (turn.status === 'timeout') {
        this.cancelQuietly(entry)
        return this.timeoutResult(
          'followup',
          entry,
          bounded.truncated,
          startedAt,
          analysisSessionId,
          { stage: 'followup_turn', partial: turn.partial },
        )
      }
      const result: AnalysisResult = {
        outcome: 'completed',
        analysisSessionId,
        summary: turn.summary,
        truncated: bounded.truncated,
        ...(turn.tokensHint !== undefined ? { tokensHint: turn.tokensHint } : {}),
        disclaimer: ANALYSIS_DISCLAIMER,
      }
      this.logResult('followup', entry, result, startedAt)
      return result
    } finally {
      entry.busy = false
    }
  }

  /**
   * Stop and dispose one analysis session (UI stop button). Idempotent: an
   * unknown id resolves as a logged no-op.
   */
  async cancel(analysisSessionId: string): Promise<void> {
    const entry = this.active.get(analysisSessionId)
    if (entry === undefined) {
      this.deps.log({ op: 'cancel', analysisSessionId, found: false })
      return
    }
    this.active.delete(analysisSessionId)
    this.cancelQuietly(entry)
    await this.disposeQuietly(entry)
    this.deps.log({
      op: 'cancel',
      analysisSessionId,
      kind: entry.kind,
      title: entry.title,
      found: true,
    })
  }

  // -------------------------------------------------------------------------
  // Turn driving.
  // -------------------------------------------------------------------------

  private async runTurn(
    entry: ActiveAnalysis,
    text: string,
    deadline: number,
  ): Promise<
    | { status: 'completed'; summary: string; tokensHint?: number }
    | { status: 'timeout'; partial: string }
    | { status: 'threw'; detail: string }
  > {
    const handle = entry.handle!
    const session = handle.agent.session
    entry.messageBaseline = session.deriveMessages().length
    entry.eventBaseline = session.events.length

    const message: AnalysisUserMessageFace = {
      id: `${entry.analysisSessionId}-msg-${++entry.messageSeq}`,
      role: 'user',
      content: [{ type: 'text', text }],
      source: { kind: 'plugin', plugin: this.pluginName },
    }

    try {
      // Synchronous inbox splice; waking delivery flips status synchronously,
      // so the subsequent whenIdle() observes this turn (d.ts facts above).
      handle.agent.followup(message)
    } catch (error) {
      return { status: 'threw', detail: describeError(error) }
    }

    let idle: TimeoutRace<void>
    try {
      idle = await withTimeout(handle.agent.whenIdle(), deadline - this.now())
    } catch (error) {
      return { status: 'threw', detail: describeError(error) }
    }
    if (idle.timedOut) {
      // Read the log before the caller cancels: a model that streamed text
      // and then stalled has already appended assistant messages, and they
      // are the only part of this turn worth anything.
      return {
        status: 'timeout',
        partial: extractNewAssistantText(session.deriveMessages(), entry.messageBaseline),
      }
    }

    const summary = extractNewAssistantText(session.deriveMessages(), entry.messageBaseline)
    const tokensHint = extractTokensHint(session.events, entry.eventBaseline)
    entry.messageBaseline = session.deriveMessages().length
    entry.eventBaseline = session.events.length
    return {
      status: 'completed',
      summary,
      ...(tokensHint !== undefined ? { tokensHint } : {}),
    }
  }

  private buildInitialPrompt(
    kind: AnalysisInput['kind'],
    title: string,
    bounded: { text: string; truncated: boolean },
  ): string {
    return [
      ANALYSIS_GUIDANCE,
      '',
      `[分析对象 / subject] kind=${kind} title=${title}`,
      '',
      `--- 会话摘要开始 / summary begin (有界输入${bounded.truncated ? ',已截断 / truncated' : ''}) ---`,
      bounded.text,
      '--- 会话摘要结束 / summary end ---',
    ].join('\n')
  }

  // -------------------------------------------------------------------------
  // Cleanup and result/log helpers.
  // -------------------------------------------------------------------------

  private cancelQuietly(entry: ActiveAnalysis): void {
    try {
      entry.handle?.agent.cancel({ kind: 'user' })
    } catch (error) {
      this.deps.log({
        op: 'cancel',
        analysisSessionId: entry.analysisSessionId,
        found: true,
        detail: `cancel threw: ${describeError(error)}`,
      })
    }
  }

  private async disposeQuietly(entry: ActiveAnalysis): Promise<void> {
    if (entry.handle === undefined) return
    try {
      await entry.handle.dispose()
    } catch (error) {
      this.deps.log({
        op: 'cancel',
        analysisSessionId: entry.analysisSessionId,
        found: true,
        detail: `dispose threw: ${describeError(error)}`,
      })
    }
  }

  private mintSessionId(): string {
    return `${ANALYSIS_SESSION_PREFIX}${this.now().toString(36)}-${++this.mintCounter}`
  }

  private failResult(
    phase: 'request' | 'followup',
    ident: Partial<Pick<ActiveAnalysis, 'analysisSessionId' | 'kind' | 'title'>>,
    errorCode: AnalysisErrorCode,
    opts: { truncated: boolean; startedAt: number; detail?: string },
  ): AnalysisResult {
    const result: AnalysisResult = {
      outcome: 'failed',
      truncated: opts.truncated,
      errorCode,
      ...(opts.detail !== undefined ? { detail: opts.detail } : {}),
      disclaimer: ANALYSIS_DISCLAIMER,
    }
    this.deps.log({
      op: 'result',
      phase,
      ...(ident.analysisSessionId !== undefined
        ? { analysisSessionId: ident.analysisSessionId }
        : {}),
      ...(ident.kind !== undefined ? { kind: ident.kind } : {}),
      ...(ident.title !== undefined ? { title: ident.title } : {}),
      outcome: 'failed',
      errorCode,
      ...(opts.detail !== undefined ? { detail: opts.detail } : {}),
      elapsedMs: this.now() - opts.startedAt,
    })
    return result
  }

  private timeoutResult(
    phase: 'request' | 'followup',
    entry: ActiveAnalysis,
    truncated: boolean,
    startedAt: number,
    analysisSessionId: string | undefined,
    opts: { stage: AnalysisTimeoutStage; partial?: string },
  ): AnalysisResult {
    const partial = opts.partial ?? ''
    const result: AnalysisResult = {
      outcome: 'timeout',
      ...(analysisSessionId !== undefined ? { analysisSessionId } : {}),
      ...(partial !== '' ? { summary: partial } : {}),
      truncated,
      errorCode: 'timeout',
      timeoutStage: opts.stage,
      disclaimer: ANALYSIS_DISCLAIMER,
    }
    this.deps.log({
      op: 'result',
      phase,
      analysisSessionId: entry.analysisSessionId,
      kind: entry.kind,
      title: entry.title,
      outcome: 'timeout',
      errorCode: 'timeout',
      timeoutStage: opts.stage,
      ...(partial !== '' ? { partial: true } : {}),
      elapsedMs: this.now() - startedAt,
    })
    return result
  }

  private logResult(
    phase: 'request' | 'followup',
    entry: ActiveAnalysis,
    result: AnalysisResult,
    startedAt: number,
  ): void {
    this.deps.log({
      op: 'result',
      phase,
      analysisSessionId: entry.analysisSessionId,
      kind: entry.kind,
      title: entry.title,
      outcome: result.outcome,
      ...(result.tokensHint !== undefined ? { tokensHint: result.tokensHint } : {}),
      truncated: result.truncated,
      elapsedMs: this.now() - startedAt,
    })
  }
}
