/**
 * InjectGateway — the single entry point of the injection write path
 * (design §4.d dual-path injection, §4.f.5 / §5.3 two-phase confirm,
 * §8 threat model: server-issued one-time confirmToken).
 *
 * Unifies the three planes both injection paths must share (ADR-4):
 *
 * - **Confirmation**: two-phase `prepare` → `execute`. `prepare` re-verifies
 *   the target live and issues a crypto-random one-time confirmToken
 *   (≥128 bit, 60s TTL) bound to requestId + target + mode + message sha256.
 *   `execute` refuses missing / expired / reused tokens, and refuses a
 *   message whose hash differs from the prepare-time binding (anti-swap).
 *   Any execute attempt against a live token voids it, success or not
 *   (consume-on-attempt).
 * - **Idempotency**: the first execute result per requestId is cached
 *   (5 min TTL) and replayed on repeats. `outcome: 'unknown'` is terminal:
 *   a repeated execute returns the cached unknown and NEVER re-fires the
 *   executor (S6 — no retry through the gateway).
 * - **Logging**: exactly one entry per prepare/execute carrying ts /
 *   requestId / target / mode / phase / result / errorCode / message byte
 *   size and sha256 prefix — never the message body or head plaintext
 *   (the head preview only travels in the prepare response for the UI).
 *
 * The token gate defends against browser-mediated attackers only; it does
 * not claim to stop a local process that can drive both phases itself
 * (ADR-8 trust posture — same as guard.ts).
 *
 * Pure DI: no cordis/dsh imports. Path executors (dsh in-process,
 * sidecar send CLI) are injected; this module owns the contract, not the
 * transport.
 *
 * @module
 */

import { createHash, randomBytes, randomUUID, timingSafeEqual } from 'node:crypto'

// ---------------------------------------------------------------------------
// Public types (kept local to this module by design).
// ---------------------------------------------------------------------------

/** Injection target: one observed session of one agent. */
export interface InjectTarget {
  agent: string
  sessionId: string
}

/** Injection mode, aligned with dsh `session.prompt` semantics. */
export type InjectMode = 'queue' | 'steer'

/** Terminal outcome vocabulary shared by both executor paths. */
export type InjectOutcome = 'delivered' | 'failed' | 'unknown'

/** Gateway-issued error vocabulary (executors may add their own codes). */
export type InjectErrorCode =
  | 'inject_disabled'
  | 'invalid_message'
  | 'target_not_found'
  | 'target_dead'
  | 'too_many_pending'
  | 'token_missing'
  | 'token_expired'
  | 'token_reused'
  | 'token_mismatch'
  | 'unsupported_agent'
  | 'executor_error'

/** Result envelope returned by executors and by the gateway itself. */
export interface InjectResult {
  outcome: InjectOutcome
  /** Gateway code ({@link InjectErrorCode}) or an executor-native code. */
  errorCode?: string
  detail?: string
  /** True when this is a cached first result replayed for a repeat execute. */
  replayed?: boolean
}

/**
 * Live target snapshot produced by the injected `verifyTarget` re-check
 * (backed by store/bridge outside this module). Status is an observed,
 * possibly lagging value (G11).
 */
export interface TargetStatus {
  agent: string
  sessionId: string
  status: string
  title?: string
  project?: string
}

/** Confirmation plan shown to the user; the message body is never retained. */
export interface InjectPlan {
  target: InjectTarget
  mode: InjectMode
  /** Live status snapshot captured by `prepare`'s re-verification. */
  targetStatus: TargetStatus
  /** Byte size + head preview for the confirm dialog (head is UI-only). */
  messagePreview: { bytes: number; head: string }
}

/** What the gateway hands an executor once every gate has passed. */
export interface InjectExecutionRequest {
  target: InjectTarget
  mode: InjectMode
  message: string
  requestId: string
}

/** One injection path (dsh in-process API, or the sidecar send CLI). */
export interface InjectExecutor {
  kind: 'dsh' | 'send-cli'
  execute(req: InjectExecutionRequest): Promise<InjectResult>
}

