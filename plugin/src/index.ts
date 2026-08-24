/**
 * Agent Sidecar — dsh host-half plugin entry (M1 + M2 + M3 assembly).
 *
 * Wires the pure modules onto the cordis context:
 *   SessionStore → SidecarSocketClient → FusionQuery (holder) → Reconciler
 *   → DaemonSupervisor → InjectGateway (dsh + send-cli executors)
 *   → AnalysisEngine (lazy agents.create + fusion input adapter)
 *   → createRoutes → ctx.webServer prefix route.
 *
 * M3 fusion wiring in brief: FusionQuery lives behind a holder facade so
 * the dsh event feed can bind lazily (`ctx.inject(['sessions'])`, swap-in/
 * swap-out); sessionQuery resolves per call via reflect `get`; the daemon
 * `replay` op arrives through a paging adapter over `client.replay`; and
 * the reconciler's store face tees subscribe events into fusion's ring.
 *
 * Named exports only: postmortem 0001 documents that a default-exported
 * plugin object silently drops `inject`, so the loader must see the named
 * `name`/`inject`/`Config`/`apply` faces directly on the module namespace.
 *
 * `inject` declares only the two services every milestone needs (webServer,
 * subprocess). `agents` — the M2 dsh in-process injection path — is
 * consumed through a LAZY `ctx.inject(['agents'], …)` instead: cordis
 * `inject` knows no optional tier (`Inject = (keyof M)[] | map`, all
 * required), so a top-level declaration would pend the whole fiber in any
 * composition without dsh-agent and take the M1 read surface down with it.
 * dsh-base does bundle `@deepseek-ai/dsh-agent`, so in standard
 * compositions the lazy callback fires at boot anyway; in agent-less
 * compositions the plugin still loads and the injection surface degrades
 * to the send-cli path only (a dsh-target execute fails with an honest
 * "agents service unavailable" detail) while the M3 analysis actions
 * answer 501 `analysis_unavailable` (same binding gates both).
 *
 * Service contracts consumed here were verified against the installed dsh
 * 0.1.1-rc.2 type declarations, not docs:
 * - `ctx.webServer.register({kind:'prefix', path, handler})` → disposer;
 *   handler is plain node:http and owns the response lifecycle
 *   (@deepseek-ai/dsh-host-webserver lib/types/index.d.ts).
 * - `ctx.subprocess.spawn(spec)` is fully explicit (argv/cwd/stdio/graceMs/
 *   env, argv never shell-interpreted); the handle exposes `done`,
 *   `stdin` (iff spawned with `stdin: 'pipe'`), tree-scoped `terminate()`
 *   (SIGTERM → graceMs → SIGKILL) and `waitForExit()`
 *   (@deepseek-ai/dsh-subprocess lib/types/types.d.ts).
 * - `ctx.agents` is dsh-agent's AgentRegistry (`get(id)` live lookup,
 *   `resume({resumeSessionId})` → AgentHandle); the structural
 *   {@link AgentsServiceFace} in dsh-inject.ts is satisfied directly.
 * The faces below are structural on purpose: the plugin's type surface
 * stays on the two devDependency SDKs (cordis, schemastery) while the
 * service packages resolve at runtime from the dsh profile tree.
 *
 * @module @shendeguize/dsh-agent-sidecar
 */

