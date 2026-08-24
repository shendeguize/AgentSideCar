import z from "@deepseek-ai/schemastery";
import { IncomingMessage, ServerResponse } from "node:http";
import { Readable, Writable } from "node:stream";
import { Context } from "@deepseek-ai/cordis";

//#region src/analysis.d.ts
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
/** Widened user-message face (same shape family as dsh-inject's, F11). */
interface AnalysisUserMessageFace {
  readonly id: string;
  readonly role: 'user';
  readonly content: ReadonlyArray<{
    readonly type: string;
    readonly text?: string;
  }>;
  readonly source: {
    readonly kind: string;
    readonly plugin?: string;
  };
}
/** Widened derived-message face over dsh-llm `Message` (message.d.ts:120-129). */
interface AnalysisDerivedMessageFace {
  readonly role: string;
  readonly content: ReadonlyArray<{
    readonly type: string;
    readonly text?: string;
  }>;
}
/** Widened session-event envelope face (dsh-session types.d.ts:425-443). */
interface AnalysisSessionEventFace {
  readonly type: string;
  /** `assistant/message` events carry `{ usage?: TokenUsage }` here. */
  readonly data?: unknown;
}
/** Read face over the live session log (dsh-session index.d.ts:106-267). */
interface AnalysisSessionLogFace {
  /** Cached surface projection of the derived LLM history (index.d.ts:259). */
  deriveMessages(): ReadonlyArray<AnalysisDerivedMessageFace>;
  /** Immutable append-only event snapshot (index.d.ts:174). */
  readonly events: ReadonlyArray<AnalysisSessionEventFace>;
}
/** Cancellation cause face; `{kind:'user'}` ∈ `AgentCancelCause` (dsh-session types.d.ts:118-127). */
interface AnalysisCancelCauseFace {
  readonly kind: 'user';
}
/** Live-agent face: prompt in, quiescence + log read back (runtime-types.d.ts:60-133). */
interface AnalysisAgentDriverFace {
  readonly session: AnalysisSessionLogFace;
  followup(message: AnalysisUserMessageFace): void;
  cancel(cause: AnalysisCancelCauseFace): void;
  whenIdle(): Promise<void>;
}
/**
 * The dedicated analysis session handle — `AgentHandle` face
 * (index.d.ts:155-158): send messages via `agent.followup`, read responses via
 * `agent.whenIdle` + `agent.session`, stop via `dispose()`.
 */
