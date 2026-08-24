import z from "@deepseek-ai/schemastery";
import { IncomingMessage, ServerResponse } from "node:http";
import { Readable, Writable } from "node:stream";
import { Context } from "@deepseek-ai/cordis";

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
/** AI bypass-analysis switch (M3). */
interface AnalysisConfig {
  enabled: boolean;
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
export { Config, HostContext, SettingsScopeFace, SettingsServiceFace, SubprocessCollectSpec, SubprocessHandle, SubprocessOutcome, SubprocessOutputReader, SubprocessService, SubprocessSpawnSpec, WebServerService, apply, inject, name };