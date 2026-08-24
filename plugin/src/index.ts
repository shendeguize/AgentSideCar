/**
 * Agent Sidecar — dsh host-half plugin entry (M1 + M2 assembly).
 *
 * Wires the pure modules onto the cordis context:
 *   SessionStore → SidecarSocketClient → Reconciler → DaemonSupervisor
 *   → InjectGateway (dsh + send-cli executors) → createRoutes
 *   → ctx.webServer prefix route.
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
 * "agents service unavailable" detail).
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

import { Config } from './config.ts'
import { Reconciler, SidecarSocketClient } from './bridge.ts'
import { createDshInjectExecutor, type AgentsServiceFace } from './dsh-inject.ts'
import type { GuardOptions } from './guard.ts'
import {
  InjectGateway,
  type InjectTarget,
  type TargetStatus,
} from './inject-gateway.ts'
import { API_PREFIX, createRoutes } from './routes.ts'
import { createSendCliExecutor, type SpawnLike } from './send-cli.ts'
import { SessionStore } from './session-store.ts'
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
  // policy=off still reconciles read-only against an externally managed
  // daemon: off means "lifecycle is not ours", not "do not read data".
  const reconciler = new Reconciler(client, store, {
    activeMs: config.stream.reconcileActiveMs,
    idleMs: config.stream.reconcileIdleMs,
  })
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
  let liveAgents: AgentsServiceFace | null = null
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

  const routes = createRoutes({ store, supervisor, guardOptions, injectGateway, log })

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
    reconciler.start()
    supervisor.start()
    return async () => {
      offStateChange()
      await supervisor.stop()
      reconciler.stop()
      routes.dispose()
      removeRoute()
    }
  }, 'agent-sidecar: host assembly (route + reconciler + supervisor)')

  // dsh injection path binding (M2). Lazy inject, same pattern as settings
  // below: `agents` (dsh-agent AgentRegistry) is present in every dsh-base
  // composition, but a top-level hard inject would pend the whole fiber in
  // agent-less compositions (see module doc). The callback rides its own
  // fiber: cordis unloads and re-runs it whenever the service changes, and
  // the effect disposer unbinds so the executor degrades cleanly again.
  // Binding the service reference here (not per call) keeps resume's owner
  // context on this fiber, so a handle resumed for injection is drained by
  // cordis if the plugin unloads.
  ctx.inject(['agents'], (injected) => {
    const actx = injected as HostContext & { agents: AgentsServiceFace }
    liveAgents = actx.agents
    actx.effect(() => () => {
      liveAgents = null
    }, 'agent-sidecar: agents binding release')
    log('debug', 'dsh inject path online (agents service bound)')
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