/**
 * One audit entry. Deliberately body-free: only byte size and a sha256
 * prefix identify the message (S8; the head preview never lands here).
 */
export interface InjectLogEntry {
  /** Epoch ms from the injected clock. */
  ts: number
  phase: 'prepare' | 'execute'
  /** Null when a prepare was rejected before a requestId was issued. */
  requestId: string | null
  /** Null when an execute referenced a requestId the gateway never issued. */
  target: InjectTarget | null
  mode: InjectMode | null
  /** prepare: token issued; execute: outcome === 'delivered'. */
  ok: boolean
  outcome?: InjectOutcome
  errorCode?: string
  replayed?: boolean
  /** UTF-8 byte size of the submitted message. */
  messageBytes: number
  /** First 12 hex chars of the message sha256 — never the body itself. */
  messageSha12: string
}

/** `prepare` input. */
export interface PrepareRequest {
  target: InjectTarget
  mode: InjectMode
  message: string
}

/** `prepare` output: an issued confirmation, or a vocabulary rejection. */
export type PrepareResult =
  | {
      ok: true
      requestId: string
      confirmToken: string
      plan: InjectPlan
      /** Epoch ms after which the confirmToken is dead. */
      expiresAt: number
    }
  | { ok: false; errorCode: InjectErrorCode; detail?: string }

/** `execute` input: the confirmation issued by `prepare` plus the message. */
export interface ExecuteRequest {
  requestId: string
  confirmToken: string
  message: string
}

/** Dependency seam; everything effectful is injected. */
export interface InjectGatewayDeps {
  executors: { dsh: InjectExecutor; sendCli: InjectExecutor }
  /** Live target re-check (store/bridge outside); null = target unknown. */
  verifyTarget(target: InjectTarget): Promise<TargetStatus | null>
  /** Reads the `inject.enabled` gate at call time (live setting). */
  allowWrite(): boolean
  /** Sink for every audit entry (also kept in the internal ring). */
  log(entry: InjectLogEntry): void
  /** Clock override for tests; defaults to `Date.now`. */
  now?(): number
  /** requestId source override for tests; defaults to `randomUUID`. */
  randomId?(): string
}

// ---------------------------------------------------------------------------
// Tunables (exported where tests / routes need the exact boundary).
// ---------------------------------------------------------------------------

/** Message byte cap, aligned with the sidecar send 16 KiB limit (§4.d). */
export const MAX_MESSAGE_BYTES = 16 * 1024
/** confirmToken lifetime (§4.f.5). */
export const TOKEN_TTL_MS = 60_000
/** In-flight (issued, unconsumed, unexpired) token cap; beyond it new prepares are refused. */
export const MAX_PENDING_TOKENS = 32
/** Idempotency window: how long a first execute result is replayable. */
export const RESULT_CACHE_TTL_MS = 5 * 60_000

/** 16 random bytes = 128 bits, the spec floor for the confirmToken. */
const TOKEN_BYTES = 16
/** Head preview cap (chars) for the confirm dialog; UI-only, never logged. */
const HEAD_PREVIEW_CHARS = 120
/** Bound of the internal audit ring served by {@link InjectGateway.getRecentLog}. */
const LOG_RING_LIMIT = 256
const DEFAULT_LOG_QUERY_LIMIT = 50
/** Sha256 hex prefix length recorded in logs. */
const SHA_LOG_CHARS = 12

/** External agents reachable through the sidecar `send` CLI path (§4.d). */
const SEND_CLI_AGENTS: ReadonlySet<string> = new Set(['claude', 'codex', 'cursor-cli'])

// ---------------------------------------------------------------------------
// Internals.
// ---------------------------------------------------------------------------

interface MessageDigest {
  bytes: number
  sha256: string
  sha12: string
}

function digestMessage(message: string): MessageDigest {
  const sha256 = createHash('sha256').update(message, 'utf8').digest('hex')
  return {
    bytes: Buffer.byteLength(message, 'utf8'),
    sha256,
    sha12: sha256.slice(0, SHA_LOG_CHARS),
  }
}

