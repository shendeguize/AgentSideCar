/**
 * Sidecar daemon lifecycle supervisor — the probe-adopt-else-host state
 * machine of design §4.a (.local/tasks/make_dsh_mode/design/dsh_plugin_design.md).
 *
 * Pure dependency-injected module: imports nothing from cordis/dsh. The
 * plugin entry (index.ts) wires the real seams — Unix-socket `ping`,
 * `ctx.subprocess.spawn` running `<sidecarCommand> daemon run`, read-only
 * LaunchAgent detection via `agent-sidecar service status` — and registers
 * `stop()` inside a `ctx.effect` disposer.
 *
 * Ownership contract: the supervisor only ever terminates processes it
 * spawned itself (HOSTING/HOSTED). Externally managed daemons (ADOPTED,
 * DEFER) are never killed — teardown merely disconnects.
 */

export type SupervisorState =
  | 'probe'
  | 'adopted'
  | 'defer'
  | 'reprobe'
  | 'hosting'
  | 'hosted'
  | 'backoff'
  | 'failed'

export type SupervisorPolicy = 'adopt-or-host' | 'adopt-only' | 'off'

export type LogLevel = 'debug' | 'info' | 'warn' | 'error'

/** Daemon self-description returned by the Unix-socket `ping` op. */
export interface PingInfo {
  pid: number
  version: string
  /** Version in the daemon's own tree; absent when it cannot be read. */
  sourceVersion?: string
  /** The daemon's tree no longer matches the code it loaded. */
  sourceChanged?: boolean
  http: {
    enabled: boolean
    host?: string
    port?: number
  }
}

/** Minimal handle over a spawned `daemon run` foreground child process. */
export interface DaemonProcess {
  /** Settles once the process exits, for any reason. */
  readonly exited: Promise<number | null>
  /** Tree-scoped SIGTERM → grace → SIGKILL; safe to call more than once. */
  terminate(): Promise<void>
}

/** Opaque timer handle so Node globals and injected fake timers interoperate. */
export type TimerHandle = unknown

export interface SupervisorDeps {
  /** One socket ping round-trip; resolves null on any failure (no socket, timeout, bad payload). */
  ping(): Promise<PingInfo | null>
  /** Spawn the daemon as a supervised foreground child. */
  spawnDaemon(): DaemonProcess
  /** Read-only macOS LaunchAgent detection; true means launchd owns the daemon. */
  detectLaunchAgent(): Promise<boolean>
  log(level: LogLevel, msg: string, meta?: Record<string, unknown>): void
  /** Injectable timer pair (fake timers in tests). Defaults to globals. */
  setTimeout?(fn: () => void, ms: number): TimerHandle
  clearTimeout?(handle: TimerHandle): void
}

export interface SupervisorOptions {
  policy: SupervisorPolicy
  /** Consecutive hosting failures that trip FAILED. Default 5. */
  backoffLimit?: number
  /** First backoff delay; doubles on each consecutive failure. Default 1000. */
  backoffBaseMs?: number
  /** Ceiling for the exponential backoff. Default 30000. */
  backoffCapMs?: number
  /** DEFER re-probe cadence. Default 5000. */
  probeIntervalMs?: number
  /** ADOPTED health re-ping cadence. Default 5000. */
  adoptedRepingMs?: number
  /** Consecutive ADOPTED ping misses before REPROBE. Default 3. */
  adoptedFailureLimit?: number
  /**
   * HOSTING readiness window before the spawn counts as failed. Default
   * 45000: a daemon answers nothing until its first index scan ends, and that
   * scan grows with the index — 22s on a 1,950-session machine. A window
   * shorter than the scan executes healthy daemons in a loop.
   */
  hostReadyTimeoutMs?: number
  /** Ping cadence while waiting for a hosted daemon to become ready. Default 500. */
  hostReadyPingIntervalMs?: number
}

export type StateListener = (state: SupervisorState, previous: SupervisorState) => void

/** Why the last hosting attempt failed. */
export type FailureReason =
  /** The child never ran — a missing or unexecutable command lands here. */
  | 'spawn-error'
  /** The child ran and left before answering. */
  | 'daemon-exit'
  /** The child stayed silent past the readiness window. */
  | 'ready-timeout'