interface AnalysisSession {
  readonly agent: AnalysisAgentDriverFace;
  dispose(): Promise<void>;
}
/** Per-agent options face over SDK `AgentOptions` (runtime-types.d.ts:21-28). */
interface AnalysisAgentOptionsFace {
  /** Provider route (must have a registered adapter at call time). */
  readonly provider?: string;
  /** Model id interpreted by the selected provider adapter. */
  readonly model?: string;
  /** Maximum output tokens for each conversation-model request. */
  readonly maxTokens?: number;
}
/** `CreateAgentOptions` face (index.d.ts:65-118): engine-minted id + creation abort. */
interface AnalysisCreateOptions {
  readonly sessionId: string;
  readonly signal?: AbortSignal;
  /**
   * Provider/model routing for the analysis agent (`CreateAgentOptions.
   * agentOptions`). The engine never sets this — the integration layer's
   * create adapter resolves and attaches it (A-1: an agent created without
   * provider/model fails prompt assembly on the `{{model}}` variable and
   * `buildRequest`, completing with an empty summary and zero tokens).
   */
  readonly agentOptions?: AnalysisAgentOptionsFace;
  /**
   * Session creation metadata (`CreateAgentOptions.meta` subset). Also
   * attached by the create adapter, never the engine: the deployment
   * persona's `{{cwd}}` prompt variable reads `session.header.cwd`, which
   * only `meta.cwd` populates (same reason dsh-headless passes
   * `meta: { cwd: process.cwd() }` on its own create call).
   */
  readonly meta?: {
    readonly cwd?: string;
  };
}
/** Minimal `ctx.agents` face: the one factory entry point this engine uses. */
interface AnalysisAgentFace {
  /** `AgentRegistry.create` (index.d.ts:288). */
  create(options: AnalysisCreateOptions): Promise<AnalysisSession>;
}
//#endregion
//#region src/supervisor.d.ts
type SupervisorPolicy = 'adopt-or-host' | 'adopt-only' | 'off';
//#endregion
//#region src/config.d.ts
/** Daemon lifecycle governance (design §4.a). */
interface DaemonConfig {
  /** adopt-or-host: probe→adopt→else spawn; adopt-only: never spawn; off: no lifecycle management (read-only reconcile still runs). */
  policy: SupervisorPolicy;
  /** Consecutive hosting failures before the supervisor trips FAILED. */
  backoffLimit: number;
}
/** How to reach/launch the sidecar itself. */
interface SidecarInvocationConfig {
  /** argv prefix of the sidecar executable (PATH name, absolute path, or e.g. python3+zipapp as multiple entries). */
  command: string[];
  /** Empty = default `~/.agent_sidecar` (honoring AGENT_SIDECAR_RUNTIME_DIR); non-empty redirects via env for spawned daemons. */
  runtimeDir: string;
}
/** Reconciler snapshot cadences (design §4.b / ADR-2). */
interface StreamConfig {
  /** `status` snapshot cadence while any session is working (ms). */
  reconcileActiveMs: number;
  /** `status` snapshot cadence otherwise (ms). */
  reconcileIdleMs: number;
}
/** Write-path master switch and defaults (M2 consumes defaultMode). */
interface InjectConfig {
  /** Master gate: false hides all inject affordances and 403s write actions server-side. */
  enabled: boolean;
  /** Default injection mode offered by the inject panel. */
  defaultMode: 'queue' | 'steer';
}
/** AI bypass-analysis switch and model routing (M3). */
interface AnalysisConfig {
  enabled: boolean;
  /**
   * Explicit provider route for the dedicated analysis agents. Empty (the
   * default) reuses the host's default model selection (`agentDefaultModel`
   * service, the same source dsh's own entry points read). Takes effect
   * only together with a non-empty `model`.
   */
  provider: string;
  /** Explicit model id for the analysis agents; see {@link provider}. */
  model: string;
}
/** Board rendering knobs (client half). */
interface UiConfig {
  /** Session recency window shown on the board (hours). */
  timeWindowHours: number;
  /** Whether dead sessions are listed. */
  showDead: boolean;
}
/** Skill provider switch (M4). */
interface SkillConfig {
  provide: boolean;
}
/** Validated composition config (all defaults filled by the schema). */
interface Config {
  daemon: DaemonConfig;
  sidecar: SidecarInvocationConfig;
  stream: StreamConfig;
  inject: InjectConfig;
  analysis: AnalysisConfig;
  ui: UiConfig;
  skill: SkillConfig;
}
declare const Config: z<Config>;
//#endregion
//#region src/dsh-inject.d.ts
/**
 * Widened message parameter face: the SDK's `UserMessage` is assignable to
 * this shape, which keeps `Agent`'s method signatures structurally
 * compatible with {@link DshAgentFace} without importing SDK types.
 */
interface DshUserMessageFace {
  readonly id: string;
  readonly role: 'user';
  readonly content: ReadonlyArray<{
    readonly type: string;
  }>;
  readonly source: {
    readonly kind: string;
  };
}
/** Live-agent face: the two injection entry points (runtime-types.d.ts:115/:123). */
interface DshAgentFace {
  followup(message: DshUserMessageFace): void;
  steer(message: DshUserMessageFace): void;
}
/** `AgentHandle` face (index.d.ts:155-158). `dispose` is deliberately absent:
 * the executor never tears down an agent it resumed — disposal would unload
 * the session and cancel the just-queued work. */