/** Returns a rejection detail, or null when the message is acceptable. */
function validateMessage(message: string, bytes: number): string | null {
  if (bytes === 0) return 'message is empty'
  if (message.includes('\u0000')) return 'message contains a NUL byte'
  if (bytes > MAX_MESSAGE_BYTES) {
    return `message is ${bytes} bytes; limit is ${MAX_MESSAGE_BYTES}`
  }
  return null
}

/** Constant-time token comparison (length leak is inherent and harmless). */
function tokenEquals(expected: string, provided: string): boolean {
  const a = Buffer.from(expected, 'utf8')
  const b = Buffer.from(provided, 'utf8')
  return a.length === b.length && timingSafeEqual(a, b)
}

/** One issued confirmation, alive from prepare until pruned. */
interface PendingToken {
  token: string
  target: InjectTarget
  mode: InjectMode
  messageSha256: string
  expiresAt: number
  /** Set on the first execute attempt and never cleared (one shot). */
  consumed: boolean
}

interface CachedResult {
  result: InjectResult
  expiresAt: number
}

// ---------------------------------------------------------------------------
// Gateway.
// ---------------------------------------------------------------------------

/** The confirmation / idempotency / logging hub for both injection paths. */
export class InjectGateway {
  private readonly deps: InjectGatewayDeps
  private readonly now: () => number
  private readonly randomId: () => string
  private readonly pending = new Map<string, PendingToken>()
  private readonly results = new Map<string, CachedResult>()
  private readonly logRing: InjectLogEntry[] = []

  constructor(deps: InjectGatewayDeps) {
    this.deps = deps
    this.now = deps.now ?? Date.now
    this.randomId = deps.randomId ?? randomUUID
  }

  /**
   * Phase one: gate, validate, re-verify, then issue a one-time
   * confirmation bound to this exact target + mode + message.
   *
   * Pipeline (order per spec): allowWrite gate → message pre-validation
   * (≤16 KiB by bytes, non-empty, no NUL) → live target re-check →
   * injectable-agent whitelist → capacity check → token issuance.
   */
  async prepare(req: PrepareRequest): Promise<PrepareResult> {
    this.prune(this.now())
    const digest = digestMessage(req.message)

    if (!this.deps.allowWrite()) {
      return this.rejectPrepare(req, digest, 'inject_disabled')
    }

    const invalid = validateMessage(req.message, digest.bytes)
    if (invalid !== null) {
      return this.rejectPrepare(req, digest, 'invalid_message', invalid)
    }

    const target: InjectTarget = { agent: req.target.agent, sessionId: req.target.sessionId }
    const status = await this.deps.verifyTarget(target)
    if (status === null) {
      return this.rejectPrepare(req, digest, 'target_not_found')
    }
    if (status.status === 'dead') {
      return this.rejectPrepare(req, digest, 'target_dead')
    }

    // Agents with no injection path (copilot / kimi / cursor-ide / …) are
    // refused HERE, before a token is issued: a prepare that can only ever
    // fail at execute would waste a confirm dialog and in-flight capacity
    // on a caller that gets its unsupported_agent verdict one phase late
    // (M2 review F-6). The execute-side check stays as defense in depth.
    if (this.executorFor(target.agent) === null) {
      return this.rejectPrepare(req, digest, 'unsupported_agent')
    }

    // Re-read the clock after the async re-check so capacity and TTL are
    // decided at actual issuance time.
    const issuedAt = this.now()
    if (this.inFlightCount(issuedAt) >= MAX_PENDING_TOKENS) {
      return this.rejectPrepare(req, digest, 'too_many_pending')
    }

    const requestId = this.randomId()
    const confirmToken = randomBytes(TOKEN_BYTES).toString('hex')
    const expiresAt = issuedAt + TOKEN_TTL_MS
    this.pending.set(requestId, {
      token: confirmToken,
      target,
      mode: req.mode,
      messageSha256: digest.sha256,
      expiresAt,
      consumed: false,
    })

    this.record({
      ts: issuedAt,
      phase: 'prepare',
      requestId,
      target: { ...target },
      mode: req.mode,
      ok: true,
      messageBytes: digest.bytes,
      messageSha12: digest.sha12,
    })

    return {
      ok: true,
      requestId,
      confirmToken,
      plan: {
        target: { ...target },
        mode: req.mode,
        targetStatus: { ...status },
        messagePreview: {
          bytes: digest.bytes,
          head: req.message.slice(0, HEAD_PREVIEW_CHARS),
        },
      },
      expiresAt,
    }
  }