/**
 * The cause behind BACKOFF/FAILED, kept so a surface can report it.
 *
 * The supervisor logs every failure, but a remote board reads a socket and an
 * HTTP payload, not the host's log: without this the page can only say the
 * daemon is offline, which is the one thing the reader already knows.
 */
export interface SupervisorFailure {
  readonly reason: FailureReason
  /** Exit code when the child ran and left, else null. */
  readonly exitCode: number | null
  /** Bounded error text (e.g. `spawn agent-sidecar ENOENT`), else null. */
  readonly detail: string | null
}

/** Enough to name a command and its errno; short enough for one banner. */
const FAILURE_DETAIL_MAX = 200

const defaultSetTimeout = (fn: () => void, ms: number): TimerHandle =>
  globalThis.setTimeout(fn, ms)

const defaultClearTimeout = (handle: TimerHandle): void => {
  globalThis.clearTimeout(handle as ReturnType<typeof globalThis.setTimeout>)
}

const describeError = (error: unknown): string =>
  error instanceof Error ? error.message : String(error)

export class DaemonSupervisor {
  private readonly deps: SupervisorDeps
  private readonly opts: Required<SupervisorOptions>

  private _state: SupervisorState = 'probe'
  private _lastPing: PingInfo | null = null
  private _lastFailure: SupervisorFailure | null = null
  private readonly listeners = new Set<StateListener>()
  private readonly timers = new Set<TimerHandle>()
  /** Only ever non-null for a process this supervisor spawned itself. */
  private proc: DaemonProcess | null = null
  /**
   * Invalidation token for async continuations (ping/detect results, process
   * exit watchers): each macro transition bumps it, so continuations started
   * under an older epoch abandon instead of acting on a stale world.
   */
  private epoch = 0
  private started = false
  private stopped = false
  /** Consecutive hosting failures (readiness timeout or early exit). */
  private hostFailures = 0
  /** Consecutive ADOPTED re-ping misses. */
  private pingFailures = 0

  constructor(deps: SupervisorDeps, options: SupervisorOptions) {
    this.deps = deps
    this.opts = {
      policy: options.policy,
      backoffLimit: options.backoffLimit ?? 5,
      backoffBaseMs: options.backoffBaseMs ?? 1000,
      backoffCapMs: options.backoffCapMs ?? 30_000,
      probeIntervalMs: options.probeIntervalMs ?? 5000,
      adoptedRepingMs: options.adoptedRepingMs ?? 5000,
      adoptedFailureLimit: options.adoptedFailureLimit ?? 3,
      hostReadyTimeoutMs: options.hostReadyTimeoutMs ?? 45_000,
      hostReadyPingIntervalMs: options.hostReadyPingIntervalMs ?? 500,
    }
  }

  get state(): SupervisorState {
    return this._state
  }

  /** Last successful ping payload; null until the daemon answered once. */
  get lastPing(): PingInfo | null {
    return this._lastPing
  }

  /** Cause of the last hosting failure; null whenever a daemon is answering. */
  get lastFailure(): SupervisorFailure | null {
    return this._lastFailure
  }

  /** Subscribe to state transitions; returns an unsubscribe function. */
  onStateChange(listener: StateListener): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  start(): void {
    if (this.started || this.stopped) return
    this.started = true
    if (this.opts.policy === 'off') {
      // Terminal quiescence: no probing, no timers, no spawn. DEFER is the
      // closest state in the vocabulary to "lifecycle is not ours to manage".
      this.deps.log('info', 'daemon management policy is off; supervisor standing down')
      this.setState('defer')
      return
    }
    void this.runDetermination('probe')
  }

  /**
   * Tear everything down for the ctx.effect disposer: probe/backoff timers
   * first, then terminate a self-spawned daemon. Adopted or launchd-managed
   * daemons are never touched. Idempotent.
   */
  async stop(): Promise<void> {
    if (this.stopped) return
    this.stopped = true
    this.epoch += 1
    this.clearAllTimers()
    this.listeners.clear()
    const proc = this.proc
    this.proc = null
    if (proc) {
      this.deps.log('info', 'stopping supervisor: terminating self-hosted daemon')
      try {
        await proc.terminate()
      } catch (error) {
        this.deps.log('warn', 'terminate during stop failed', { error: describeError(error) })
      }
    }
  }

