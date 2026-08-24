/**
 * dsh in-process injection executor — injection path one of design §4.d
 * (.local/tasks/make_dsh_mode/design/dsh_plugin_design.md): native dsh
 * sessions are reached through the `ctx.agents` registry, closing the
 * sidecar send `unsupported_dsh` gap.
 *
 * API facts verified against the installed SDK
 * (`@deepseek-ai/dsh-agent@0.1.1-rc.2` d.ts, authoritative over the design
 * sketch):
 *
 * - `ctx.agents` is `AgentRegistry` (lib/types/index.d.ts:28):
 *   `get(id): Agent | undefined` (:349) and
 *   `resume({resumeSessionId}): Promise<AgentHandle>` (:296) with
 *   `AgentHandle = { agent; dispose() }` (:155-158).
 * - `Agent.followup(message: UserMessage): void` and
 *   `Agent.steer(message: UserMessage): void` are SYNCHRONOUS inbox splices
 *   (runtime-types.d.ts:115/:123) — the design sketch's `await` is a
 *   deviation. A sync return means the message entered the live inbox
 *   (delivered); a sync throw is the only call-site failure face.
 * - `UserMessage` = `{ id, role: 'user', content: ContentBlock[], source }`
 *   (dsh-llm message.d.ts:120-133); text content is `[{type:'text',text}]`
 *   (types.d.ts:39-42, F11) and plugin attribution is
 *   `source: { kind: 'plugin', plugin: <name> }` (message.d.ts:98-101).
 * - resume is NOT idempotent: resuming a live session throws
 *   `cannot prepare session "<id>" while it is live`
 *   (session-persistence coordinator), and resume rejects when no
 *   persistence backend is configured — so the executor MUST `get` first
 *   and only resume a miss. persistence prepare may also retry unboundedly
 *   under concurrent writers, hence the bounded resume wait here.
 *
 * queue → `followup` (own next turn), steer → `steer` (nearest step),
 * matching dsh's own `session.prompt` mode vocabulary.
 *
 * Error vocabulary is normalized with path two on the shared subset
 * `session_not_found | executor_error | timeout`; dsh-native error text
 * only travels in `detail`. Log lines never carry the message body — only
 * byte size and a sha256 prefix (S8).
 *
 * Pure DI: no cordis/dsh imports. {@link AgentsServiceFace} is a minimal
 * structural face extracted from the d.ts; the real `AgentRegistry`
 * satisfies it directly (method-syntax members keep parameter checks
 * bivariant, so the SDK's branded `SessionId` / wider `UserMessage`
 * signatures remain assignable).
 *
 * @module
 */

import { createHash } from 'node:crypto'

import type { InjectExecutionRequest, InjectExecutor, InjectResult } from './inject-gateway.ts'

// ---------------------------------------------------------------------------
// Minimal structural faces over the dsh-agent SDK.
// ---------------------------------------------------------------------------

/** The exact message this executor constructs (F11: content-block array). */
export interface DshInjectMessage {
  /** Plain-string form of the SDK's branded `MessageId`; unique per request. */
  readonly id: string
  readonly role: 'user'
  readonly content: readonly [{ readonly type: 'text'; readonly text: string }]
  /** Plugin attribution (message.d.ts:98-101). */
  readonly source: { readonly kind: 'plugin'; readonly plugin: string }
}

/**
 * Widened message parameter face: the SDK's `UserMessage` is assignable to
 * this shape, which keeps `Agent`'s method signatures structurally
 * compatible with {@link DshAgentFace} without importing SDK types.
 */
export interface DshUserMessageFace {
  readonly id: string
  readonly role: 'user'
  readonly content: ReadonlyArray<{ readonly type: string }>
  readonly source: { readonly kind: string }
}

/** Live-agent face: the two injection entry points (runtime-types.d.ts:115/:123). */
export interface DshAgentFace {
  followup(message: DshUserMessageFace): void
  steer(message: DshUserMessageFace): void
}

/** `AgentHandle` face (index.d.ts:155-158). `dispose` is deliberately absent:
 * the executor never tears down an agent it resumed — disposal would unload
 * the session and cancel the just-queued work. */
export interface DshAgentHandleFace {
  readonly agent: DshAgentFace
}

/** Minimal `ctx.agents` (`AgentRegistry`) face: locate + resume. */
export interface AgentsServiceFace {
  /** Live lookup (index.d.ts:349); undefined = session not loaded. */
  get(sessionId: string): DshAgentFace | undefined
  /** Load a persisted session and start an agent on it (index.d.ts:296). */
  resume(options: { readonly resumeSessionId: string }): Promise<DshAgentHandleFace>
}