  /**
   * Phase two: validate the confirmation, dispatch to the path executor,
   * and cache the first result per requestId.
   *
   * Rejection order: cached replay (idempotency wins) → token missing →
   * token reused → token expired → token/message binding mismatch →
   * unsupported agent. Every attempt against a live token consumes it,
   * whatever happens afterwards.
   */
  async execute(req: ExecuteRequest): Promise<InjectResult> {
    const now = this.now()
    this.prune(now)
    const digest = digestMessage(req.message)
    const record = this.pending.get(req.requestId) ?? null

    const cached = this.results.get(req.requestId)
    if (cached !== undefined) {
      // A replay must still present the original binding (anti-swap holds
      // on the idempotent path too). The binding facts live on the pending
      // record — and the prune() constants do NOT make "cache alive but
      // pending pruned" impossible: the cache expiry stamp is taken AFTER
      // the executor awaited, so an executor that runs past the token's
      // nominal expiry (send-cli worst case ~35s) stretches the cache past
      // the pending prune time (issuedAt + TOKEN_TTL_MS +
      // RESULT_CACHE_TTL_MS). In that window an unverifiable replay must
      // fail closed rather than hand a cached result to a caller that only
      // knows the requestId (M2 review F-5).
      if (record === null) {
        return this.rejectExecute(req.requestId, null, digest, 'token_missing')
      }
      if (
        !tokenEquals(record.token, req.confirmToken) ||
        record.messageSha256 !== digest.sha256
      ) {
        return this.rejectExecute(req.requestId, record, digest, 'token_mismatch')
      }
      const replay: InjectResult = { ...cached.result, replayed: true }
      this.logExecuteResult(req.requestId, record, digest, replay)
      return replay
    }

    if (!req.confirmToken) {
      // A malformed request (no token at all) is not an "attempt": the
      // issued token, if any, stays live.
      return this.rejectExecute(req.requestId, record, digest, 'token_missing')
    }
    if (record === null) {
      return this.rejectExecute(req.requestId, null, digest, 'token_missing')
    }
    if (record.consumed) {
      return this.rejectExecute(req.requestId, record, digest, 'token_reused')
    }
    if (record.expiresAt <= now) {
      this.pending.delete(req.requestId)
      return this.rejectExecute(req.requestId, record, digest, 'token_expired')
    }

    // Consume-on-attempt: one execute attempt per issued token, success or
    // not. A later retry needs a fresh prepare (fresh user confirmation).
    record.consumed = true

    if (!tokenEquals(record.token, req.confirmToken)) {
      return this.rejectExecute(req.requestId, record, digest, 'token_mismatch')
    }
    if (record.messageSha256 !== digest.sha256) {
      return this.rejectExecute(req.requestId, record, digest, 'token_mismatch')
    }

    const executor = this.executorFor(record.target.agent)
    if (executor === null) {
      return this.rejectExecute(req.requestId, record, digest, 'unsupported_agent')
    }

    let result: InjectResult
    try {
      result = await executor.execute({
        target: { ...record.target },
        mode: record.mode,
        message: req.message,
        requestId: req.requestId,
      })
    } catch (err) {
      result = {
        outcome: 'failed',
        errorCode: 'executor_error',
        detail: err instanceof Error ? err.message : String(err),
      }
    }

    // Cache every terminal result — delivered, failed, and unknown alike.
    // 'unknown' is thereby terminal: replays serve this cache and the
    // consumed token blocks any second dispatch forever (S6).
    this.results.set(req.requestId, {
      result: { ...result },
      expiresAt: this.now() + RESULT_CACHE_TTL_MS,
    })
    this.logExecuteResult(req.requestId, record, digest, result)
    return result
  }

