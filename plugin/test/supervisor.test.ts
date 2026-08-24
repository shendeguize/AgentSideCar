/**
 * Fake-timer coverage of the probe-adopt-else-host supervisor state machine
 * (src/supervisor.ts, design §4.a). All timing uses the module defaults:
 * hostReadyTimeoutMs 5000, hostReadyPingIntervalMs 500, probeIntervalMs 5000,
 * adoptedRepingMs 5000, adoptedFailureLimit 3, backoff 1s→2s→4s→8s, limit 5.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  DaemonSupervisor,
  type DaemonProcess,
  type PingInfo,
  type SupervisorDeps,
  type SupervisorOptions,
  type SupervisorState,
} from '../src/supervisor'

const PING: PingInfo = { pid: 4242, version: '9.9.9', http: { enabled: false } }

/** Flush pending microtasks under fake timers without advancing the clock. */
const flush = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(0)
}

interface FakeProc {
  handle: DaemonProcess
  terminate: ReturnType<typeof vi.fn>
  exit: (code: number | null) => void
}

function createFakeProc(): FakeProc {
  let resolveExit: (code: number | null) => void = () => {}
  const exited = new Promise<number | null>((resolve) => {
    resolveExit = resolve
  })
  const terminate = vi.fn(async () => {})
  return {
    handle: { exited, terminate },
    terminate,
    exit: (code) => resolveExit(code),
  }
}