// ---------------------------------------------------------------------------
// Logging seam (same shape as send-cli's; never receives the message body).
// ---------------------------------------------------------------------------

export type DshInjectLogLevel = 'debug' | 'info' | 'warn' | 'error'

export type DshInjectLog = (
  level: DshInjectLogLevel,
  msg: string,
  meta?: Record<string, unknown>,
) => void

// ---------------------------------------------------------------------------
// Tunables.
// ---------------------------------------------------------------------------

/** Default `source.plugin` attribution value. */
export const DEFAULT_PLUGIN_NAME = 'agent-sidecar'
/**
 * Bounded wait for `agents.resume`: persistence prepare retries unboundedly
 * under concurrent external writers, so an unloaded-session resume is the
 * one async face of this otherwise in-process path.
 */
export const DEFAULT_RESUME_TIMEOUT_MS = 30_000

/** Sha256 hex prefix length recorded in logs (matches inject-gateway). */
const SHA_LOG_CHARS = 12

// ---------------------------------------------------------------------------
// Internals.
// ---------------------------------------------------------------------------

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Race a resume against the bounded wait; null = timed out (still pending). */
async function resumeWithin(
  resume: Promise<DshAgentHandleFace>,
  timeoutMs: number,
): Promise<DshAgentHandleFace | null> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      resume,
      new Promise<null>((resolve) => {
        timer = setTimeout(() => resolve(null), timeoutMs)
      }),
    ])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

// ---------------------------------------------------------------------------
// Executor.
// ---------------------------------------------------------------------------

export function createDshInjectExecutor(deps: {
  agents: AgentsServiceFace
  log?: DshInjectLog
  pluginName?: string
  /** Bounded resume wait override; default {@link DEFAULT_RESUME_TIMEOUT_MS}. */
  resumeTimeoutMs?: number
}): InjectExecutor {
  const pluginName = deps.pluginName ?? DEFAULT_PLUGIN_NAME
  const log: DshInjectLog = deps.log ?? (() => {})
  const resumeTimeoutMs = deps.resumeTimeoutMs ?? DEFAULT_RESUME_TIMEOUT_MS

  return {
    kind: 'dsh',
    async execute(req: InjectExecutionRequest): Promise<InjectResult> {
      const sessionId = req.target.sessionId
      const messageBytes = Buffer.byteLength(req.message, 'utf8')
      const messageSha12 = createHash('sha256')
        .update(req.message, 'utf8')
        .digest('hex')
        .slice(0, SHA_LOG_CHARS)
      const baseMeta = {
        requestId: req.requestId,
        sessionId,
        mode: req.mode,
        messageBytes,
        messageSha12,
      }

      // Locate: loaded → inject directly; resume throws on a live session
      // ("cannot prepare ... while it is live"), so `get` must win first.
      let agent = deps.agents.get(sessionId)
      const resumed = agent === undefined

      if (agent === undefined) {
        log('debug', 'dsh session not loaded; resuming', baseMeta)
        let handle: DshAgentHandleFace | null
        try {
          handle = await resumeWithin(
            deps.agents.resume({ resumeSessionId: sessionId }),
            resumeTimeoutMs,
          )
        } catch (error) {
          const detail = describeError(error)
          log('warn', 'dsh resume failed', { ...baseMeta, error: detail })
          return { outcome: 'failed', errorCode: 'session_not_found', detail }
        }
        if (handle === null) {
          // The resume is still pending; nothing was injected, so 'failed'
          // is honest (unlike path two's post-dispatch 'unknown').
          log('warn', 'dsh resume timed out', { ...baseMeta, resumeTimeoutMs })
          return {
            outcome: 'failed',
            errorCode: 'timeout',
            detail: `resume did not settle within ${resumeTimeoutMs}ms`,
          }
        }
        // The handle's dispose() capability is intentionally dropped: the
        // resumed agent must stay live to run the queued/steered turn.
        agent = handle.agent
      }

      const message: DshInjectMessage = {
        id: `${pluginName}-${req.requestId}`,
        role: 'user',
        content: [{ type: 'text', text: req.message }],
        source: { kind: 'plugin', plugin: pluginName },
      }

      // queue → followup (own next turn), steer → steer (nearest step).
      // Both are synchronous inbox splices: sync return = delivered.
      try {
        if (req.mode === 'steer') agent.steer(message)
        else agent.followup(message)
      } catch (error) {
        const detail = describeError(error)
        log('warn', 'dsh injection call threw', { ...baseMeta, resumed, error: detail })
        return { outcome: 'failed', errorCode: 'executor_error', detail }
      }

      log('info', 'dsh injection delivered', { ...baseMeta, resumed })
      return { outcome: 'delivered' }
    },
  }
}
