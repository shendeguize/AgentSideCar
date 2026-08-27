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
 *   `resume({resumeSessionId,agentOptions?,signal?,setup?}): Promise<AgentHandle>` (:296)
 *   with `agentOptions.provider/model` declared in runtime-types.d.ts:21-28,
 *   and
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
 * `target_not_found | executor_error | timeout`; dsh-native error text is
 * never returned or logged. Log lines never carry the message body — only
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
import { performance } from 'node:perf_hooks'

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

/** `AgentHandle` face (index.d.ts:155-158), including late-timeout cleanup. */
export interface DshAgentHandleFace {
  readonly agent: DshAgentFace
  dispose(): Promise<void>
}

/** Complete route required to start a cold resumed Agent. */
export interface DshModelRoute {
  readonly provider: string
  readonly model: string
}

/**
 * Narrow host port for the current default route. `null` means unavailable;
 * callers defensively revalidate the pair at the boundary.
 */
export type HostDefaultModelResolver = () => DshModelRoute | null

/** Synchronous publication commit returned by the rc.2 AgentSetup contract. */
export interface DshAgentSetupCommitFace {
  commit(): void
}

/** Unpublished setup context exposes the exact restored Agent + Session. */
export interface AgentSetupContextFace {
  readonly agent?: {
    readonly session?: {
      readonly header: unknown
      readonly events: readonly unknown[]
    }
  }
}

/** Setup accepted by `agents.resume`; the cold proof always supplies a sync one. */
export type DshAgentSetupFace = (
  agentContext: AgentSetupContextFace,
) =>
  | DshAgentSetupCommitFace
  | Promise<DshAgentSetupCommitFace | void>
  | void

/** A synchronous, no-await guard installed in the unpublished transaction. */
export type DshSynchronousSetupGuard = (
  agentContext: AgentSetupContextFace,
) => DshAgentSetupCommitFace | void

/** Cold existence + effective-preset proof produced by the authoritative host port. */
export type DshColdPresetProof =
  | {
      readonly state: 'absent'
      readonly setup: DshSynchronousSetupGuard
    }
  | { readonly state: 'missing' }
  | { readonly state: 'present' }
  | { readonly state: 'unknown' }

/** Stable internal rejection from the unpublished setup/commit boundary. */
export class DshResumeGuardError extends Error {
  readonly errorCode: 'dsh_preset_unsupported' | 'executor_error'

  constructor(errorCode: 'dsh_preset_unsupported' | 'executor_error') {
    super('dsh cold resume publication guard rejected')
    this.name = 'DshResumeGuardError'
    this.errorCode = errorCode
  }
}

/** Abortable host inspection port over persisted header + event state. */
export type ColdPresetResolver = (
  sessionId: string,
  signal: AbortSignal,
) => Promise<DshColdPresetProof>