function createHarness(options: Partial<SupervisorOptions> = {}) {
  let pingResult: PingInfo | null = null
  const procs: FakeProc[] = []
  const deps = {
    ping: vi.fn(async (): Promise<PingInfo | null> => pingResult),
    spawnDaemon: vi.fn((): DaemonProcess => {
      const proc = createFakeProc()
      procs.push(proc)
      return proc.handle
    }),
    detectLaunchAgent: vi.fn(async () => false),
    log: vi.fn(),
  }
  const sup = new DaemonSupervisor(deps, { policy: 'adopt-or-host', ...options })
  const states: SupervisorState[] = []
  sup.onStateChange((state) => states.push(state))
  return {
    sup,
    deps,
    procs,
    states,
    setPing: (result: PingInfo | null) => {
      pingResult = result
    },
  }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('DaemonSupervisor', () => {
  it('adopts an already-running daemon (probe → adopted)', async () => {
    const h = createHarness()
    h.setPing(PING)
    h.sup.start()
    expect(h.sup.state).toBe('probe')
    await flush()
    expect(h.sup.state).toBe('adopted')
    expect(h.sup.lastPing).toEqual(PING)
    expect(h.deps.spawnDaemon).not.toHaveBeenCalled()
    expect(h.deps.detectLaunchAgent).not.toHaveBeenCalled()
    expect(h.states).toEqual(['adopted'])
  })

  it('defers to an installed LaunchAgent, re-probes periodically, never spawns', async () => {
    const h = createHarness()
    h.deps.detectLaunchAgent.mockResolvedValue(true)
    h.sup.start()
    await flush()
    expect(h.sup.state).toBe('defer')

    // Several re-probe cycles while the daemon stays down: still DEFER.
    await vi.advanceTimersByTimeAsync(15_000)
    expect(h.sup.state).toBe('defer')
    expect(h.deps.spawnDaemon).not.toHaveBeenCalled()
    expect(h.deps.ping.mock.calls.length).toBeGreaterThanOrEqual(4)

    // launchd finally brings the daemon up: DEFER → ADOPTED.
    h.setPing(PING)
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.sup.state).toBe('adopted')
    expect(h.deps.spawnDaemon).not.toHaveBeenCalled()
  })

  it('hosts when nothing else manages the daemon (probe → hosting → hosted)', async () => {
    const h = createHarness()
    h.sup.start()
    await flush()
    expect(h.sup.state).toBe('hosting')
    expect(h.deps.spawnDaemon).toHaveBeenCalledTimes(1)

    h.setPing(PING)
    await vi.advanceTimersByTimeAsync(500)
    expect(h.sup.state).toBe('hosted')
    expect(h.sup.lastPing).toEqual(PING)

    // The readiness deadline must be defused after HOSTED.
    await vi.advanceTimersByTimeAsync(60_000)
    expect(h.sup.state).toBe('hosted')
    expect(h.procs[0]!.terminate).not.toHaveBeenCalled()
    expect(h.states).toEqual(['hosting', 'hosted'])
  })

  it('readiness timeout → exponential backoff sequence (1s→2s→4s→8s) → failed', async () => {
    const h = createHarness()
    h.sup.start()
    await flush()
    expect(h.sup.state).toBe('hosting')

    // Failure 1: readiness window elapses; the stuck spawn is terminated.
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.sup.state).toBe('backoff')
    expect(h.procs[0]!.terminate).toHaveBeenCalledTimes(1)

    // Failures 2-4: doubling delays gate each respawn exactly.
    let spawns = 1
    for (const delayMs of [1000, 2000, 4000]) {
      await vi.advanceTimersByTimeAsync(delayMs - 1)
      expect(h.deps.spawnDaemon).toHaveBeenCalledTimes(spawns)
      await vi.advanceTimersByTimeAsync(1)
      spawns += 1
      expect(h.deps.spawnDaemon).toHaveBeenCalledTimes(spawns)
      expect(h.sup.state).toBe('hosting')
      await vi.advanceTimersByTimeAsync(5000)
      expect(h.sup.state).toBe('backoff')
    }

    // Failure 5 exhausts the budget: BACKOFF → FAILED, nothing left ticking.
    await vi.advanceTimersByTimeAsync(8000)
    expect(h.deps.spawnDaemon).toHaveBeenCalledTimes(5)
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.sup.state).toBe('failed')
    expect(vi.getTimerCount()).toBe(0)
    for (const proc of h.procs) expect(proc.terminate).toHaveBeenCalledTimes(1)
    expect(h.states.filter((s) => s === 'backoff')).toHaveLength(5)
  })

  it('spawned process exiting during hosting → backoff without terminate', async () => {
    const h = createHarness()
    h.sup.start()
    await flush()
    expect(h.sup.state).toBe('hosting')

    h.procs[0]!.exit(1)
    await flush()
    expect(h.sup.state).toBe('backoff')
    expect(h.procs[0]!.terminate).not.toHaveBeenCalled()
  })

  it('hosted daemon crash → backoff with a fresh failure budget → re-host', async () => {
    const h = createHarness()
    h.sup.start()
    await flush()
    h.setPing(PING)
    await vi.advanceTimersByTimeAsync(500)
    expect(h.sup.state).toBe('hosted')

    h.setPing(null)
    h.procs[0]!.exit(1)
    await flush()
    expect(h.sup.state).toBe('backoff')

    // First-failure delay (1s) proves the budget was reset by HOSTED.
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.sup.state).toBe('hosting')
    expect(h.deps.spawnDaemon).toHaveBeenCalledTimes(2)
  })

  it('adopted daemon going silent → reprobe → hosting', async () => {
    const h = createHarness()
    h.setPing(PING)
    h.sup.start()
    await flush()
    expect(h.sup.state).toBe('adopted')

    h.setPing(null)
    await vi.advanceTimersByTimeAsync(5000) // miss 1
    expect(h.sup.state).toBe('adopted')
    await vi.advanceTimersByTimeAsync(5000) // miss 2
    expect(h.sup.state).toBe('adopted')
    await vi.advanceTimersByTimeAsync(5000) // miss 3 → REPROBE → determination
    expect(h.states).toContain('reprobe')
    expect(h.sup.state).toBe('hosting')
    expect(h.deps.spawnDaemon).toHaveBeenCalledTimes(1)
  })

  it('adopt-only policy never spawns, even after losing an adopted daemon', async () => {
    const h = createHarness({ policy: 'adopt-only' })
    h.sup.start()
    await flush()
    expect(h.sup.state).toBe('defer')

    await vi.advanceTimersByTimeAsync(15_000)
    expect(h.deps.spawnDaemon).not.toHaveBeenCalled()

    h.setPing(PING)
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.sup.state).toBe('adopted')

    h.setPing(null)
    await vi.advanceTimersByTimeAsync(15_000) // 3 misses → reprobe → defer again
    expect(h.states).toContain('reprobe')
    expect(h.sup.state).toBe('defer')
    expect(h.deps.spawnDaemon).not.toHaveBeenCalled()
  })

  it('policy off: start() reaches a terminal quiescent state without connecting', async () => {
    const h = createHarness({ policy: 'off' })
    h.sup.start()
    expect(h.sup.state).toBe('defer')
    expect(vi.getTimerCount()).toBe(0)

    await vi.advanceTimersByTimeAsync(120_000)
    expect(h.deps.ping).not.toHaveBeenCalled()
    expect(h.deps.detectLaunchAgent).not.toHaveBeenCalled()
    expect(h.deps.spawnDaemon).not.toHaveBeenCalled()
    expect(h.sup.state).toBe('defer')
  })

  it('stop() during hosting clears timers first, terminates own process, freezes state', async () => {
    const h = createHarness()
    h.sup.start()
    await flush()
    expect(h.sup.state).toBe('hosting')
    expect(vi.getTimerCount()).toBeGreaterThan(0)

    await h.sup.stop()
    expect(vi.getTimerCount()).toBe(0)
    expect(h.procs[0]!.terminate).toHaveBeenCalledTimes(1)

    // The exit watcher must be detached: no BACKOFF after stop.
    h.procs[0]!.exit(0)
    await flush()
    expect(h.sup.state).toBe('hosting')

    // Idempotent: a second stop never re-terminates.
    await h.sup.stop()
    expect(h.procs[0]!.terminate).toHaveBeenCalledTimes(1)
  })

  it('stop() in hosted kills the spawned daemon', async () => {
    const h = createHarness()
    h.sup.start()
    await flush()
    h.setPing(PING)
    await vi.advanceTimersByTimeAsync(500)
    expect(h.sup.state).toBe('hosted')

    await h.sup.stop()
    expect(h.procs[0]!.terminate).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('stop() in adopted/defer never touches the external daemon', async () => {
    const adopted = createHarness()
    adopted.setPing(PING)
    adopted.sup.start()
    await flush()
    expect(adopted.sup.state).toBe('adopted')
    expect(vi.getTimerCount()).toBeGreaterThan(0)
    await adopted.sup.stop()
    expect(vi.getTimerCount()).toBe(0)
    expect(adopted.deps.spawnDaemon).not.toHaveBeenCalled()

    const deferred = createHarness()
    deferred.deps.detectLaunchAgent.mockResolvedValue(true)
    deferred.sup.start()
    await flush()
    expect(deferred.sup.state).toBe('defer')
    await deferred.sup.stop()
    expect(vi.getTimerCount()).toBe(0)
    expect(deferred.deps.spawnDaemon).not.toHaveBeenCalled()
  })

  it('retry() from FAILED resets the failure budget and re-probes', async () => {
    const h = createHarness({ backoffLimit: 2 })
    h.sup.start()
    await flush()
    await vi.advanceTimersByTimeAsync(5000) // failure 1 → backoff
    await vi.advanceTimersByTimeAsync(1000) // → hosting #2
    await vi.advanceTimersByTimeAsync(5000) // failure 2 → failed
    expect(h.sup.state).toBe('failed')
    expect(vi.getTimerCount()).toBe(0)

    h.sup.retry()
    expect(h.sup.state).toBe('probe')
    await flush()
    expect(h.sup.state).toBe('hosting')
    expect(h.deps.spawnDaemon).toHaveBeenCalledTimes(3)

    // One failure lands in BACKOFF, not straight back to FAILED: budget was reset.
    await vi.advanceTimersByTimeAsync(5000)
    expect(h.sup.state).toBe('backoff')

    // Daemon shows up externally during backoff: the re-probe adopts it.
    h.setPing(PING)
    await vi.advanceTimersByTimeAsync(1000)
    expect(h.sup.state).toBe('adopted')
  })

  it('retry() is a no-op outside FAILED', async () => {
    const h = createHarness()
    h.setPing(PING)
    h.sup.start()
    await flush()
    expect(h.sup.state).toBe('adopted')

    const pingCalls = h.deps.ping.mock.calls.length
    h.sup.retry()
    await flush()
    expect(h.sup.state).toBe('adopted')
    expect(h.deps.ping.mock.calls.length).toBe(pingCalls)
  })

  it('uses injected setTimeout/clearTimeout', async () => {
    const injectedSet = vi.fn((fn: () => void, ms: number) => setTimeout(fn, ms))
    const injectedClear = vi.fn((handle: unknown) =>
      clearTimeout(handle as ReturnType<typeof setTimeout>),
    )
    const deps: SupervisorDeps = {
      ping: async () => null,
      spawnDaemon: () => {
        throw new Error('must not spawn in this test')
      },
      detectLaunchAgent: async () => true,
      log: () => {},
      setTimeout: injectedSet,
      clearTimeout: injectedClear,
    }
    const sup = new DaemonSupervisor(deps, { policy: 'adopt-or-host' })
    sup.start()
    await flush()
    expect(sup.state).toBe('defer')
    expect(injectedSet).toHaveBeenCalled()

    await sup.stop()
    expect(injectedClear).toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('onStateChange unsubscribe detaches the listener', async () => {
    const h = createHarness()
    const seen: SupervisorState[] = []
    const off = h.sup.onStateChange((state) => seen.push(state))
    off()

    h.setPing(PING)
    h.sup.start()
    await flush()
    expect(h.sup.state).toBe('adopted')
    expect(seen).toEqual([])
    expect(h.states).toEqual(['adopted']) // the harness listener still fires
  })
})