interface DshAgentHandleFace {
  readonly agent: DshAgentFace;
}
/** Minimal `ctx.agents` (`AgentRegistry`) face: locate + resume. */
interface AgentsServiceFace {
  /** Live lookup (index.d.ts:349); undefined = session not loaded. */
  get(sessionId: string): DshAgentFace | undefined;
  /** Load a persisted session and start an agent on it (index.d.ts:296). */
  resume(options: {
    readonly resumeSessionId: string;
  }): Promise<DshAgentHandleFace>;
}
//#endregion
//#region src/index.d.ts
declare const name = "agent-sidecar";
/** Required services; see the module doc for why `agents` is lazy instead. */
declare const inject: string[];
/** `ctx.webServer` face (route registration only). */
interface WebServerService {
  register(route: {
    kind: 'exact' | 'prefix';
    path: string;
    handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void>;
  }): () => void;
}
/** Bounded in-memory collection for one child output stream. */
interface SubprocessCollectSpec {
  maxBytes: number;
  spill?: {
    maxBytes: number;
  };
}
/** Fully-specified spawn request (`ctx.subprocess` applies no defaults). */
interface SubprocessSpawnSpec {
  argv: readonly string[];
  cwd: string;
  stdio: {
    stdin: 'ignore' | 'pipe' | {
      readonly data: string;
    };
    stdout: 'pipe' | 'inherit' | SubprocessCollectSpec;
    stderr: 'pipe' | 'inherit' | SubprocessCollectSpec;
  };
  graceMs: number;
  signal?: AbortSignal;
  env?: NodeJS.ProcessEnv;
}
/** Exit facts of one closed process (Node `close`-event vocabulary). */
interface SubprocessOutcome {
  exitCode: number | null;
  signal: NodeJS.Signals | null;
}
/** Offset-based, non-consuming reader over one collect-mode stream. */
interface SubprocessOutputReader {
  readFrom(fromByte: number): {
    text: string;
    nextOffset: number;
    lossy: boolean;
  };
}
/** Live child-process handle rooted in its own process tree. */
interface SubprocessHandle {
  readonly pid: number;
  /** Present iff spawned with `stdin: 'pipe'` (dsh-subprocess types.d.ts:158). */
  readonly stdin: Writable | undefined;
  readonly stdout: Readable | undefined;
  readonly stderr: Readable | undefined;
  readonly collected: {
    readonly stdout?: SubprocessOutputReader;
    readonly stderr?: SubprocessOutputReader;
  };
  readonly done: Promise<SubprocessOutcome>;
  terminate(): void;
  waitForExit(signal?: AbortSignal): Promise<boolean>;
}
/** `ctx.subprocess` face (managed pipe-process primitive only). */
interface SubprocessService {
  spawn(spec: SubprocessSpawnSpec): SubprocessHandle;
}
/** Owner scope returned by `ctx.settings.register` (read/observe subset). */
interface SettingsScopeFace<T> {
  get(): T;
  watch(callback: (next: T, prev: T) => void): () => void;
}
/**
 * `ctx.settings` face (namespace registration only). Source:
 * dsh-settings SettingsProvider.register — `register(ns, schema, {base,
 * applies})` → owner scope; the namespace brand is compile-time only, so a
 * plain string is structurally sound. Registration rides the CALLER's
 * fiber (service proxy binds this.ctx), so disposal is automatic.
 */
interface SettingsServiceFace {
  register<T>(ns: string, schema: unknown, options?: {
    base?: Partial<T>;
    applies?: 'live' | 'restart';
  }): SettingsScopeFace<T>;
}
/**
 * The lazily-bound `ctx.agents` registry surface: M2 injection consumes
 * `get`/`resume` (dsh-inject's {@link AgentsServiceFace}), M3 analysis
 * consumes `create` (analysis.ts's {@link AnalysisAgentFace}). One lazy
 * binding serves both paths — and gates both degradations.
 */
type AgentsRegistryFace = AgentsServiceFace & Pick<AnalysisAgentFace, 'create'>;
/**
 * `ctx.agentDefaultModel` face (dsh-agent-default-model index.d.ts:40-56):
 * the host's default model selection — the SAME source dsh's own entry
 * points read when creating agents (dsh-headless `run()` passes
 * `agentOptions: {provider, model}` from `currentSelection()`;
 * dsh-host-apiproxy exposes it as `defaultModelSelection`). Resolved per
 * call via reflect `get` (never a hard inject): the service is core in
 * dsh-base compositions but the plugin must degrade honestly without it.
 */
interface AgentDefaultModelFace {
  currentSelection(): {
    provider: string;
    model: string;
  };
}
/** The plugin context with the two hard-injected services visible. */
type HostContext = Context & {
  webServer: WebServerService;
  subprocess: SubprocessService;
};
/**
 * Assemble the M1 host half.
 *
 * Teardown is order-sensitive, so the whole assembly lives in ONE
 * `ctx.effect` disposer (design §4.a: "顺序敏感拆除放同一 disposer"):
 * supervisor first (terminates a self-hosted daemon, never an adopted one),
 * then the reconciler (closes the subscribe stream and timers), then
 * `routes.dispose()` (ends SSE clients, unsubscribes), and the webServer
 * route disposer last.
 *
 * @param ctx - plugin context handed by the cordis loader.
 * @param config - schema-validated composition config (defaults filled).
 */
declare function apply(ctx: HostContext, config: Config): void;
//#endregion
export { AgentDefaultModelFace, AgentsRegistryFace, Config, HostContext, SettingsScopeFace, SettingsServiceFace, SubprocessCollectSpec, SubprocessHandle, SubprocessOutcome, SubprocessOutputReader, SubprocessService, SubprocessSpawnSpec, WebServerService, apply, inject, name };