/** Minimal `ctx.agents` (`AgentRegistry`) face: locate + resume. */
export interface AgentsServiceFace {
  /** Whether the cold resume owner binding (agents + persistence) is live. */
  isAvailable(): boolean
  /** Live lookup (index.d.ts:349); undefined = session not loaded. */
  get(sessionId: string): DshAgentFace | undefined
  /** Load a persisted session and start an agent on it (index.d.ts:296). */
  resume(options: {
    readonly resumeSessionId: string
    readonly agentOptions?: DshModelRoute
    readonly signal?: AbortSignal
    readonly setup?: DshAgentSetupFace
  }): Promise<DshAgentHandleFace>
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

/**
 * Resolve and validate one complete host-default pair. The runtime validation
 * is intentional even though the port is narrow: optional/foreign services
 * can still return partial or blank data through structural casts.
 */
function resolveModelRoute(
  resolver: HostDefaultModelResolver | undefined,
): DshModelRoute | null {
  if (resolver === undefined) return null
  try {
    const route = resolver() as
      | { readonly provider?: unknown; readonly model?: unknown }
      | null
    if (route === null) return null
    const provider =
      typeof route.provider === 'string' ? route.provider.trim() : ''
    const model = typeof route.model === 'string' ? route.model.trim() : ''
    return provider !== '' && model !== '' ? { provider, model } : null
  } catch {
    return null
  }
}

const DEADLINE_TIMEOUT = Symbol('dsh-deadline-timeout')

interface OperationDeadline {
  readonly controller: AbortController
  readonly expiresAt: number
  readonly timeout: Promise<typeof DEADLINE_TIMEOUT>
  readonly expired: () => boolean
  readonly clear: () => void
}

function createDeadline(
  timeoutMs: number,
  now: () => number,
): OperationDeadline {
  const controller = new AbortController()
  const expiresAt = now() + timeoutMs
  let deadlineWon = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const timeout = new Promise<typeof DEADLINE_TIMEOUT>((resolve) => {
    timer = setTimeout(() => {
      timer = undefined
      deadlineWon = true
      resolve(DEADLINE_TIMEOUT)
      controller.abort(new Error('dsh cold resume deadline exceeded'))
    }, Math.max(0, timeoutMs))
  })
  return {
    controller,
    expiresAt,
    timeout,
    expired: () => deadlineWon || now() >= expiresAt,
    clear: () => {
      if (timer !== undefined) {
        clearTimeout(timer)
        timer = undefined
      }
    },
  }
}

type DeadlineResult<T> =
  | { readonly kind: 'value'; readonly value: T }
  | { readonly kind: 'error' }
  | { readonly kind: 'timeout' }

async function settleBeforeDeadline<T>(
  operation: Promise<T>,
  deadline: OperationDeadline,
): Promise<DeadlineResult<T>> {
  const observed = operation.then(
    (value): DeadlineResult<T> => ({ kind: 'value', value }),
    (): DeadlineResult<T> => ({ kind: 'error' }),
  )
  const settled = await Promise.race([observed, deadline.timeout])
  return settled === DEADLINE_TIMEOUT ? { kind: 'timeout' } : settled
}

async function disposeQuietly(handle: DshAgentHandleFace): Promise<boolean> {
  try {
    await handle.dispose()
    return true
  } catch {
    return false
  }
}

type ResumeOutcome =
  | { readonly kind: 'agent'; readonly agent: DshAgentFace }
  | {
      readonly kind: 'failed'
      readonly errorCode:
        | 'target_not_found'
        | 'dsh_preset_unsupported'
        | 'dsh_agents_unavailable'
        | 'executor_error'
    }
  | { readonly kind: 'timeout' }
  | { readonly kind: 'stale' }
  | { readonly kind: 'retire-failed' }

type RetirementResult =
  | { readonly kind: 'retired' }
  | { readonly kind: 'failed' }
type WaitOutcome = Exclude<ResumeOutcome, { readonly kind: 'retire-failed' }>

interface ResumingSlot {
  readonly state: 'resuming'
  readonly generation: object
  readonly controller: AbortController
  readonly operation: Promise<ResumeOutcome>
}

interface FulfilledSlot {
  readonly state: 'fulfilled'
  readonly generation: object
  readonly agent: DshAgentFace
  readonly handle: DshAgentHandleFace
}

interface RetiringSlot {
  readonly state: 'retiring'
  readonly generation: object
  readonly settlement: Promise<RetirementResult>
}

type ResumeSlot = ResumingSlot | FulfilledSlot | RetiringSlot

export interface DshInjectExecutor extends InjectExecutor {
  /** Retire all slots owned by one departing cold-service binding. */
  invalidateColdServiceGeneration(generation: object): Promise<void>
  /** Abort pending owned resumes and dispose every fulfilled slot-owned handle. */
  dispose(): Promise<void>
}

type ColdCheck =
  | {
      readonly kind: 'ready'
      readonly route: DshModelRoute
      readonly proof: Extract<DshColdPresetProof, { readonly state: 'absent' }>
      readonly generation: object
      readonly modelRouteAvailable: true
      readonly presetInspectionAvailable: true
      readonly presetSupported: true
    }
  | {
      readonly kind: 'live'
      readonly agent: DshAgentFace
      readonly generation: object
      readonly modelRouteAvailable: boolean
      readonly presetInspectionAvailable: true
      readonly presetSupported: boolean
    }
  | {
      readonly kind: 'failed'
      readonly errorCode:
        | 'target_not_found'
        | 'dsh_model_unconfigured'
        | 'dsh_preset_unsupported'
        | 'dsh_agents_unavailable'
        | 'executor_error'
        | 'timeout'
      readonly modelRouteAvailable: boolean
      readonly presetInspectionAvailable: boolean
      readonly presetSupported: boolean
    }

// ---------------------------------------------------------------------------
// Executor.
// ---------------------------------------------------------------------------

const MAX_EXECUTOR_DETAIL_CHARS = 512

function sanitizedExecutorDetail(error: unknown, fallback: string): string {
  const raw = error instanceof Error ? error.message : String(error)
  const normalized = raw
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/(?:[A-Za-z]:[\\/]|\/)[^\s"'<>]+/g, '<path>')
    .replace(/\s+/g, ' ')
    .trim()
  const detail = normalized === '' ? fallback : `${fallback}: ${normalized}`
  return detail.slice(0, MAX_EXECUTOR_DETAIL_CHARS)
}

export function createDshInjectExecutor(deps: {
  agents: AgentsServiceFace
  /** Optional because the host service itself is optional; cold use fails closed. */
  resolveHostDefaultModel?: HostDefaultModelResolver
  /** Abortable effective-preset inspection over public host services. */
  resolveColdPreset?: ColdPresetResolver
  log?: DshInjectLog
  pluginName?: string
  /** Absolute cold inspection + resume deadline. */
  resumeTimeoutMs?: number
  /** Per-request waiter timeout override; the shared slot keeps its own deadline. */
  requestTimeoutMs?: (req: InjectExecutionRequest) => number
  /** Identity token for the currently mounted agents+persistence binding. */
  currentColdServiceGeneration?: () => object | null
  /** Monotonic clock override for tests. */
  now?: () => number
}): DshInjectExecutor {
  const pluginName = deps.pluginName ?? DEFAULT_PLUGIN_NAME
  const log: DshInjectLog = deps.log ?? (() => {})
  const resumeTimeoutMs = deps.resumeTimeoutMs ?? DEFAULT_RESUME_TIMEOUT_MS
  const now = deps.now ?? performance.now.bind(performance)
  const slots = new Map<string, ResumeSlot>()
  const generationInvalidations = new Map<object, Promise<void>>()
  const fallbackGeneration = {}
  const resolveCurrentGeneration =
    deps.currentColdServiceGeneration ??
    (() => (deps.agents.isAvailable() ? fallbackGeneration : null))
  const currentGeneration = (): object | null => {
    try {
      return resolveCurrentGeneration()
    } catch {
      return null
    }
  }
  let unloading = false
  let disposePromise: Promise<void> | null = null

  const checkCold = async (
    sessionId: string,
    deadline: OperationDeadline,
    timeoutCode: 'executor_error' | 'timeout',
  ): Promise<ColdCheck> => {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const generation = currentGeneration()
      if (unloading || generation === null || !deps.agents.isAvailable()) {
        return {
          kind: 'failed',
          errorCode: 'dsh_agents_unavailable',
          modelRouteAvailable: false,
          presetInspectionAvailable: false,
          presetSupported: false,
        }
      }
      if (deps.resolveColdPreset === undefined) {
        return {
          kind: 'failed',
          errorCode: 'executor_error',
          modelRouteAvailable: false,
          presetInspectionAvailable: false,
          presetSupported: false,
        }
      }

      const inspected = await settleBeforeDeadline(
        Promise.resolve().then(() =>
          deps.resolveColdPreset!(sessionId, deadline.controller.signal),
        ),
        deadline,
      )
      if (inspected.kind === 'timeout') {
        return {
          kind: 'failed',
          errorCode: timeoutCode,
          modelRouteAvailable: false,
          presetInspectionAvailable: false,
          presetSupported: false,
        }
      }
      if (currentGeneration() !== generation) continue
      let live: DshAgentFace | undefined
      try {
        live = deps.agents.get(sessionId)
      } catch {
        return {
          kind: 'failed',
          errorCode: 'executor_error',
          modelRouteAvailable: false,
          presetInspectionAvailable: false,
          presetSupported: false,
        }
      }
      if (live !== undefined) {
        return {
          kind: 'live',
          agent: live,
          generation,
          modelRouteAvailable: false,
          presetInspectionAvailable: true,
          presetSupported:
            inspected.kind === 'value' && inspected.value.state === 'absent',
        }
      }
      if (inspected.kind === 'error' || inspected.value.state === 'unknown') {
        return {
          kind: 'failed',
          errorCode: 'executor_error',
          modelRouteAvailable: false,
          presetInspectionAvailable: false,
          presetSupported: false,
        }
      }
      if (inspected.value.state === 'missing') {
        return {
          kind: 'failed',
          errorCode: 'target_not_found',
          modelRouteAvailable: false,
          presetInspectionAvailable: true,
          presetSupported: false,
        }
      }
      if (!deps.agents.isAvailable()) {
        return {
          kind: 'failed',
          errorCode: 'dsh_agents_unavailable',
          modelRouteAvailable: false,
          presetInspectionAvailable: true,
          presetSupported: inspected.value.state === 'absent',
        }
      }
      if (inspected.value.state === 'present') {
        return {
          kind: 'failed',
          errorCode: 'dsh_preset_unsupported',
          modelRouteAvailable: false,
          presetInspectionAvailable: true,
          presetSupported: false,
        }
      }
      const route = resolveModelRoute(deps.resolveHostDefaultModel)
      if (route === null) {
        return {
          kind: 'failed',
          errorCode: 'dsh_model_unconfigured',
          modelRouteAvailable: false,
          presetInspectionAvailable: true,
          presetSupported: true,
        }
      }
      return {
        kind: 'ready',
        route,
        proof: inspected.value,
        generation,
        modelRouteAvailable: true,
        presetInspectionAvailable: true,
        presetSupported: true,
      }
    }
    return {
      kind: 'failed',
      errorCode: 'executor_error',
      modelRouteAvailable: false,
      presetInspectionAvailable: false,
      presetSupported: false,
    }
  }

  const startResumeSlot = (
    sessionId: string,
    route: DshModelRoute,
    setup: DshSynchronousSetupGuard,
    generation: object,
  ): ResumingSlot => {
    const slotDeadline = createDeadline(resumeTimeoutMs, now)
    let raw: Promise<DshAgentHandleFace>
    try {
      raw = Promise.resolve(deps.agents.resume({
        resumeSessionId: sessionId,
        agentOptions: { provider: route.provider, model: route.model },
        signal: slotDeadline.controller.signal,
        setup,
      }))
    } catch (error) {
      raw = Promise.reject(error)
    }
    let slot: ResumingSlot
    const operation = raw.then(
      async (handle): Promise<ResumeOutcome> => {
        slotDeadline.clear()
        if (unloading) {
          return (await disposeQuietly(handle))
            ? { kind: 'failed', errorCode: 'executor_error' }
            : { kind: 'retire-failed' }
        }
        if (
          currentGeneration() !== generation ||
          slots.get(sessionId) !== slot
        ) {
          return (await disposeQuietly(handle))
            ? { kind: 'stale' }
            : { kind: 'retire-failed' }
        }
        const fulfilled: FulfilledSlot = {
          state: 'fulfilled',
          generation,
          agent: handle.agent,
          handle,
        }
        slots.set(sessionId, fulfilled)
        return { kind: 'agent', agent: handle.agent }
      },
      async (error): Promise<ResumeOutcome> => {
        if (slots.get(sessionId) === slot) slots.delete(sessionId)
        if (unloading) {
          slotDeadline.clear()
          return { kind: 'failed', errorCode: 'executor_error' }
        }
        if (currentGeneration() !== generation) {
          slotDeadline.clear()
          return { kind: 'stale' }
        }
        if (error instanceof DshResumeGuardError) {
          slotDeadline.clear()
          return { kind: 'failed', errorCode: error.errorCode }
        }
        let winner: DshAgentFace | undefined
        try {
          winner = deps.agents.get(sessionId)
        } catch {
          slotDeadline.clear()
          return { kind: 'failed', errorCode: 'executor_error' }
        }
        if (winner !== undefined) {
          slotDeadline.clear()
          return { kind: 'agent', agent: winner }
        }
        if (slotDeadline.expired()) {
          slotDeadline.clear()
          return { kind: 'timeout' }
        }
        if (deps.resolveColdPreset !== undefined) {
          const proof = await settleBeforeDeadline(
            Promise.resolve().then(() =>
              deps.resolveColdPreset!(sessionId, slotDeadline.controller.signal),
            ),
            slotDeadline,
          )
          if (proof.kind === 'timeout') {
            slotDeadline.clear()
            return { kind: 'timeout' }
          }
          if (currentGeneration() !== generation) {
            slotDeadline.clear()
            return { kind: 'stale' }
          }
          try {
            winner = deps.agents.get(sessionId)
          } catch {
            slotDeadline.clear()
            return { kind: 'failed', errorCode: 'executor_error' }
          }
          if (winner !== undefined) {
            slotDeadline.clear()
            return { kind: 'agent', agent: winner }
          }
          if (proof.kind === 'value') {
            if (proof.value.state === 'missing') {
              slotDeadline.clear()
              return { kind: 'failed', errorCode: 'target_not_found' }
            }
            if (proof.value.state === 'present') {
              slotDeadline.clear()
              return {
                kind: 'failed',
                errorCode: 'dsh_preset_unsupported',
              }
            }
          }
        }
        slotDeadline.clear()
        return { kind: 'failed', errorCode: 'executor_error' }
      },
    )
    slot = {
      state: 'resuming',
      generation,
      controller: slotDeadline.controller,
      operation,
    }
    slots.set(sessionId, slot)
    return slot
  }

  const waitForSlot = async (
    slot: ResumeSlot,
    deadline: OperationDeadline,
  ): Promise<WaitOutcome> => {
    if (slot.state === 'retiring') {
      const settled = await settleBeforeDeadline(slot.settlement, deadline)
      if (settled.kind === 'timeout') return { kind: 'timeout' }
      if (settled.kind === 'error' || settled.value.kind === 'failed') {
        return { kind: 'failed', errorCode: 'executor_error' }
      }
      return { kind: 'stale' }
    }
    if (slot.generation !== currentGeneration()) return { kind: 'stale' }
    if (slot.state === 'fulfilled') {
      return { kind: 'agent', agent: slot.agent }
    }
    const settled = await settleBeforeDeadline(slot.operation, deadline)
    if (settled.kind === 'timeout') return { kind: 'timeout' }
    if (settled.kind === 'error') {
      return { kind: 'failed', errorCode: 'executor_error' }
    }
    if (settled.value.kind === 'retire-failed') {
      return { kind: 'failed', errorCode: 'executor_error' }
    }
    if (slot.generation !== currentGeneration()) return { kind: 'stale' }
    return settled.value
  }

  const beginRetirement = (
    sessionId: string,
    slot: ResumingSlot | FulfilledSlot,
  ): RetiringSlot => {
    const current = slots.get(sessionId)
    if (current !== slot && current?.state === 'retiring') return current

    let settle!: (result: RetirementResult) => void
    const base = new Promise<RetirementResult>((resolve) => {
      settle = resolve
    })
    let tombstone: RetiringSlot
    const settlement = base.then((result) => {
      if (slots.get(sessionId) === tombstone) slots.delete(sessionId)
      return result
    })
    tombstone = {
      state: 'retiring',
      generation: slot.generation,
      settlement,
    }
    slots.set(sessionId, tombstone)

    if (slot.state === 'resuming') {
      slot.controller.abort(new Error('dsh cold service generation retired'))
      void slot.operation.then(
        (outcome) =>
          settle(
            outcome.kind === 'retire-failed'
              ? { kind: 'failed' }
              : { kind: 'retired' },
          ),
        () => settle({ kind: 'failed' }),
      )
    } else {
      void disposeQuietly(slot.handle).then((disposed) =>
        settle(disposed ? { kind: 'retired' } : { kind: 'failed' }),
      )
    }
    return tombstone
  }

  const invalidateGeneration = (generation: object): Promise<void> => {
    const existing = generationInvalidations.get(generation)
    if (existing !== undefined) return existing
    const owned = [...slots.entries()].filter(
      ([, slot]) => slot.generation === generation,
    )
    const cleanup = Promise.all(
      owned.map(([sessionId, slot]) =>
        slot.state === 'retiring'
          ? slot.settlement
          : beginRetirement(sessionId, slot).settlement,
      ),
    ).then(() => {})
    generationInvalidations.set(generation, cleanup)
    void cleanup.then(() => {
      if (generationInvalidations.get(generation) === cleanup) {
        generationInvalidations.delete(generation)
      }
    })
    return cleanup
  }

  const usableSlot = (sessionId: string): ResumeSlot | undefined => {
    const slot = slots.get(sessionId)
    if (slot === undefined) return undefined
    if (slot.state === 'retiring') return slot
    if (slot.generation !== currentGeneration()) {
      void invalidateGeneration(slot.generation)
      return slots.get(sessionId)
    }
    if (slot.state === 'resuming') return slot
    try {
      if (deps.agents.get(sessionId) === slot.agent) return slot
    } catch {
      // A departing registry can throw while its owner tears down.
    }
    return beginRetirement(sessionId, slot)
  }

  const disposeSlots = (): Promise<void> => {
    if (disposePromise !== null) return disposePromise
    unloading = true
    const owned = [...slots.values()]
    for (const slot of owned) {
      if (slot.state === 'resuming') {
        slot.controller.abort(new Error('dsh injection executor unloading'))
      }
    }
    disposePromise = Promise.all(
      [...new Set(owned.map((slot) => slot.generation))].map((generation) =>
        invalidateGeneration(generation),
      ),
    ).then(() => {
      slots.clear()
    })
    return disposePromise
  }

  const timeoutFor = (req: InjectExecutionRequest): number => {
    if (deps.requestTimeoutMs === undefined) return resumeTimeoutMs
    try {
      const value = deps.requestTimeoutMs(req)
      return Number.isFinite(value) && value >= 0 ? value : resumeTimeoutMs
    } catch {
      return resumeTimeoutMs
    }
  }

  return {
    kind: 'dsh',
    async preflight(target) {
      if (unloading) return { ok: false, errorCode: 'executor_error' }
      let deadline: OperationDeadline | null = null
      const existingSlot = usableSlot(target.sessionId)
      if (existingSlot?.state === 'retiring') {
        deadline = createDeadline(resumeTimeoutMs, now)
        const retired = await waitForSlot(existingSlot, deadline)
        if (retired.kind === 'timeout' || retired.kind === 'failed') {
          deadline.clear()
          return { ok: false, errorCode: 'executor_error' }
        }
      } else if (existingSlot !== undefined) {
        return { ok: true }
      }
      // A live Agent already owns its route. Never resolve, resume, or mutate
      // that route during prepare.
      if (deps.agents.get(target.sessionId) !== undefined) {
        deadline?.clear()
        log('debug', 'dsh injection preflight ready', {
          liveAgentAvailable: true,
        })
        return { ok: true }
      }

      deadline ??= createDeadline(resumeTimeoutMs, now)
      const checked = await checkCold(
        target.sessionId,
        deadline,
        'executor_error',
      )
      if (checked.kind === 'live') {
        deadline.clear()
        return { ok: true }
      }
      if (!deadline.expired() && deps.agents.get(target.sessionId) !== undefined) {
        deadline.clear()
        return { ok: true }
      }
      deadline.clear()
      const ready = checked.kind === 'ready'
      const errorCode =
        checked.kind === 'failed' && checked.errorCode !== 'timeout'
          ? checked.errorCode
          : 'executor_error'
      log(
        ready ? 'debug' : 'warn',
        ready
          ? 'dsh cold injection preflight ready'
          : 'dsh cold injection preflight rejected',
        {
          liveAgentAvailable: false,
          coldServicesAvailable: deps.agents.isAvailable(),
          routingSource: 'host-default',
          modelRouteAvailable: checked.modelRouteAvailable,
          presetInspectionAvailable: checked.presetInspectionAvailable,
          presetSupported: checked.presetSupported,
        },
      )
      return ready
        ? { ok: true }
        : { ok: false, errorCode }
    },
    async execute(req: InjectExecutionRequest): Promise<InjectResult> {
      if (unloading) {
        return { outcome: 'failed', errorCode: 'executor_error' }
      }
      const sessionId = req.target.sessionId
      const messageBytes = Buffer.byteLength(req.message, 'utf8')
      const messageSha12 = createHash('sha256')
        .update(req.message, 'utf8')
        .digest('hex')
        .slice(0, SHA_LOG_CHARS)
      const baseMeta = {
        mode: req.mode,
        messageBytes,
        messageSha12,
      }

      let agent: DshAgentFace | undefined
      let coldPath = false
      let requestDeadline: OperationDeadline | null = null
      selection: for (
        let generationAttempt = 0;
        generationAttempt < 4;
        generationAttempt += 1
      ) {
        agent = undefined
        coldPath = false
        let selectedGeneration: object | null = null
        let slot = usableSlot(sessionId)

        if (slot !== undefined) {
          coldPath = true
          selectedGeneration = slot.generation
          if (slot.state === 'fulfilled') {
            agent = slot.agent
          } else {
            requestDeadline ??= createDeadline(timeoutFor(req), now)
            const outcome = await waitForSlot(slot, requestDeadline)
            if (outcome.kind === 'stale') {
              continue selection
            }
            if (outcome.kind === 'timeout') {
              return { outcome: 'failed', errorCode: 'timeout' }
            }
            if (outcome.kind === 'failed') {
              requestDeadline.clear()
              return { outcome: 'failed', errorCode: outcome.errorCode }
            }
            agent = outcome.agent
          }
        } else {
          agent = deps.agents.get(sessionId)
        }

        if (agent === undefined) {
          coldPath = true
          requestDeadline ??= createDeadline(timeoutFor(req), now)
          const checked = await checkCold(sessionId, requestDeadline, 'timeout')

          // A same-plugin slot linearizes before any live lookup: while its
          // resume is publishing, waiters must join it rather than adopt a
          // transiently visible Agent through get().
          slot = usableSlot(sessionId)
          if (slot !== undefined) {
            selectedGeneration = slot.generation
            const outcome = await waitForSlot(slot, requestDeadline)
            if (outcome.kind === 'stale') {
              continue selection
            }
            if (outcome.kind === 'timeout') {
              return { outcome: 'failed', errorCode: 'timeout' }
            }
            if (outcome.kind === 'failed') {
              requestDeadline.clear()
              return { outcome: 'failed', errorCode: outcome.errorCode }
            }
            agent = outcome.agent
          } else if (checked.kind === 'live') {
            agent = checked.agent
            selectedGeneration = checked.generation
            coldPath = false
          } else if (!requestDeadline.expired()) {
            agent = deps.agents.get(sessionId)
          }

          if (agent === undefined && checked.kind === 'failed') {
            requestDeadline.clear()
            log('warn', 'dsh cold injection rejected', {
              ...baseMeta,
              coldServicesAvailable: deps.agents.isAvailable(),
              routingSource: 'host-default',
              modelRouteAvailable: checked.modelRouteAvailable,
              presetInspectionAvailable: checked.presetInspectionAvailable,
              presetSupported: checked.presetSupported,
            })
            return { outcome: 'failed', errorCode: checked.errorCode }
          }

          if (agent === undefined && checked.kind === 'ready') {
            if (currentGeneration() !== checked.generation) {
              continue selection
            }
            // No await between the final slot check and insertion: this
            // request's generation/route/proof is the linearization winner.
            slot = usableSlot(sessionId)
            if (slot === undefined) {
              const live = deps.agents.get(sessionId)
              if (live !== undefined) agent = live
              else {
                slot = startResumeSlot(
                  sessionId,
                  checked.route,
                  checked.proof.setup,
                  checked.generation,
                )
              }
            }
            if (agent === undefined && slot !== undefined) {
              selectedGeneration = slot.generation
              const outcome = await waitForSlot(slot, requestDeadline)
              if (outcome.kind === 'stale') {
                continue selection
              }
              if (outcome.kind === 'timeout') {
                return { outcome: 'failed', errorCode: 'timeout' }
              }
              if (outcome.kind === 'failed') {
                requestDeadline.clear()
                log('warn', 'dsh resume failed', {
                  ...baseMeta,
                  routingSource: 'host-default',
                  modelRouteAvailable: true,
                  presetSupported:
                    outcome.errorCode !== 'dsh_preset_unsupported',
                })
                return { outcome: 'failed', errorCode: outcome.errorCode }
              }
              agent = outcome.agent
            }
          }
        }

        if (requestDeadline !== null && requestDeadline.expired()) {
          requestDeadline.clear()
          return { outcome: 'failed', errorCode: 'timeout' }
        }
        if (
          selectedGeneration !== null &&
          selectedGeneration !== currentGeneration()
        ) {
          continue selection
        }
        requestDeadline?.clear()
        if (agent !== undefined) break selection
      }
      if (agent === undefined) {
        requestDeadline?.clear()
        return { outcome: 'failed', errorCode: 'executor_error' }
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
        log('warn', 'dsh injection call threw', {
          ...baseMeta,
          coldPath,
          resumed: coldPath,
          ...(coldPath
            ? { routingSource: 'host-default', modelRouteAvailable: true }
            : {}),
        })
        return {
          outcome: 'failed',
          errorCode: 'executor_error',
          detail: sanitizedExecutorDetail(error, 'dsh injection call failed'),
        }
      }

      log('info', 'dsh injection delivered', {
        ...baseMeta,
        coldPath,
        resumed: coldPath,
        ...(coldPath
          ? { routingSource: 'host-default', modelRouteAvailable: true }
          : {}),
      })
      return { outcome: 'delivered' }
    },
    invalidateColdServiceGeneration: invalidateGeneration,
    dispose: disposeSlots,
  }
}