  /** From FAILED only: reset the failure budget and re-run the determination. */
  retry(): void {
    if (this.stopped) return
    if (this._state !== 'failed') {
      this.deps.log('debug', 'retry ignored outside FAILED state', { state: this._state })
      return
    }
    this.hostFailures = 0
    this.pingFailures = 0
    this.deps.log('info', 'retry requested: failure budget reset, re-probing')
    void this.runDetermination('probe')
  }

  // ---------------------------------------------------------------- probe

  /** Shared PROBE/REPROBE determination: ping → adopt, else LaunchAgent → defer, else host. */
  private async runDetermination(entry: 'probe' | 'reprobe'): Promise<void> {
    const ep = ++this.epoch
    this.clearAllTimers()
    this.setState(entry)
    const info = await this.safePing()
    if (this.invalidated(ep)) return
    if (info) {
      this.enterAdopted(info)
      return
    }
    let managed = false
    try {
      managed = await this.deps.detectLaunchAgent()
    } catch (error) {
      this.deps.log('warn', 'LaunchAgent detection failed; assuming absent', {
        error: describeError(error),
      })
    }
    if (this.invalidated(ep)) return
    if (managed) {
      this.enterDefer('LaunchAgent installed; launchd owns daemon liveness')
      return
    }
    if (this.opts.policy === 'adopt-only') {
      this.enterDefer('policy adopt-only forbids spawning')
      return
    }
    this.enterHosting()
  }

  // -------------------------------------------------------------- adopted

  private enterAdopted(info: PingInfo): void {
    this.clearAllTimers()
    this._lastPing = info
    this._lastFailure = null
    this.pingFailures = 0
    this.hostFailures = 0
    this.deps.log('info', 'adopted existing daemon', { pid: info.pid, version: info.version })
    this.setState('adopted')
    this.scheduleAdoptedPing()
  }

  private scheduleAdoptedPing(): void {
    this.schedule(this.opts.adoptedRepingMs, () => {
      void this.adoptedPing()
    })
  }

  private async adoptedPing(): Promise<void> {
    const ep = this.epoch
    const info = await this.safePing()
    if (this.invalidated(ep) || this._state !== 'adopted') return
    if (info) {
      this._lastPing = info
      this.pingFailures = 0
      this.scheduleAdoptedPing()
      return
    }
    this.pingFailures += 1
    this.deps.log('warn', 'adopted daemon missed ping', {
      misses: this.pingFailures,
      limit: this.opts.adoptedFailureLimit,
    })
    if (this.pingFailures >= this.opts.adoptedFailureLimit) {
      this.pingFailures = 0
      void this.runDetermination('reprobe')
      return
    }
    this.scheduleAdoptedPing()
  }

  // ---------------------------------------------------------------- defer

  /** External management (launchd, or adopt-only policy): re-probe periodically, never spawn. */
  private enterDefer(reason: string): void {
    this.clearAllTimers()
    this.deps.log('info', 'deferring daemon management', { reason })
    this.setState('defer')
    this.scheduleDeferProbe()
  }

  private scheduleDeferProbe(): void {
    this.schedule(this.opts.probeIntervalMs, () => {
      void this.deferProbe()
    })
  }

  private async deferProbe(): Promise<void> {
    const ep = this.epoch
    const info = await this.safePing()
    if (this.invalidated(ep) || this._state !== 'defer') return
    if (info) {
      this.enterAdopted(info)
      return
    }
    this.scheduleDeferProbe()
  }

  // -------------------------------------------------------------- hosting

  private enterHosting(): void {
    const ep = ++this.epoch
    this.clearAllTimers()
    this.setState('hosting')
    let proc: DaemonProcess
    try {
      proc = this.deps.spawnDaemon()
    } catch (error) {
      this.deps.log('error', 'failed to spawn daemon', { error: describeError(error) })
      this.enterBackoff('spawn-error', null, describeError(error))
      return
    }
    this.proc = proc
    // The watcher survives into HOSTED (same epoch); any later transition
    // bumps the epoch and thereby detaches it.
    proc.exited.then(
      (code) => {
        if (this.invalidated(ep)) return
        this.proc = null
        this.deps.log('warn', 'hosted daemon exited', { code, state: this._state })
        this.enterBackoff('daemon-exit', code)
      },
      (error: unknown) => {
        if (this.invalidated(ep)) return
        this.proc = null
        // The host rejects this promise when the child could not be launched
        // at all, which is a different thing to report than an exit code.
        this.deps.log('warn', 'hosted daemon exit watch failed', { error: describeError(error) })
        this.enterBackoff('spawn-error', null, describeError(error))
      },
    )
    this.schedule(this.opts.hostReadyTimeoutMs, () => {
      if (this.invalidated(ep)) return
      this.deps.log('warn', 'hosted daemon readiness timeout', {
        timeoutMs: this.opts.hostReadyTimeoutMs,
      })
      this.disposeProc()
      this.enterBackoff('ready-timeout', null)
    })
    this.scheduleReadyPoll(ep)
  }