  /** Read-only audit view (newest first), for the M3 detail page. */
  getRecentLog(limit: number = DEFAULT_LOG_QUERY_LIMIT): InjectLogEntry[] {
    const bounded = Math.min(Math.max(Math.floor(limit), 0), this.logRing.length)
    return this.logRing.slice(this.logRing.length - bounded).reverse()
  }

  // -------------------------------------------------------------------------

  private executorFor(agent: string): InjectExecutor | null {
    if (agent === 'dsh') return this.deps.executors.dsh
    if (SEND_CLI_AGENTS.has(agent)) return this.deps.executors.sendCli
    return null
  }

  /** Issued-but-unconsumed-and-unexpired tokens count toward the cap. */
  private inFlightCount(now: number): number {
    let count = 0
    for (const record of this.pending.values()) {
      if (!record.consumed && record.expiresAt > now) count += 1
    }
    return count
  }

  /**
   * Housekeeping. Pending records outlive their token TTL by the result
   * cache window so that late replays keep their binding check and late
   * reuse attempts still answer `token_reused` (not `token_missing`).
   */
  private prune(now: number): void {
    for (const [id, record] of this.pending) {
      if (record.expiresAt + RESULT_CACHE_TTL_MS <= now) this.pending.delete(id)
    }
    for (const [id, cached] of this.results) {
      if (cached.expiresAt <= now) this.results.delete(id)
    }
  }

  private rejectPrepare(
    req: PrepareRequest,
    digest: MessageDigest,
    errorCode: InjectErrorCode,
    detail?: string,
  ): PrepareResult {
    this.record({
      ts: this.now(),
      phase: 'prepare',
      requestId: null,
      target: { agent: req.target.agent, sessionId: req.target.sessionId },
      mode: req.mode,
      ok: false,
      errorCode,
      messageBytes: digest.bytes,
      messageSha12: digest.sha12,
    })
    return detail === undefined
      ? { ok: false, errorCode }
      : { ok: false, errorCode, detail }
  }

  private rejectExecute(
    requestId: string,
    record: PendingToken | null,
    digest: MessageDigest,
    errorCode: InjectErrorCode,
  ): InjectResult {
    this.record({
      ts: this.now(),
      phase: 'execute',
      requestId,
      target: record === null ? null : { ...record.target },
      mode: record === null ? null : record.mode,
      ok: false,
      outcome: 'failed',
      errorCode,
      messageBytes: digest.bytes,
      messageSha12: digest.sha12,
    })
    return { outcome: 'failed', errorCode }
  }

  private logExecuteResult(
    requestId: string,
    record: PendingToken | null,
    digest: MessageDigest,
    result: InjectResult,
  ): void {
    this.record({
      ts: this.now(),
      phase: 'execute',
      requestId,
      target: record === null ? null : { ...record.target },
      mode: record === null ? null : record.mode,
      ok: result.outcome === 'delivered',
      outcome: result.outcome,
      ...(result.errorCode !== undefined ? { errorCode: result.errorCode } : {}),
      ...(result.replayed !== undefined ? { replayed: result.replayed } : {}),
      messageBytes: digest.bytes,
      messageSha12: digest.sha12,
    })
  }

  private record(entry: InjectLogEntry): void {
    const frozen: InjectLogEntry = Object.freeze({
      ...entry,
      target: entry.target === null ? null : Object.freeze({ ...entry.target }),
    })
    this.logRing.push(frozen)
    if (this.logRing.length > LOG_RING_LIMIT) {
      this.logRing.splice(0, this.logRing.length - LOG_RING_LIMIT)
    }
    this.deps.log(frozen)
  }
}