import type { IncomingMessage, ServerResponse } from 'node:http'
import { homedir } from 'node:os'
import { isAbsolute, join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import type { Readable, Writable } from 'node:stream'
import type { Context } from '@deepseek-ai/cordis'

import {
  AnalysisEngine,
  type AnalysisAgentFace,
  type AnalysisInput,
  type AnalysisSession,
} from './analysis.ts'
import { Config } from './config.ts'
import { Reconciler, SidecarSocketClient } from './bridge.ts'
import { createDshInjectExecutor, type AgentsServiceFace } from './dsh-inject.ts'
import {
  FusionQuery,
  type DshEventFace,
  type SessionQueryFace,
  type SidecarEventFace,
  type SidecarReplayFace,
  type UnifiedSession,
} from './fusion.ts'
import type { GuardOptions } from './guard.ts'
import {
  InjectGateway,
  type InjectTarget,
  type TargetStatus,
} from './inject-gateway.ts'
import {
  API_PREFIX,
  createRoutes,
  type AnalysisTargetRequest,
  type FusionApi,
} from './routes.ts'
import { createSendCliExecutor, type SpawnLike } from './send-cli.ts'
import { SessionStore } from './session-store.ts'
import {
  registerSidecarSkillProvider,
  type SkillsServiceFace,
} from './skills-provider.ts'
import { DaemonSupervisor, type DaemonProcess, type LogLevel } from './supervisor.ts'

export { Config } from './config.ts'

export const name = 'agent-sidecar'

/** Required services; see the module doc for why `agents` is lazy instead. */
export const inject = ['webServer', 'subprocess']

// ---------------------------------------------------------------------------
// Structural faces of the consumed dsh services (see module doc for sources).
// ---------------------------------------------------------------------------

/** `ctx.webServer` face (route registration only). */
export interface WebServerService {
  register(route: {
    kind: 'exact' | 'prefix'
    path: string
    handler: (req: IncomingMessage, res: ServerResponse) => void | Promise<void>
  }): () => void
}

/** Bounded in-memory collection for one child output stream. */
export interface SubprocessCollectSpec {
  maxBytes: number
  spill?: { maxBytes: number }
}

/** Fully-specified spawn request (`ctx.subprocess` applies no defaults). */
export interface SubprocessSpawnSpec {
  argv: readonly string[]
  cwd: string
  stdio: {
    stdin: 'ignore' | 'pipe' | { readonly data: string }
    stdout: 'pipe' | 'inherit' | SubprocessCollectSpec
    stderr: 'pipe' | 'inherit' | SubprocessCollectSpec
  }
  graceMs: number
  signal?: AbortSignal
  env?: NodeJS.ProcessEnv
}

/** Exit facts of one closed process (Node `close`-event vocabulary). */
export interface SubprocessOutcome {
  exitCode: number | null
  signal: NodeJS.Signals | null
}

/** Offset-based, non-consuming reader over one collect-mode stream. */
export interface SubprocessOutputReader {
  readFrom(fromByte: number): { text: string; nextOffset: number; lossy: boolean }
}

/** Live child-process handle rooted in its own process tree. */
export interface SubprocessHandle {
  readonly pid: number
  /** Present iff spawned with `stdin: 'pipe'` (dsh-subprocess types.d.ts:158). */
  readonly stdin: Writable | undefined
  readonly stdout: Readable | undefined
  readonly stderr: Readable | undefined
  readonly collected: {
    readonly stdout?: SubprocessOutputReader
    readonly stderr?: SubprocessOutputReader
  }
  readonly done: Promise<SubprocessOutcome>
  terminate(): void
  waitForExit(signal?: AbortSignal): Promise<boolean>
}

/** `ctx.subprocess` face (managed pipe-process primitive only). */
export interface SubprocessService {
  spawn(spec: SubprocessSpawnSpec): SubprocessHandle
}

/** Owner scope returned by `ctx.settings.register` (read/observe subset). */
export interface SettingsScopeFace<T> {
  get(): T
  watch(callback: (next: T, prev: T) => void): () => void
}

/**
 * `ctx.settings` face (namespace registration only). Source:
 * dsh-settings SettingsProvider.register — `register(ns, schema, {base,
 * applies})` → owner scope; the namespace brand is compile-time only, so a
 * plain string is structurally sound. Registration rides the CALLER's
 * fiber (service proxy binds this.ctx), so disposal is automatic.
 */
export interface SettingsServiceFace {
  register<T>(
    ns: string,
    schema: unknown,
    options?: { base?: Partial<T>; applies?: 'live' | 'restart' },
  ): SettingsScopeFace<T>
}

/**
 * The lazily-bound `ctx.agents` registry surface: M2 injection consumes
 * `get`/`resume` (dsh-inject's {@link AgentsServiceFace}), M3 analysis
 * consumes `create` (analysis.ts's {@link AnalysisAgentFace}). One lazy
 * binding serves both paths — and gates both degradations.
 */
export type AgentsRegistryFace = AgentsServiceFace & Pick<AnalysisAgentFace, 'create'>

/**
 * `ctx.agentDefaultModel` face (dsh-agent-default-model index.d.ts:40-56):
 * the host's default model selection — the SAME source dsh's own entry
 * points read when creating agents (dsh-headless `run()` passes
 * `agentOptions: {provider, model}` from `currentSelection()`;
 * dsh-host-apiproxy exposes it as `defaultModelSelection`). Resolved per
 * call via reflect `get` (never a hard inject): the service is core in
 * dsh-base compositions but the plugin must degrade honestly without it.
 */
export interface AgentDefaultModelFace {
  currentSelection(): { provider: string; model: string }
}

/** The plugin context with the two hard-injected services visible. */
export type HostContext = Context & {
  webServer: WebServerService
  subprocess: SubprocessService
}

// ---------------------------------------------------------------------------
// Sidecar invocation facts (mirroring sidecar/daemon.py and launchd.py).
// ---------------------------------------------------------------------------

/** `SOCKET_NAME` in sidecar/daemon.py. */
const SOCKET_NAME = 'daemon.sock'
/** `RUNTIME_ENV` / `LEGACY_RUNTIME_ENV` in sidecar/daemon.py. */
const RUNTIME_ENV = 'AGENT_SIDECAR_RUNTIME_DIR'
const LEGACY_RUNTIME_ENV = 'AGENT_SIDECAR_HOME'

/** SIGTERM → grace → SIGKILL window for a hosted daemon (design §4.a: 5s). */
const DAEMON_GRACE_MS = 5000
/** Whole-run bound for one `service status` detection probe. */
const DETECT_TIMEOUT_MS = 10_000
/** SIGTERM → grace → SIGKILL window when the send-cli hard timeout kills. */
const SEND_CLI_GRACE_MS = 2000
/** Output cap for the detection probe (one sanitized message line). */
const DETECT_OUTPUT_BYTES = 4096
/** Per-line clamp when forwarding daemon output into ctx.logger (S8). */
const LOG_LINE_LIMIT = 400
/** Per-page `replay` limit forwarded to the daemon (its own cap is 1024). */
const REPLAY_PAGE_LIMIT = 512
/** Page cap per fusion replay pull: bounds one timeline fan-out to ≤2048 events. */
const REPLAY_MAX_PAGES = 4

// Bounds of the fusion→AnalysisInput adapter (§7-B: the engine re-bounds
// the whole text to maxInputChars anyway; these keep the assembly cheap
// and the head of the text — which survives engine truncation — useful).
/** Timeline entries pulled into one session-analysis summary. */
const ANALYSIS_TIMELINE_LIMIT = 120
/** Sessions listed per project-analysis overview. */
const ANALYSIS_MAX_SESSIONS = 30
/** Project groups listed in a cross-agent analysis overview. */
const ANALYSIS_MAX_GROUPS = 12
/** Sessions listed per group in a cross-agent analysis overview. */
const ANALYSIS_CROSS_SESSIONS = 5
/** Clamp on one line of untrusted text (titles, event text). */
const ANALYSIS_LINE_CLAMP = 200
/** Clamp on the user question (placed at the head, so it survives truncation). */
const ANALYSIS_QUESTION_CLAMP = 2000

/**
 * `service status` messages that mean "a LaunchAgent owns daemon liveness"
 * (sidecar/launchd.py `_status`): exit 0 is `service is running (pid N)`;
 * exit 1 covers `service is loaded but daemon is not running` and
 * `service is degraded; ...` (both installed) as well as
 * `service is unloaded...` (not installed). There is no `--json` face —
 * the single sanitized message line IS the contract.
 */
const SERVICE_PRESENT = /^service is (?:running|loaded|degraded)/m

/**
 * Resolve the effective runtime directory the way sidecar/daemon.py
 * `default_runtime_dir()` does: explicit config wins, then the
 * AGENT_SIDECAR_RUNTIME_DIR / legacy AGENT_SIDECAR_HOME environment of the
 * dsh host process, then `~/.agent_sidecar`.
 */
function resolveRuntimeDir(configured: string, env: NodeJS.ProcessEnv): string {
  const raw =
    configured.trim() !== ''
      ? configured.trim()
      : (env[RUNTIME_ENV] ?? env[LEGACY_RUNTIME_ENV] ?? '').trim()
  if (raw === '') return join(homedir(), '.agent_sidecar')
  const expanded =
    raw === '~' ? homedir() : raw.startsWith('~/') ? join(homedir(), raw.slice(2)) : raw
  return isAbsolute(expanded) ? expanded : resolve(expanded)
}

// ---------------------------------------------------------------------------
// Fusion → AnalysisInput assembly helpers (pure; bounded per the constants).
// ---------------------------------------------------------------------------

/** Flatten and clamp one line of untrusted text for an analysis summary. */
function clampAnalysisText(text: string, max = ANALYSIS_LINE_CLAMP): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length <= max ? flat : `${flat.slice(0, max)}…`
}