  private scheduleReadyPoll(ep: number): void {
    this.schedule(this.opts.hostReadyPingIntervalMs, () => {
      void this.readyPoll(ep)
    })
  }

  private async readyPoll(ep: number): Promise<void> {
    if (this.invalidated(ep)) return
    const info = await this.safePing()
    if (this.invalidated(ep) || this._state !== 'hosting') return
    if (info) {
      this.enterHosted(info)
      return
    }
    this.scheduleReadyPoll(ep)
  }

  private enterHosted(info: PingInfo): void {
    // Deliberately no epoch bump: the process exit watcher from enterHosting
    // must stay attached so a crash in HOSTED still lands in BACKOFF.
    this.clearAllTimers()
    this._lastPing = info
    this._lastFailure = null
    this.hostFailures = 0
    this.pingFailures = 0
    this.deps.log('info', 'hosted daemon ready', { pid: info.pid, version: info.version })
    this.setState('hosted')
  }

  // -------------------------------------------------------------- backoff

  private enterBackoff(
    reason: FailureReason,
    code: number | null,
    detail: string | null = null,
  ): void {
    this.epoch += 1
    this.clearAllTimers()
    this.hostFailures += 1
    this._lastFailure = {
      reason,
      exitCode: code,
      detail: detail === null ? null : detail.slice(0, FAILURE_DETAIL_MAX),
    }
    this.setState('backoff')
    if (this.hostFailures >= this.opts.backoffLimit) {
      this.deps.log('error', 'hosting failure budget exhausted; giving up', {
        failures: this.hostFailures,
        reason,
      })
      this.setState('failed')
      return
    }
    const delayMs = Math.min(
      this.opts.backoffBaseMs * 2 ** (this.hostFailures - 1),
      this.opts.backoffCapMs,
    )
    this.deps.log('warn', 'hosting failed; backing off', {
      reason,
      code,
      failures: this.hostFailures,
      delayMs,
    })
    // Re-run the full determination after the delay: if a daemon appeared
    // externally in the meantime we adopt it instead of fighting the
    // single-instance lock with another doomed spawn.
    this.schedule(delayMs, () => {
      void this.runDetermination('probe')
    })
  }

  // -------------------------------------------------------------- helpers

  private invalidated(ep: number): boolean {
    return this.stopped || this.epoch !== ep
  }

  private async safePing(): Promise<PingInfo | null> {
    try {
      return await this.deps.ping()
    } catch (error) {
      this.deps.log('debug', 'ping threw; treating as unreachable', {
        error: describeError(error),
      })
      return null
    }
  }

  /** Fire-and-forget terminate of the self-spawned process (readiness timeout path). */
  private disposeProc(): void {
    const proc = this.proc
    this.proc = null
    if (!proc) return
    proc.terminate().catch((error: unknown) => {
      this.deps.log('warn', 'terminate failed', { error: describeError(error) })
    })
  }

  private setState(next: SupervisorState): void {
    if (this._state === next) return
    const previous = this._state
    this._state = next
    this.deps.log('debug', 'supervisor state transition', { from: previous, to: next })
    for (const listener of [...this.listeners]) {
      try {
        listener(next, previous)
      } catch (error) {
        this.deps.log('warn', 'state listener threw', { error: describeError(error) })
      }
    }
  }

  private schedule(ms: number, fn: () => void): TimerHandle {
    const set = this.deps.setTimeout ?? defaultSetTimeout
    let handle: TimerHandle
    handle = set(() => {
      this.timers.delete(handle)
      if (this.stopped) return
      fn()
    }, ms)
    this.timers.add(handle)
    return handle
  }

  private clearAllTimers(): void {
    const clear = this.deps.clearTimeout ?? defaultClearTimeout
    for (const handle of this.timers) clear(handle)
    this.timers.clear()
  }
}