/** Same trailing-slash normalization fusion uses for project group keys. */
function normalizeAnalysisProject(project: string): string {
  if (project.length > 1 && project.endsWith('/')) {
    const stripped = project.replace(/\/+$/, '')
    return stripped === '' ? '/' : stripped
  }
  return project
}

/** One unified-session line in a project / cross-agent overview. */
function describeUnifiedSession(session: UnifiedSession): string {
  const title = session.title !== '' ? clampAnalysisText(session.title) : '(untitled)'
  const live = session.live ? '|live' : ''
  const updated = new Date(session.lastActivityAt).toISOString()
  return `- [${session.agent}|${session.status}${live}] ${title} (updated ${updated})`
}

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
export function apply(ctx: HostContext, config: Config): void {
  const runtimeDir = resolveRuntimeDir(config.sidecar.runtimeDir, process.env)
  const socketPath = join(runtimeDir, SOCKET_NAME)
  const command = config.sidecar.command
  /** Explicit redirect only when configured; the ambient env already flows. */
  const childEnv: NodeJS.ProcessEnv | undefined =
    config.sidecar.runtimeDir.trim() !== '' ? { [RUNTIME_ENV]: runtimeDir } : undefined

  const log = (level: LogLevel, msg: string, meta?: object): void => {
    ctx.logger[level](
      meta === undefined ? `agent-sidecar: ${msg}` : `agent-sidecar: ${msg} ${JSON.stringify(meta)}`,
    )
  }

  /** Clamped per-line forwarding of daemon output (design §4.c, S8-safe). */
  const forwardLines = (stream: Readable | undefined, level: 'debug' | 'warn'): void => {
    if (stream === undefined) return
    stream.on('error', () => {})
    const lines = createInterface({ input: stream })
    lines.on('line', (line) => {
      const text = line.length > LOG_LINE_LIMIT ? `${line.slice(0, LOG_LINE_LIMIT)}…` : line
      if (text.trim() !== '') ctx.logger[level](`agent-sidecar daemon: ${text}`)
    })
  }

  /** Spawn `<command> daemon run` as a supervised foreground child. */
  const spawnDaemon = (): DaemonProcess => {
    const handle = ctx.subprocess.spawn({
      argv: [...command, 'daemon', 'run'],
      cwd: homedir(),
      stdio: { stdin: 'ignore', stdout: 'pipe', stderr: 'pipe' },
      graceMs: DAEMON_GRACE_MS,
      env: childEnv,
    })
    forwardLines(handle.stdout, 'debug')
    forwardLines(handle.stderr, 'warn')
    return {
      exited: handle.done.then((outcome) => outcome.exitCode),
      // Gentle by construction: subprocess `terminate()` is the tree-scoped
      // SIGTERM → graceMs → SIGKILL escalation; waitForExit observes the
      // whole tree so teardown returns on real quiescence.
      terminate: async () => {
        handle.terminate()
        await handle.waitForExit()
      },
    }
  }

  /**
   * Read-only LaunchAgent detection: darwin-only, one bounded
   * `service status` run, parsed per {@link SERVICE_PRESENT}. Any failure
   * (non-zero control exit, timeout, unspawnable CLI) reads as "absent" —
   * the supervisor already treats detection errors that way.
   */
  const detectLaunchAgent = async (): Promise<boolean> => {
    if (process.platform !== 'darwin') return false
    const handle = ctx.subprocess.spawn({
      argv: [...command, 'service', 'status'],
      cwd: homedir(),
      stdio: {
        stdin: 'ignore',
        stdout: { maxBytes: DETECT_OUTPUT_BYTES },
        stderr: { maxBytes: DETECT_OUTPUT_BYTES },
      },
      graceMs: 2000,
      signal: AbortSignal.timeout(DETECT_TIMEOUT_MS),
      env: childEnv,
    })
    const outcome = await handle.done
    if (outcome.exitCode === 0) return true
    if (outcome.exitCode !== 1) return false
    const text = handle.collected.stdout?.readFrom(0).text ?? ''
    return SERVICE_PRESENT.test(text)
  }

  // ------------------------------------------------------------- assembly

  const store = new SessionStore()
  const client = new SidecarSocketClient({ socketPath })

  // ------------------------------------------------------ M3 fusion assembly

  // SidecarReplayFace → bridge.replay: one fusion pull pages the daemon op
  // until the history is exhausted, bounded to REPLAY_MAX_PAGES so a huge
  // transcript can never wedge one HTTP request. Coded daemon errors
  // (unknown_session / replay_unsupported / ...) propagate as rejections;
  // fusion degrades that source and reports it via `sources` (design §4.e).
  const replayFace: SidecarReplayFace = {
    replay: async ({ sessionId, afterSeq }) => {
      const events: SidecarEventFace[] = []
      let cursor = afterSeq ?? 0
      for (let page = 0; page < REPLAY_MAX_PAGES; page += 1) {
        const result = await client.replay(sessionId, cursor, REPLAY_PAGE_LIMIT)
        events.push(...result.events)
        if (!result.truncated || result.lastSeq === null || result.lastSeq <= cursor) break
        cursor = result.lastSeq
      }
      return events
    },
  }

  // SessionQueryFace → ctx.sessionQuery, re-resolved on EVERY use through
  // the reflect `get` (never a hard inject): dsh-session-query may mount
  // late or never, and fusion degrades per call instead of pending.
  const getSessionQuery = (): SessionQueryFace | null => {
    const getter = (ctx as { get?: (name: string) => unknown }).get
    if (typeof getter !== 'function') return null
    const engine = getter.call(ctx, 'sessionQuery')
    return engine === undefined || engine === null ? null : (engine as SessionQueryFace)
  }

  const buildFusion = (dshEvents: DshEventFace | null): FusionQuery =>
    new FusionQuery({
      store,
      dshEvents,
      getSessionQuery,
      replay: replayFace,
    })

  // DshEventFace is bound through the lazy `sessions` inject below, but
  // fusion must exist NOW (routes capture it). Holder pattern: construct
  // sidecar-only first, swap in a feed-backed instance when dsh-session
  // binds, swap back on release — so `getCapabilities().dshEvents` always
  // reports the truth instead of a permanently-optimistic facade. The swap
  // drops the old instance's bounded event rings; replay and the live
  // stream repopulate them, so no timeline data is lost, only hints.
  const fusionHolder = { current: buildFusion(null) }
  const fusion: FusionApi = {
    getUnifiedSessions: () => fusionHolder.current.getUnifiedSessions(),
    getSessionTimeline: (sessionId, opts) =>
      fusionHolder.current.getSessionTimeline(sessionId, opts),
    getProjectGroups: (opts) => fusionHolder.current.getProjectGroups(opts),
    getLineage: (sessionId) => fusionHolder.current.getLineage(sessionId),
    searchSessions: (query, opts) => fusionHolder.current.searchSessions(query, opts),
    getCapabilities: () => fusionHolder.current.getCapabilities(),
  }

  // policy=off still reconciles read-only against an externally managed
  // daemon: off means "lifecycle is not ours", not "do not read data".
  // The store face tees each subscribe-stream event into fusion's bounded
  // ring (timeline hints) on its way into the session cache.
  const reconciler = new Reconciler(
    client,
    {
      applySnapshot: (rows) => {
        store.applySnapshot(rows)
      },
      applyEvent: (ev) => {
        store.applyEvent(ev)
        fusionHolder.current.ingestSidecarEvent(ev)
      },
      setStreamHealth: (health) => {
        store.setStreamHealth(health)
      },
      hasWorkingSessions: () => store.hasWorkingSessions(),
    },
    {
      activeMs: config.stream.reconcileActiveMs,
      idleMs: config.stream.reconcileIdleMs,
    },
  )
  const supervisor = new DaemonSupervisor(
    { ping: () => client.ping(), spawnDaemon, detectLaunchAgent, log },
    { policy: config.daemon.policy, backoffLimit: config.daemon.backoffLimit },
  )
  // `effective` tracks the settings-resolved config once the settings
  // namespace registers below; until then (and in compositions without
  // dsh-settings) it IS the entry config.
  let effective: Config = config
  const guardOptions: GuardOptions = {
    allowWriteActions: () => effective.inject.enabled,
  }

  // ------------------------------------------------- M2 injection assembly

  // dsh in-process path (§4.d path one). `liveAgents` is bound by the lazy
  // agents inject below; until then (and in compositions without dsh-agent)
  // the face reports the path unavailable: `get` misses, `resume` rejects,
  // and the executor surfaces an honest failure while send-cli keeps working.
  // The same binding carries `create` for the M3 analysis engine.
  let liveAgents: AgentsRegistryFace | null = null
  const agentsFace: AgentsServiceFace = {
    get: (sessionId) => liveAgents?.get(sessionId),
    resume: (options) =>
      liveAgents === null
        ? Promise.reject(
            new Error('dsh agents service is not available in this composition'),
          )
        : liveAgents.resume(options),
  }
  const dshExecutor = createDshInjectExecutor({
    agents: agentsFace,
    log,
    pluginName: name,
  })

  // send-cli path (§4.d path two): adapt `ctx.subprocess.spawn` onto the
  // executor's SpawnLike seam. stdin is a real pipe (the message travels
  // via `--message-stdin`, never argv); the runtimeDir redirect flows via
  // the same childEnv the daemon paths use, so send talks to the same
  // daemon. `done` rejects only on spawn-level failures — exactly the
  // `exited` contract — and `kill()` maps to the tree-scoped terminate.
  const spawnSendCli: SpawnLike = (argv) => {
    const handle = ctx.subprocess.spawn({
      argv,
      cwd: homedir(),
      stdio: { stdin: 'pipe', stdout: 'pipe', stderr: 'pipe' },
      graceMs: SEND_CLI_GRACE_MS,
      env: childEnv,
    })
    // Dead-pipe writes must not throw asynchronously (adapter obligation);
    // the authoritative failure surfaces through `exited`.
    handle.stdin?.on('error', () => {})
    return {
      stdin: {
        write: (chunk) => {
          handle.stdin?.write(chunk)
        },
        end: () => {
          handle.stdin?.end()
        },
      },
      onStdout: (listener) => {
        handle.stdout?.on('data', listener)
      },
      onStderr: (listener) => {
        handle.stderr?.on('data', listener)
      },
      exited: handle.done.then((outcome) => outcome.exitCode),
      kill: () => {
        handle.terminate()
      },
    }
  }
  const sendCliExecutor = createSendCliExecutor({
    spawn: spawnSendCli,
    log,
    opts: { command },
  })

  /** Live target re-check against the reconciled store (§4.f.5 prepare). */
  const verifyTarget = async (target: InjectTarget): Promise<TargetStatus | null> => {
    const view = store
      .getBoardState()
      .sessions.find(
        (s) => s.agent === target.agent && s.session_id === target.sessionId,
      )
    if (view === undefined) return null
    return {
      agent: view.agent,
      sessionId: view.session_id,
      status: view.status,
      title: view.title,
      project: view.project,
    }
  }

  // Constructed even when inject.enabled=false: `allowWrite` reads the live
  // `effective` value on every prepare, and the route layer's
  // guardWriteAction blocks first anyway (two independent gates, no
  // duplication). Gateway audit entries are body-free by construction
  // (byte size + sha256 prefix only), so forwarding them whole is S8-safe.
  const injectGateway = new InjectGateway({
    executors: { dsh: dshExecutor, sendCli: sendCliExecutor },
    verifyTarget,
    allowWrite: () => effective.inject.enabled,
    log: (entry) => log(entry.ok ? 'info' : 'warn', `inject ${entry.phase}`, entry),
  })

  // -------------------------------------------------- M3 analysis assembly

  // In-flight analysis sessions created through this assembly, tracked at
  // the wiring layer (the engine keeps its bookkeeping private): every
  // engine cleanup path goes through `handle.dispose()`, which unregisters
  // here, so whatever is left when the plugin unloads is exactly the set
  // the effect disposer must cancel (design: 在途分析会话随 dispose 清理).
  const liveAnalysisSessions = new Set<AnalysisSession>()

  /**
   * Resolve the provider/model the analysis agent runs on (A-1 fix: an
   * agent created without agentOptions has no model — `{{model}}` prompt
   * assembly and `buildRequest` both fail, yielding an empty summary).
   * Explicit `analysis.provider`+`analysis.model` config wins (both
   * non-empty, read live); otherwise the host's default model selection is
   * reused via `ctx.agentDefaultModel` — the same source dsh's own entry
   * points (headless/apiproxy) read. `null` = no model anywhere: routes
   * pre-reject `analysis.request` as `analysis_model_unconfigured`.
   */
  const resolveAnalysisModel = (): { provider: string; model: string } | null => {
    const provider = effective.analysis.provider.trim()
    const model = effective.analysis.model.trim()
    if (provider !== '' && model !== '') return { provider, model }
    const getter = (ctx as { get?: (name: string) => unknown }).get
    if (typeof getter !== 'function') return null
    const service = getter.call(ctx, 'agentDefaultModel') as
      | AgentDefaultModelFace
      | undefined
      | null
    if (service === undefined || service === null) return null
    try {
      const selection = service.currentSelection()
      if (
        typeof selection?.provider === 'string' &&
        selection.provider !== '' &&
        typeof selection.model === 'string' &&
        selection.model !== ''
      ) {
        return { provider: selection.provider, model: selection.model }
      }
    } catch {
      // A throwing selection reads as "no default available" — the routes'
      // pre-check turns that into an honest analysis_model_unconfigured.
    }
    return null
  }

  const createAnalysisAgent: AnalysisAgentFace['create'] = async (options) => {
    const agents = liveAgents
    if (agents === null) {
      // Raced past the routes' availability probe: surfaces as an honest
      // create_failed result, never a crash.
      throw new Error('dsh agents service is not available in this composition')
    }
    const selection = resolveAnalysisModel()
    if (selection === null) {
      // Raced past the routes' model pre-check (config/settings flipped
      // mid-flight): surfaces as an honest create_failed, never an agent
      // that assembles `{{model}}`-less prompts into empty summaries (A-1).
      throw new Error(
        'no analysis model available: set analysis.provider/analysis.model or mount agentDefaultModel',
      )
    }
    const handle = await agents.create({
      ...options,
      agentOptions: { provider: selection.provider, model: selection.model },
      // The deployment persona's `{{cwd}}` variable reads session.header.cwd,
      // which only meta.cwd populates — same as dsh-headless's own create
      // call (A-1: without it prompt assembly errors and the summary is '').
      meta: { cwd: process.cwd() },
    })
    const tracked: AnalysisSession = {
      agent: handle.agent,
      dispose: async () => {
        liveAnalysisSessions.delete(tracked)
        await handle.dispose()
      },
    }
    liveAnalysisSessions.add(tracked)
    return tracked
  }

  // Engine log entries are body-free by the engine's own contract (S8):
  // kind/title/ids/outcomes/sizes only — safe to forward whole.
  const analysisEngine = new AnalysisEngine({
    createAgent: createAnalysisAgent,
    allowAnalysis: () => effective.analysis.enabled,
    log: (entry) =>
      log(entry.errorCode !== undefined ? 'warn' : 'info', `analysis ${entry.op}`, entry),
  })

  /**
   * Assemble the bounded AnalysisInput for one target from fusion data
   * (design §4.e.3: summaries come from the fused timelines/overviews).
   * `null` = target unknown to fusion → the routes answer 404. The user
   * question rides the HEAD of the text so it survives the engine's
   * tail truncation, and the session timeline lists NEWEST events first
   * for the same reason: when the engine's head-keep truncation bites,
   * it should shed the oldest — least informative — events (F5).
   */
  const buildAnalysisInput = async (
    req: AnalysisTargetRequest,
  ): Promise<AnalysisInput | null> => {
    const questionLines =
      req.question !== undefined && req.question.trim() !== ''
        ? [
            '[用户问题 / question]',
            clampAnalysisText(req.question, ANALYSIS_QUESTION_CLAMP),
            '',
          ]
        : []

    if (req.targetKind === 'session') {
      const targetId = req.targetId ?? ''
      const session =
        fusion.getUnifiedSessions().find((s) => s.sessionId === targetId) ?? null
      if (session === null) return null
      const page = await fusion.getSessionTimeline(targetId, {
        limit: ANALYSIS_TIMELINE_LIMIT,
      })
      const sources = page.sources
      const summaryText = [
        ...questionLines,
        `[会话概览 / session] agent=${session.agent} status=${session.status} live=${session.live}`,
        `title: ${session.title !== '' ? clampAnalysisText(session.title) : '(untitled)'}`,
        `project: ${session.project}`,
        `last activity: ${new Date(session.lastActivityAt).toISOString()}`,
        '',
        `[时间线 / timeline,最新在前 / newest first] ${page.entries.length} events (sources: dshLive=${sources.dshLive} dshCold=${sources.dshCold} replay=${sources.sidecarReplay} buffer=${sources.sidecarBuffer})`,
        ...[...page.entries].reverse().map(
          (entry) =>
            `- [${new Date(entry.ts).toISOString()}] ${entry.kind}` +
            `${entry.seq !== null ? ` seq=${entry.seq}` : ''}` +
            `${entry.text !== '' ? ` ${clampAnalysisText(entry.text)}` : ''}`,
        ),
      ].join('\n')
      return {
        kind: 'session',
        title:
          session.title !== '' ? session.title : `${session.agent} ${session.sessionId}`,
        summaryText,
        meta: { targetId, agent: session.agent },
      }
    }

    if (req.targetKind === 'project') {
      const wanted = normalizeAnalysisProject(req.targetId ?? '')
      const group =
        fusion
          .getProjectGroups()
          .find((g) => normalizeAnalysisProject(g.project) === wanted) ?? null
      if (group === null) return null
      const omitted = group.sessions.length - ANALYSIS_MAX_SESSIONS
      const summaryText = [
        ...questionLines,
        `[项目概览 / project] ${group.project}`,
        `agents: ${group.agents.join(', ')} | sessions: ${group.sessions.length} | last activity: ${new Date(group.lastActivityAt).toISOString()}`,
        '',
        ...group.sessions.slice(0, ANALYSIS_MAX_SESSIONS).map(describeUnifiedSession),
        ...(omitted > 0 ? [`… ${omitted} more sessions omitted`] : []),
      ].join('\n')
      return {
        kind: 'project',
        title: `project ${group.project}`,
        summaryText,
        meta: { targetId: group.project },
      }
    }

    // cross-agent: whole-board overview, always resolvable (possibly empty).
    const groups = fusion.getProjectGroups()
    const sessionsTotal = groups.reduce((n, g) => n + g.sessions.length, 0)
    const omittedGroups = groups.length - ANALYSIS_MAX_GROUPS
    const summaryText = [
      ...questionLines,
      `[跨 agent 概览 / cross-agent overview] ${groups.length} projects, ${sessionsTotal} sessions in the correlation window`,
      '',
      ...groups.slice(0, ANALYSIS_MAX_GROUPS).flatMap((group) => [
        `[${group.project}] agents: ${group.agents.join(', ')} | sessions: ${group.sessions.length}`,
        ...group.sessions.slice(0, ANALYSIS_CROSS_SESSIONS).map(describeUnifiedSession),
        '',
      ]),
      ...(omittedGroups > 0 ? [`… ${omittedGroups} more projects omitted`] : []),
    ].join('\n')
    return { kind: 'cross-agent', title: 'cross-agent overview', summaryText }
  }

  const routes = createRoutes({
    store,
    supervisor,
    guardOptions,
    injectGateway,
    fusion,
    // The analysis write gate reads the LIVE setting, same posture as the
    // inject gate; the engine's allowAnalysis reads the same value (two
    // independent gates, no duplication).
    analysisEnabled: () => effective.analysis.enabled,
    analysis: {
      engine: analysisEngine,
      buildInput: buildAnalysisInput,
      available: () => liveAgents !== null,
      modelConfigured: () => resolveAnalysisModel() !== null,
    },
    log,
  })

  ctx.effect(() => {
    const removeRoute = ctx.webServer.register({
      kind: 'prefix',
      path: API_PREFIX,
      handler: routes.handle,
    })
    // Cold-start latency (M1 acceptance ②): the moment the supervisor
    // confirms a reachable daemon — ADOPTED and HOSTED are both entered off
    // a successful ping, so the socket exists — reconcile immediately
    // instead of waiting out whatever poll the reconciler has pending.
    const offStateChange = supervisor.onStateChange((state) => {
      if (state === 'adopted' || state === 'hosted') void reconciler.reconcileNow()
    })
    fusionHolder.current.start()
    reconciler.start()
    supervisor.start()
    return async () => {
      offStateChange()
      await supervisor.stop()
      reconciler.stop()
      // In-flight analysis sessions die with the plugin: dispose() stops
      // the agent loop and removes the session (the UI "stop" semantics),
      // bounding token burn across plugin reloads (§7-B / design risk 12).
      await Promise.all(
        [...liveAnalysisSessions].map((handle) => handle.dispose().catch(() => {})),
      )
      routes.dispose()
      removeRoute()
      // Whichever instance the holder points at by now (idempotent stop;
      // a feed-bound instance is also stopped by its own inject release).
      fusionHolder.current.stop()
    }
  }, 'agent-sidecar: host assembly (route + reconciler + supervisor + fusion + analysis)')

  // Fusion dsh event feed binding (M3). Lazy inject on `sessions`
  // (dsh-session's service key; the feed itself is the cordis event bus —
  // `ctx.on('session/event' | 'session/created' | 'session/disposed')` —
  // but only meaningful while the service is mounted, and gating on it
  // keeps `getCapabilities().dshEvents.available` honest). Compositions
  // without dsh-session simply never run this: fusion stays sidecar-only
  // and every route keeps working (degradation, not failure).
  ctx.inject(['sessions'], (injected) => {
    const sctx = injected as HostContext
    // 'session/*' keys live in dsh-session's Events augmentation, which
    // this package deliberately does not import (structural-faces rule);
    // the cast keeps the listener registration honest at runtime.
    const bus = sctx as unknown as {
      on(event: string, handler: (...args: never[]) => void): () => void
    }
    const feed: DshEventFace = {
      on: (event: string, handler: (...args: never[]) => void) => bus.on(event, handler),
    }
    const withFeed = buildFusion(feed)
    withFeed.start()
    const previous = fusionHolder.current
    fusionHolder.current = withFeed
    previous.stop()
    sctx.effect(() => () => {
      // Service departing: swap back to a sidecar-only fusion so queries
      // keep answering (and capabilities report the feed as gone).
      const downgraded = buildFusion(null)
      downgraded.start()
      fusionHolder.current = downgraded
      withFeed.stop()
    }, 'agent-sidecar: fusion dsh feed release')
    log('debug', 'fusion dsh event feed online (sessions service bound)')
  })

  // dsh injection + analysis path binding (M2/M3). Lazy inject, same
  // pattern as settings below: `agents` (dsh-agent AgentRegistry) is
  // present in every dsh-base composition, but a top-level hard inject
  // would pend the whole fiber in agent-less compositions (see module
  // doc). The callback rides its own fiber: cordis unloads and re-runs it
  // whenever the service changes, and the effect disposer unbinds so the
  // executor AND the analysis engine degrade cleanly again. Binding the
  // service reference here (not per call) keeps resume/create's owner
  // context on this fiber, so handles created for injection or analysis
  // are drained by cordis if the plugin unloads.
  ctx.inject(['agents'], (injected) => {
    const actx = injected as HostContext & { agents: AgentsRegistryFace }
    liveAgents = actx.agents
    actx.effect(() => () => {
      liveAgents = null
    }, 'agent-sidecar: agents binding release')
    log('debug', 'dsh inject + analysis paths online (agents service bound)')
  })

  // Skill path two (T6.2, design §7): register the embedded agent-sidecar
  // skill provider. Lazy inject on `skills` (dsh-skill's service key):
  // compositions without dsh-skill simply never run this — silent,
  // capability-honest skip. Registration rides this callback's fiber, so
  // cordis unregisters the provider on plugin unload / service departure
  // (dsh-skill d.ts:243-244 "Fiber disposal unregisters the provider and
  // invalidates catalog caches"); no manual effect wrapping, same posture
  // as settings.register below. The gate reads the APPLY-TIME config value
  // on purpose (restart semantics, documented in the schema description):
  // a live settings flip cannot re-run this callback anyway.
  ctx.inject(['skills'], (injected) => {
    const sctx = injected as HostContext & { skills: SkillsServiceFace }
    registerSidecarSkillProvider({
      skills: sctx.skills,
      provide: config.skill.provide,
      log,
    })
  })

  // Settings namespace 'agent-sidecar' (T2.4): pairs the browser settings
  // card (keyed `settings.plugin.item` slot) and persists user edits into
  // dsh's settings document. Lazy inject: compositions without dsh-settings
  // simply never run this, and nothing else depends on it.
  // `applies` (installed dsh-settings 0.1.1-rc.2, verified at source) is
  // namespace-level UI-badge metadata surfaced through `describe()` only —
  // commit() swaps the resolved value and notifies watchers regardless, so
  // the `scope.watch → effective` chain below always takes effect
  // immediately. `applies: 'live'` ('live' | 'restart' are the only
  // values) is the honest badge for the security-relevant `inject.enabled`
  // gate, which IS read live on every prepare/execute (M2 review F-1): a
  // 'restart' badge would tell the user a gate they just closed is still
  // open. Trade-off, documented: daemon.*/stream.*/sidecar values are
  // baked into this assembly at apply time, so their edits still need a
  // plugin reload despite the badge — a UX understatement, versus a badge
  // that misstates a kill switch.
  ctx.inject(['settings'], (injected) => {
    try {
      const sctx = injected as HostContext & { settings: SettingsServiceFace }
      const scope = sctx.settings.register<Config>(name, Config, {
        base: config,
        applies: 'live',
      })
      effective = scope.get()
      const unwatch = scope.watch((next) => {
        effective = next
      })
      sctx.effect(() => () => {
        unwatch()
        effective = config
      }, 'agent-sidecar: settings scope release')
      log('debug', 'settings namespace registered', { applies: 'live' })
    } catch (err) {
      log('warn', `settings namespace registration failed: ${String(err)}`)
    }
  })

  // Single startup line; the stable "host half assembled" marker is what the
  // S0 triple evidence chain greps for (info does not reach the terminal in
  // dsh 0.1.1-rc.2 web — s0_smoke.md F-4 — but the in-process log face and
  // boot-completion probe both consume it).
  ctx.logger.info(
    `agent-sidecar: host half assembled (policy=${config.daemon.policy}, socket=${socketPath}, route=${API_PREFIX})`,
  )
}
