/**
 * Assembly coverage for the plugin entry (src/index.ts) and the composition
 * config schema (src/config.ts).
 *
 * The fake ctx mirrors the cordis surfaces the entry consumes: `effect`
 * runs the body immediately and collects disposers (disposed in reverse
 * order, like a fiber unload), `webServer.register` records the route and
 * returns a removal disposer, `subprocess.spawn` records specs, the logger
 * is silent. The "adopted" path uses a REAL Unix-socket mini daemon
 * (ping/status/subscribe per the sidecar wire protocol) instead of stubbing
 * the read-only bridge module.
 */
import { mkdtemp, rm } from 'node:fs/promises'
import { createServer, type Server } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { apply, Config, inject, name, type HostContext, type SubprocessSpawnSpec } from '../src/index'
import { API_PREFIX } from '../src/routes'
import * as entry from '../src/index'

// ---------------------------------------------------------------------------
// Fakes.
// ---------------------------------------------------------------------------

interface RecordedRoute {
  kind: string
  path: string
  handler: (req: unknown, res: unknown) => void | Promise<void>
}

interface FakeCtx {
  ctx: HostContext
  routes: RecordedRoute[]
  removedRoutes: string[]
  spawns: SubprocessSpawnSpec[]
  logs: string[]
  /** Fire one cordis event to every `ctx.on` listener (M3 dsh feed tests). */
  emit(event: string, ...args: unknown[]): void
  /** Run collected effect disposers in reverse registration order. */
  disposeAll(): Promise<void>
  /** Dispose one labeled lazy binding to simulate a host service generation. */
  disposeEffect(label: string): unknown
  /** Re-run agents-related lazy bindings with a replacement service identity. */
  remountAgentServices(services: {
    agents: unknown
    sessionPersistence: unknown
  }): void
}

/**
 * @param services - lazily injectable services by name (mirroring cordis
 * `ctx.inject`): a callback whose deps are ALL present runs immediately on
 * an extended ctx; any missing dep means the callback never runs (the
 * composition-without-that-service case, e.g. no dsh-settings/dsh-agent).
 * @param spawnOutcome - when set, every fake child's `done` resolves with
 * it (so e.g. LaunchAgent detection can complete); default: never settles.
 */
function createFakeCtx(
  services: Record<string, unknown> = {},
  spawnOutcome?: { exitCode: number | null; signal: NodeJS.Signals | null },
): FakeCtx {
  const disposers: Array<() => unknown> = []
  const routes: RecordedRoute[] = []
  const removedRoutes: string[] = []
  const spawns: SubprocessSpawnSpec[] = []
  const logs: string[] = []
  const effectsByLabel = new Map<string, () => unknown>()
  const injections: Array<{
    deps: string[]
    callback: (ctx: unknown) => void
  }> = []
  const listeners = new Map<string, Set<(...args: unknown[]) => void>>()
  const noop = (): void => {}
  const recordLog = (...args: unknown[]): void => {
    logs.push(
      args
        .map((arg) =>
          typeof arg === 'string' ? arg : JSON.stringify(arg),
        )
        .join(' '),
    )
  }
  const logger = Object.assign(() => logger, {
    info: recordLog,
    warn: recordLog,
    error: recordLog,
    debug: recordLog,
  })
  const ctx = {
    logger,
    effect(execute: () => () => unknown, label?: string) {
      const rawDisposer = execute()
      let active = true
      const disposer = (): unknown => {
        if (!active) return undefined
        active = false
        if (label !== undefined && effectsByLabel.get(label) === disposer) {
          effectsByLabel.delete(label)
        }
        return rawDisposer()
      }
      disposers.push(disposer)
      if (label !== undefined) effectsByLabel.set(label, disposer)
      return disposer
    },
    inject(deps: string[], callback: (ctx: unknown) => void) {
      injections.push({ deps, callback })
      if (!deps.every((dep) => dep in services)) return undefined
      const injected = Object.assign(Object.create(ctx as object), services)
      callback(injected)
      return undefined
    },
    /** cordis event bus subset (M3 dsh feed): `on` returns the disposer. */
    on(event: string, handler: (...args: unknown[]) => void) {
      let set = listeners.get(event)
      if (set === undefined) {
        set = new Set()
        listeners.set(event, set)
      }
      set.add(handler)
      return () => set!.delete(handler)
    },
    /** reflect `get` subset: resolves lazily-consumed services by name. */
    get(name: string) {
      return services[name]
    },
    webServer: {
      register(route: RecordedRoute) {
        routes.push(route)
        return () => {
          removedRoutes.push(route.path)
        }
      },
    },
    subprocess: {
      spawn(spec: SubprocessSpawnSpec) {
        spawns.push(spec)
        return {
          pid: 12345,
          stdin: undefined,
          stdout: undefined,
          stderr: undefined,
          collected: {},
          done: spawnOutcome === undefined ? new Promise<never>(() => {}) : Promise.resolve(spawnOutcome),
          terminate: noop,
          waitForExit: async () => true,
        }
      },
    },
  } as unknown as HostContext
  return {
    ctx,
    routes,
    removedRoutes,
    spawns,
    logs,
    emit: (event, ...args) => {
      for (const handler of listeners.get(event) ?? []) handler(...args)
    },
    disposeAll: async () => {
      for (const disposer of [...disposers].reverse()) {
        await disposer()
      }
      disposers.length = 0
    },
    disposeEffect: (label) => effectsByLabel.get(label)?.(),
    remountAgentServices: (replacement) => {
      services.agents = replacement.agents
      services.sessionPersistence = replacement.sessionPersistence
      for (const injection of injections) {
        const key = injection.deps.join(',')
        if (key !== 'agents' && key !== 'agents,sessionPersistence') continue
        const injected = Object.assign(Object.create(ctx as object), services)
        injection.callback(injected)
      }
    },
  }
}

/** Minimal loopback GET request satisfying guard layers 1-4. */
function fakeReq(url: string): unknown {
  return {
    method: 'GET',
    url,
    headers: { host: '127.0.0.1:3178' },
    socket: { remoteAddress: '127.0.0.1' },
    resume: () => {},
  }
}

/**
 * Loopback JSON POST whose body is delivered on a microtask, after the
 * action handler has attached its data/end listeners synchronously.
 */
function fakePostReq(url: string, body: unknown): unknown {
  const listeners = new Map<string, Array<(arg?: unknown) => void>>()
  let scheduled = false
  const schedule = (): void => {
    if (scheduled) return
    scheduled = true
    queueMicrotask(() => {
      const payload = Buffer.from(JSON.stringify(body), 'utf8')
      for (const cb of listeners.get('data') ?? []) cb(payload)
      for (const cb of listeners.get('end') ?? []) cb()
    })
  }
  return {
    method: 'POST',
    url,
    headers: { host: '127.0.0.1:3178', 'content-type': 'application/json' },
    socket: { remoteAddress: '127.0.0.1' },
    resume: () => {},
    on(event: string, cb: (arg?: unknown) => void) {
      const arr = listeners.get(event) ?? []
      arr.push(cb)
      listeners.set(event, arr)
      schedule()
      return this
    },
  }
}

/** POST <prefix>/action through the recorded route handler. */
async function postAction(
  route: RecordedRoute,
  body: unknown,
): Promise<{ status: number; json: Record<string, unknown> }> {
  const res = fakeRes()
  await route.handler(fakePostReq(`${API_PREFIX}/action`, body), res)
  return { status: res.statusCode, json: JSON.parse(res.body) as Record<string, unknown> }
}

interface FakeRes {
  statusCode: number
  body: string
  writeHead(status: number, headers?: Record<string, string>): void
  write(chunk: string): boolean
  end(chunk?: string): void
  on(event: string, cb: () => void): void
  destroy(): void
}

function fakeRes(): FakeRes {
  return {
    statusCode: 0,
    body: '',
    writeHead(status) {
      this.statusCode = status
    },
    write(chunk) {
      this.body += chunk
      return true
    },
    end(chunk) {
      if (chunk !== undefined) this.body += chunk
    },
    on() {},
    destroy() {},
  }
}

/** GET <prefix>/state through the recorded route handler. */
async function fetchState(route: RecordedRoute): Promise<{
  status: number
  json: { daemon: { state: string; lastPing: { pid: number } | null }; board: { sessions: unknown[] } }
}> {
  const res = fakeRes()
  await route.handler(fakeReq(`${API_PREFIX}/state`), res)
  return { status: res.statusCode, json: JSON.parse(res.body) }
}

/** GET an arbitrary subpath through the recorded route handler. */
async function fetchPath(
  route: RecordedRoute,
  subpath: string,
): Promise<{ status: number; json: Record<string, unknown> }> {
  const res = fakeRes()
  await route.handler(fakeReq(`${API_PREFIX}/${subpath}`), res)
  return { status: res.statusCode, json: JSON.parse(res.body) as Record<string, unknown> }
}

/**
 * Mini sidecar daemon on a real Unix socket: one op per connection
 * (mirroring sidecar/client.py semantics), subscribe ack held open.
 *
 * @param sessions - `status` snapshot rows (SessionRow wire shape).
 */
function startMiniDaemon(socketPath: string, sessions: unknown[] = []): Promise<Server> {
  const server = createServer((socket) => {
    let buffer = ''
    let answered = false
    socket.on('data', (chunk: Buffer) => {
      if (answered) return
      buffer += chunk.toString('utf8')
      const nl = buffer.indexOf('\n')
      if (nl < 0) return
      answered = true
      let op = ''
      try {
        op = (JSON.parse(buffer.slice(0, nl)) as { op?: string }).op ?? ''
      } catch {
        socket.destroy()
        return
      }
      if (op === 'ping') {
        socket.end(
          `${JSON.stringify({ ok: true, op: 'ping', pid: 4242, version: 'test', http: null })}\n`,
        )
      } else if (op === 'status') {
        socket.end(`${JSON.stringify({ ok: true, op: 'status', sessions })}\n`)
      } else if (op === 'subscribe') {
        socket.write(`${JSON.stringify({ ok: true, op: 'subscribe' })}\n`)
      } else {
        socket.destroy()
      }
    })
    socket.on('error', () => {})
  })
  return new Promise((resolve) => {
    server.listen(socketPath, () => resolve(server))
  })
}

/** Let real I/O callbacks land without advancing (possibly fake) timers. */
async function flushIo(rounds = 5): Promise<void> {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise<void>((resolve) => {
      setImmediate(resolve)
    })
  }
}

// ---------------------------------------------------------------------------
// Cleanup registry so failures never leak servers or tmp dirs.
// ---------------------------------------------------------------------------

const cleanups: Array<() => Promise<void> | void> = []

afterEach(async () => {
  while (cleanups.length > 0) {
    await cleanups.pop()?.()
  }
  vi.useRealTimers()
})

async function tempRuntimeDir(): Promise<string> {
  const dir = await mkdtemp(join(tmpdir(), 'sidecar-entry-'))
  cleanups.push(() => rm(dir, { recursive: true, force: true }))
  return dir
}

// ---------------------------------------------------------------------------
// Config schema.
// ---------------------------------------------------------------------------

describe('Config schema', () => {
  it('fills every documented default from an empty config', () => {
    const config = Config({})
    expect(config.daemon.policy).toBe('adopt-or-host')
    expect(config.daemon.backoffLimit).toBe(5)
    expect(config.sidecar.command).toEqual(['agent-sidecar'])
    expect(config.sidecar.runtimeDir).toBe('')
    expect(config.stream.reconcileActiveMs).toBe(2000)
    expect(config.stream.reconcileIdleMs).toBe(10000)
    expect(config.inject.enabled).toBe(false)
    expect(config.inject.defaultMode).toBe('queue')
    expect(config.analysis.enabled).toBe(false)
    expect(config.analysis.provider).toBe('')
    expect(config.analysis.model).toBe('')
    expect(config.ui.timeWindowHours).toBe(24)
    expect(config.ui.showDead).toBe(false)
    expect(config.skill.provide).toBe(true)
  })

  it('merges a partial override with sibling defaults', () => {
    const config = Config({ daemon: { policy: 'adopt-only' } })
    expect(config.daemon.policy).toBe('adopt-only')
    expect(config.daemon.backoffLimit).toBe(5)
    expect(config.inject.enabled).toBe(false)
  })

  it('rejects an unknown daemon policy', () => {
    expect(() => Config({ daemon: { policy: 'always-spawn' as never } })).toThrow()
  })
})

// ---------------------------------------------------------------------------
// Entry exports (postmortem 0001: named faces, no default export).
// ---------------------------------------------------------------------------

describe('entry exports', () => {
  it('exposes the named plugin faces and no default export', () => {
    expect(name).toBe('agent-sidecar')
    expect(inject).toEqual(['webServer', 'subprocess'])
    expect(typeof apply).toBe('function')
    expect(typeof Config).toBe('function')
    expect('default' in entry).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// apply() assembly.
// ---------------------------------------------------------------------------

describe('apply', () => {
  it('registers exactly one prefix route at the API namespace', async () => {
    const fake = createFakeCtx()
    const runtimeDir = await tempRuntimeDir()
    apply(fake.ctx, Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir } }))

    expect(fake.routes).toHaveLength(1)
    expect(fake.routes[0]).toMatchObject({ kind: 'prefix', path: API_PREFIX })
    expect(typeof fake.routes[0]!.handler).toBe('function')

    await fake.disposeAll()
    expect(fake.removedRoutes).toEqual([API_PREFIX])
  })

  it('adopts an answering daemon under adopt-or-host without spawning', async () => {
    const fake = createFakeCtx()
    const runtimeDir = await tempRuntimeDir()
    const server = await startMiniDaemon(join(runtimeDir, 'daemon.sock'))
    cleanups.push(
      () =>
        new Promise<void>((resolve) => {
          server.close(() => resolve())
        }),
    )

    apply(fake.ctx, Config({ daemon: { policy: 'adopt-or-host' }, sidecar: { runtimeDir } }))
    cleanups.push(() => fake.disposeAll())

    await vi.waitFor(
      async () => {
        const { status, json } = await fetchState(fake.routes[0]!)
        expect(status).toBe(200)
        expect(json.daemon.state).toBe('adopted')
      },
      { timeout: 3000, interval: 25 },
    )

    const { json } = await fetchState(fake.routes[0]!)
    expect(json.daemon.lastPing?.pid).toBe(4242)
    expect(json.board.sessions).toEqual([])
    expect(fake.spawns).toHaveLength(0)
  })

  it('never spawns anything under policy off', async () => {
    const fake = createFakeCtx()
    const runtimeDir = await tempRuntimeDir()
    apply(fake.ctx, Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir } }))
    cleanups.push(() => fake.disposeAll())

    await flushIo(20)
    const { json } = await fetchState(fake.routes[0]!)
    // 'defer' is the supervisor's "lifecycle is not ours to manage" resting state.
    expect(json.daemon.state).toBe('defer')
    expect(fake.spawns).toHaveLength(0)
  })

  it('leaves no timers behind once every disposer has run (M2 gateway included)', async () => {
    vi.useFakeTimers({
      // Keep setImmediate/Date real so genuine socket I/O still settles.
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'],
    })
    const fake = createFakeCtx()
    const runtimeDir = await tempRuntimeDir()
    // No daemon socket: the reconciler's status poll and subscribe reconnect
    // both fail over to timers, which teardown must fully clear.
    apply(fake.ctx, Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir } }))

    for (let i = 0; i < 200 && vi.getTimerCount() < 2; i += 1) {
      await flushIo(1)
    }
    expect(vi.getTimerCount()).toBeGreaterThan(0)

    await fake.disposeAll()
    expect(vi.getTimerCount()).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// Cold-start reconcile wiring (M1 acceptance ②): the supervisor's
// ADOPTED/HOSTED transition must trigger one immediate reconcile, so the
// board fills the moment a daemon becomes reachable instead of waiting out
// the reconciler's pending poll.
// ---------------------------------------------------------------------------

describe('cold-start reconcile wiring (M1 ②)', () => {
  /** Advance fake timers in small steps, letting real socket I/O settle between them. */
  async function advanceWithIo(ms: number, step = 250): Promise<void> {
    let remaining = ms
    while (remaining > 0) {
      const chunk = Math.min(step, remaining)
      await vi.advanceTimersByTimeAsync(chunk)
      await flushIo(5)
      remaining -= chunk
    }
  }

  it('adoption of a late-appearing daemon reconciles immediately, not on the pending poll', async () => {
    vi.useFakeTimers({
      toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'],
    })
    // Spawn outcome 2 = `service status` control failure → LaunchAgent read
    // as absent, so the adopt-only supervisor lands in DEFER and re-probes.
    const fake = createFakeCtx({}, { exitCode: 2, signal: null })
    const runtimeDir = await tempRuntimeDir()
    apply(fake.ctx, Config({ daemon: { policy: 'adopt-only' }, sidecar: { runtimeDir } }))
    cleanups.push(() => fake.disposeAll())
    await flushIo(10)

    // t≈0: no socket. The initial reconcile and probe ping both failed; the
    // supervisor defers (re-probe at t=5000), the reconciler's short-backoff
    // retries (t=250/750/1750/3750) all fail too — next own retry t=7750.
    await advanceWithIo(4000)
    let state = await fetchState(fake.routes[0]!)
    expect(state.json.daemon.state).toBe('defer')
    expect(state.json.board.sessions).toHaveLength(0)

    // t=4000: the daemon appears (externally started, adopt-only never spawns).
    const server = await startMiniDaemon(join(runtimeDir, 'daemon.sock'), [DSH_SESSION_ROW])
    cleanups.push(
      () =>
        new Promise<void>((resolve) => {
          server.close(() => resolve())
        }),
    )

    // t=5000: the defer re-probe adopts. With no reconciler timer due until
    // t=7750, only the ADOPTED hand-off can fill the board this instant —
    // I/O flushes only, zero further timer advancement.
    await advanceWithIo(1000)
    for (let i = 0; i < 50; i += 1) {
      state = await fetchState(fake.routes[0]!)
      if (state.json.board.sessions.length > 0) break
      await flushIo(2)
    }
    expect(state.json.daemon.state).toBe('adopted')
    expect(state.json.board.sessions).toHaveLength(1)

    // The wiring's listener and reconcile timers all tear down cleanly.
    await fake.disposeAll()
    expect(vi.getTimerCount()).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// M2 injection wiring (T4.8): gateway assembled into the routes, live
// inject.enabled gate, agents-less degradation, dsh path end-to-end.
// ---------------------------------------------------------------------------

/** One dsh session row in the sidecar `status` wire shape (bridge SessionRow). */
const DSH_SESSION_ROW = {
  agent: 'dsh',
  session_id: 'sess-dsh-1',
  project: '/tmp/proj',
  transcript: '/tmp/proj/session.jsonl',
  updated_at: 1_700_000_000,
  title: 'demo session',
  status: 'waiting',
  extra: {},
  parent_id: null,
}

const absentPresetPersistence = {
  list: async () => [{ id: 'sess-dsh-1' }],
  inspect: async () => ({ meta: {}, events: [] }),
}

interface FakeResumeOptions {
  resumeSessionId: string
  agentOptions?: { provider?: string; model?: string }
  signal?: AbortSignal
  setup?: (
    ctx: unknown,
  ) => { commit(): void } | Promise<{ commit(): void } | void> | void
}

function runSetupSynchronously(
  options: FakeResumeOptions,
  session: { readonly header: unknown; readonly events: readonly unknown[] } = {
    header: {},
    events: [],
  },
): void {
  const setupResult = options.setup?.({ agent: { session } })
  expect(setupResult).not.toBeInstanceOf(Promise)
  if (setupResult !== undefined && !('then' in setupResult)) {
    setupResult.commit()
  }
}

async function prepareColdDsh(
  services: Record<string, unknown>,
  message = 'cold lifecycle probe',
): Promise<{
  fake: FakeCtx
  prepared: Awaited<ReturnType<typeof postAction>>
}> {
  const fake = createFakeCtx(services)
  const runtimeDir = await tempRuntimeDir()
  const server = await startMiniDaemon(join(runtimeDir, 'daemon.sock'), [
    DSH_SESSION_ROW,
  ])
  cleanups.push(
    () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve())
      }),
  )
  apply(
    fake.ctx,
    Config({
      daemon: { policy: 'adopt-only' },
      sidecar: { runtimeDir },
      inject: { enabled: true },
    }),
  )
  cleanups.push(() => fake.disposeAll())
  await vi.waitFor(
    async () => {
      const { json } = await fetchState(fake.routes[0]!)
      expect(json.board.sessions).toHaveLength(1)
    },
    { timeout: 3000, interval: 25 },
  )
  const prepared = await postAction(fake.routes[0]!, {
    type: 'inject.prepare',
    target: { agent: 'dsh', sessionId: 'sess-dsh-1' },
    mode: 'queue',
    message,
  })
  return { fake, prepared }
}

describe('M2 injection wiring', () => {
  it('dispatches inject.prepare through the wired gateway, not the M1 placeholder', async () => {
    const fake = createFakeCtx()
    const runtimeDir = await tempRuntimeDir()
    apply(
      fake.ctx,
      Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir }, inject: { enabled: true } }),
    )
    cleanups.push(() => fake.disposeAll())

    const { status, json } = await postAction(fake.routes[0]!, {
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 'missing' },
      mode: 'queue',
      message: 'ping',
    })
    // 404 target_not_found comes from the gateway's live store re-check; the
    // M1 wiring (no gateway) would have answered 501 not_implemented_until_m2.
    expect(status).toBe(404)
    expect(json.reason).toBe('target_not_found')
  })

  it('refuses inject.prepare with 403 while inject.enabled is false (default)', async () => {
    const fake = createFakeCtx()
    const runtimeDir = await tempRuntimeDir()
    apply(fake.ctx, Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir } }))
    cleanups.push(() => fake.disposeAll())

    const { status, json } = await postAction(fake.routes[0]!, {
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 'missing' },
      mode: 'queue',
      message: 'ping',
    })
    expect(status).toBe(403)
    expect(json.reason).toBe('inject_disabled')
  })

  it('loads without optional model/agents services and rejects cold prepare without a token', async () => {
    // Default fake ctx = composition without dsh-agent: the lazy inject
    // callback never runs. The fiber must not pend — M1 reads and the
    // prepare surface stay fully available (send-cli path unaffected).
    const fake = createFakeCtx()
    const runtimeDir = await tempRuntimeDir()
    const server = await startMiniDaemon(join(runtimeDir, 'daemon.sock'), [DSH_SESSION_ROW])
    cleanups.push(
      () =>
        new Promise<void>((resolve) => {
          server.close(() => resolve())
        }),
    )
    apply(
      fake.ctx,
      Config({
        daemon: { policy: 'adopt-only' },
        sidecar: { runtimeDir },
        inject: { enabled: true },
      }),
    )
    cleanups.push(() => fake.disposeAll())

    await vi.waitFor(
      async () => {
        const { status, json } = await fetchState(fake.routes[0]!)
        expect(status).toBe(200)
        expect(json.board.sessions).toHaveLength(1)
      },
      { timeout: 3000, interval: 25 },
    )

    // The target is cold and the dsh agent binding is absent: reject before
    // confirmation rather than issue a token for an unavailable resume.
    const prepared = await postAction(fake.routes[0]!, {
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 'sess-dsh-1' },
      mode: 'queue',
      message: 'ping',
    })
    expect(prepared.status).toBe(502)
    expect(prepared.json).toEqual({ reason: 'dsh_agents_unavailable' })
    expect(prepared.json.confirmToken).toBeUndefined()
  })

  it('maps a stale board row with no persisted session to target_not_found without a token', async () => {
    const inspect = vi.fn(async () => ({ meta: {}, events: [] }))
    const resume = vi.fn(async (): Promise<never> => {
      throw new Error('resume must not be called')
    })
    const model = 'SECRET-MISSING-MODEL'
    const { fake, prepared } = await prepareColdDsh({
      agents: { get: () => undefined, resume },
      sessionPersistence: {
        list: async () => [],
        inspect,
      },
      agentDefaultModel: {
        currentSelection: () => ({ provider: 'SECRET-MISSING-PROVIDER', model }),
      },
    }, 'SECRET-MISSING-MESSAGE')

    expect(prepared.status).toBe(404)
    expect(prepared.json).toEqual({ reason: 'target_not_found' })
    expect(prepared.json.confirmToken).toBeUndefined()
    expect(inspect).not.toHaveBeenCalled()
    expect(resume).not.toHaveBeenCalled()
    const external = `${JSON.stringify(prepared.json)}${fake.logs.join('\n')}`
    expect(external).not.toContain('sess-dsh-1')
    expect(external).not.toContain(model)
  })

  it('adopts a live agent created after list without inspect, model lookup, or resume', async () => {
    const followup = vi.fn()
    const steer = vi.fn()
    let live: { followup: typeof followup; steer: typeof steer } | undefined
    const inspect = vi.fn(async () => ({ meta: {}, events: [] }))
    const list = vi.fn(async () => {
      live = { followup, steer }
      return []
    })
    const resume = vi.fn(async (): Promise<never> => {
      throw new Error('resume must not be called')
    })
    const currentSelection = vi.fn(() => {
      throw new Error('live agent must not resolve a model')
    })
    const { fake, prepared } = await prepareColdDsh({
      agents: {
        get: () => live,
        resume,
      },
      sessionPersistence: { list, inspect },
      agentDefaultModel: { currentSelection },
    }, 'concurrent live message')

    expect(prepared.status).toBe(200)
    const executed = await postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: prepared.json.requestId,
      confirmToken: prepared.json.confirmToken,
      message: 'concurrent live message',
    })
    expect(executed.status).toBe(200)
    expect(executed.json).toEqual({ outcome: 'delivered' })
    expect(list).toHaveBeenCalledTimes(1)
    expect(inspect).not.toHaveBeenCalled()
    expect(currentSelection).not.toHaveBeenCalled()
    expect(resume).not.toHaveBeenCalled()
    expect(followup).toHaveBeenCalledTimes(1)
  })

  it('maps deletion during execute inspection to a stable 404 without resume or splice', async () => {
    const followup = vi.fn()
    let deleteOnInspect = false
    let persisted = true
    const inspect = vi.fn(async () => {
      if (deleteOnInspect) {
        persisted = false
        throw new Error('/SECRET/deleted/session/path')
      }
      return { meta: {}, events: [] }
    })
    const list = vi.fn(async () =>
      persisted ? [{ id: 'sess-dsh-1' }] : [],
    )
    const resume = vi.fn(async (): Promise<never> => {
      throw new Error('resume must not be called')
    })
    const { fake, prepared } = await prepareColdDsh({
      agents: { get: () => undefined, resume },
      sessionPersistence: { list, inspect },
      agentDefaultModel: {
        currentSelection: () => ({ provider: 'provider', model: 'model' }),
      },
    }, 'delete race message')
    expect(prepared.status).toBe(200)

    const logsBeforeExecute = fake.logs.length
    deleteOnInspect = true
    const executed = await postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: prepared.json.requestId,
      confirmToken: prepared.json.confirmToken,
      message: 'delete race message',
    })

    expect(executed.status).toBe(404)
    expect(executed.json).toEqual({
      outcome: 'failed',
      errorCode: 'target_not_found',
    })
    expect(list).toHaveBeenCalledTimes(3)
    expect(inspect).toHaveBeenCalledTimes(2)
    expect(resume).not.toHaveBeenCalled()
    expect(followup).not.toHaveBeenCalled()
    const external =
      `${JSON.stringify(executed.json)}${fake.logs.slice(logsBeforeExecute).join('\n')}`
    expect(external).not.toContain('/SECRET/deleted/session/path')
    expect(external).not.toContain('sess-dsh-1')
  })

  it('delivers a dsh queue injection end-to-end once agents resolves', async () => {
    const followup = vi.fn()
    const steer = vi.fn()
    const agents = {
      get: (sessionId: string) =>
        sessionId === 'sess-dsh-1' ? { followup, steer } : undefined,
      resume: async (): Promise<never> => {
        throw new Error('resume must not be called for a live session')
      },
    }
    const currentSelection = vi.fn(() => ({
      provider: 'must-not-read-live-provider',
      model: 'must-not-read-live-model',
    }))
    const fake = createFakeCtx({
      agents,
      agentDefaultModel: { currentSelection },
      sessionPersistence: absentPresetPersistence,
    })
    const runtimeDir = await tempRuntimeDir()
    const server = await startMiniDaemon(join(runtimeDir, 'daemon.sock'), [DSH_SESSION_ROW])
    cleanups.push(
      () =>
        new Promise<void>((resolve) => {
          server.close(() => resolve())
        }),
    )
    apply(
      fake.ctx,
      Config({
        daemon: { policy: 'adopt-only' },
        sidecar: { runtimeDir },
        inject: { enabled: true },
      }),
    )
    cleanups.push(() => fake.disposeAll())

    await vi.waitFor(
      async () => {
        const { json } = await fetchState(fake.routes[0]!)
        expect(json.board.sessions).toHaveLength(1)
      },
      { timeout: 3000, interval: 25 },
    )

    const prepared = await postAction(fake.routes[0]!, {
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 'sess-dsh-1' },
      mode: 'queue',
      message: 'hello from the board',
    })
    expect(prepared.status).toBe(200)
    const plan = prepared.json.plan as { targetStatus: { status: string } }
    expect(plan.targetStatus.status).toBe('waiting')

    const executed = await postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: prepared.json.requestId,
      confirmToken: prepared.json.confirmToken,
      message: 'hello from the board',
    })
    expect(executed.status).toBe(200)
    expect(executed.json.outcome).toBe('delivered')

    expect(followup).toHaveBeenCalledTimes(1)
    const message = followup.mock.calls[0]![0] as {
      content: unknown
      source: unknown
    }
    expect(message.content).toEqual([{ type: 'text', text: 'hello from the board' }])
    expect(message.source).toEqual({ kind: 'plugin', plugin: 'agent-sidecar' })
    expect(steer).not.toHaveBeenCalled()
    expect(currentSelection).not.toHaveBeenCalled()
  })

  it('cold-resumes with complete host-default agentOptions before reporting inbox acceptance', async () => {
    const followup = vi.fn()
    const steer = vi.fn()
    const resume = vi.fn(
      async (options: FakeResumeOptions) => {
        runSetupSynchronously(options)
        return {
          agent: { followup, steer },
          dispose: async () => {},
          options,
        }
      },
    )
    const agents = {
      get: () => undefined,
      resume,
    }
    const route = {
      provider: 'cold-host-provider',
      model: 'cold-host-model',
    }
    const currentSelection = vi.fn(() => ({ ...route }))
    const fake = createFakeCtx({
      agents,
      agentDefaultModel: { currentSelection },
      sessionPersistence: absentPresetPersistence,
    })
    const runtimeDir = await tempRuntimeDir()
    const server = await startMiniDaemon(join(runtimeDir, 'daemon.sock'), [DSH_SESSION_ROW])
    cleanups.push(
      () =>
        new Promise<void>((resolve) => {
          server.close(() => resolve())
        }),
    )
    apply(
      fake.ctx,
      Config({
        daemon: { policy: 'adopt-only' },
        sidecar: { runtimeDir },
        inject: { enabled: true },
      }),
    )
    cleanups.push(() => fake.disposeAll())

    await vi.waitFor(
      async () => {
        const { json } = await fetchState(fake.routes[0]!)
        expect(json.board.sessions).toHaveLength(1)
      },
      { timeout: 3000, interval: 25 },
    )

    const prepared = await postAction(fake.routes[0]!, {
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 'sess-dsh-1' },
      mode: 'queue',
      message: 'cold accepted turn',
    })
    expect(prepared.status).toBe(200)

    const executed = await postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: prepared.json.requestId,
      confirmToken: prepared.json.confirmToken,
      message: 'cold accepted turn',
    })
    expect(executed.status).toBe(200)
    expect(executed.json).toEqual({ outcome: 'delivered' })
    expect(currentSelection).toHaveBeenCalledTimes(2)
    expect(resume).toHaveBeenCalledTimes(1)
    expect(resume).toHaveBeenCalledWith(expect.objectContaining({
      resumeSessionId: 'sess-dsh-1',
      agentOptions: {
        provider: route.provider,
        model: route.model,
      },
      signal: expect.any(AbortSignal),
      setup: expect.any(Function),
    }))
    expect(followup).toHaveBeenCalledTimes(1)
    expect(followup.mock.calls[0]![0]).toMatchObject({
      content: [{ type: 'text', text: 'cold accepted turn' }],
    })
    expect(steer).not.toHaveBeenCalled()
  })

  it('rolls back when agentPresets appears between inspect and unpublished setup', async () => {
    const followup = vi.fn()
    let published = false
    const services: Record<string, unknown> = {}
    const resume = vi.fn(async (options: FakeResumeOptions) => {
      services['agentPresets'] = {}
      runSetupSynchronously(options)
      published = true
      return {
        agent: { followup, steer: vi.fn() },
        dispose: async () => {},
      }
    })
    Object.assign(services, {
      agents: { get: () => undefined, resume },
      sessionPersistence: absentPresetPersistence,
      agentDefaultModel: {
        currentSelection: () => ({ provider: 'provider', model: 'model' }),
      },
    })
    const { fake, prepared } = await prepareColdDsh(services)
    expect(prepared.status).toBe(200)

    const executed = await postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: prepared.json.requestId,
      confirmToken: prepared.json.confirmToken,
      message: 'cold lifecycle probe',
    })

    expect(executed.status).toBe(200)
    expect(executed.json).toEqual({ outcome: 'delivered' })
    expect(published).toBe(true)
    expect(followup).toHaveBeenCalledTimes(1)
  })

  it('rolls back when the cold host binding generation changes before setup', async () => {
    const followup = vi.fn()
    let published = false
    let fake: FakeCtx | undefined
    const resume = vi.fn(async (options: FakeResumeOptions) => {
      fake!.disposeEffect('agent-sidecar: cold dsh resume services release')
      runSetupSynchronously(options)
      published = true
      return {
        agent: { followup, steer: vi.fn() },
        dispose: async () => {},
      }
    })
    const preparedResult = await prepareColdDsh({
      agents: { get: () => undefined, resume },
      sessionPersistence: absentPresetPersistence,
      agentDefaultModel: {
        currentSelection: () => ({ provider: 'provider', model: 'model' }),
      },
    })
    fake = preparedResult.fake
    expect(preparedResult.prepared.status).toBe(200)

    const executed = await postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: preparedResult.prepared.json.requestId,
      confirmToken: preparedResult.prepared.json.confirmToken,
      message: 'cold lifecycle probe',
    })

    expect(executed.status).toBe(502)
    expect(executed.json).toEqual({
      outcome: 'failed',
      errorCode: 'dsh_agents_unavailable',
    })
    expect(published).toBe(false)
    expect(followup).not.toHaveBeenCalled()
  })

  it.each([
    {
      label: 'header preset',
      session: {
        header: { agentPreset: 'SECRET-ACTUAL-HEADER-PRESET' },
        events: [],
      },
      status: 409,
      errorCode: 'dsh_preset_unsupported',
    },
    {
      label: 'late selected preset',
      session: {
        header: {},
        events: [
          {
            type: 'agent-preset/selected',
            data: { agentPreset: 'SECRET-ACTUAL-LATE-PRESET' },
          },
        ],
      },
      status: 409,
      errorCode: 'dsh_preset_unsupported',
    },
    {
      label: 'unknown selected schema',
      session: {
        header: {},
        events: [{ type: 'agent-preset/selected', data: {} }],
      },
      status: 502,
      errorCode: 'executor_error',
    },
  ])(
    'rejects actual unpublished session $label despite absent inspect proof',
    async ({ session, status, errorCode }) => {
      const followup = vi.fn()
      let published = false
      const resume = vi.fn(async (options: FakeResumeOptions) => {
        runSetupSynchronously(options, session)
        published = true
        return {
          agent: { followup, steer: vi.fn() },
          dispose: async () => {},
        }
      })
      const { fake, prepared } = await prepareColdDsh({
        agents: { get: () => undefined, resume },
        sessionPersistence: absentPresetPersistence,
        agentDefaultModel: {
          currentSelection: () => ({ provider: 'provider', model: 'model' }),
        },
      })
      expect(prepared.status).toBe(200)

      const executed = await postAction(fake.routes[0]!, {
        type: 'inject.execute',
        requestId: prepared.json.requestId,
        confirmToken: prepared.json.confirmToken,
        message: 'cold lifecycle probe',
      })

      expect(executed.status).toBe(status)
      expect(executed.json).toEqual({
        outcome: 'failed',
        errorCode,
      })
      expect(published).toBe(false)
      expect(followup).not.toHaveBeenCalled()
      const external = `${JSON.stringify(executed.json)}${fake.logs.join('\n')}`
      expect(external).not.toContain('SECRET-ACTUAL')
    },
  )

  it('disposes a fulfilled cold-resume slot when the plugin unloads', async () => {
    const followup = vi.fn()
    const disposeHandle = vi.fn(async () => {})
    const resume = vi.fn(async (options: FakeResumeOptions) => {
      runSetupSynchronously(options)
      return {
        agent: { followup, steer: vi.fn() },
        dispose: disposeHandle,
      }
    })
    const { fake, prepared } = await prepareColdDsh({
      agents: { get: () => undefined, resume },
      sessionPersistence: absentPresetPersistence,
      agentDefaultModel: {
        currentSelection: () => ({ provider: 'provider', model: 'model' }),
      },
    })
    const executed = await postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: prepared.json.requestId,
      confirmToken: prepared.json.confirmToken,
      message: 'cold lifecycle probe',
    })
    expect(executed.json).toEqual({ outcome: 'delivered' })
    expect(disposeHandle).not.toHaveBeenCalled()

    await fake.disposeAll()
    expect(disposeHandle).toHaveBeenCalledTimes(1)
  })

  it('retires a fulfilled old-generation slot and resumes on the replacement binding', async () => {
    const oldFollowup = vi.fn()
    const oldDispose = vi.fn(async () => {})
    const oldResume = vi.fn(async (options: FakeResumeOptions) => {
      runSetupSynchronously(options)
      return {
        agent: { followup: oldFollowup, steer: vi.fn() },
        dispose: oldDispose,
      }
    })
    const { fake, prepared } = await prepareColdDsh({
      agents: { get: () => undefined, resume: oldResume },
      sessionPersistence: absentPresetPersistence,
      agentDefaultModel: {
        currentSelection: () => ({ provider: 'provider', model: 'model' }),
      },
    })
    const first = await postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: prepared.json.requestId,
      confirmToken: prepared.json.confirmToken,
      message: 'cold lifecycle probe',
    })
    expect(first.json).toEqual({ outcome: 'delivered' })
    expect(oldFollowup).toHaveBeenCalledTimes(1)

    await fake.disposeEffect('agent-sidecar: cold dsh resume services release')
    await fake.disposeEffect('agent-sidecar: agents binding release')
    expect(oldDispose).toHaveBeenCalledTimes(1)

    const newFollowup = vi.fn()
    const newResume = vi.fn(async (options: FakeResumeOptions) => {
      runSetupSynchronously(options)
      return {
        agent: { followup: newFollowup, steer: vi.fn() },
        dispose: async () => {},
      }
    })
    fake.remountAgentServices({
      agents: { get: () => undefined, resume: newResume },
      sessionPersistence: absentPresetPersistence,
    })
    const preparedAgain = await postAction(fake.routes[0]!, {
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 'sess-dsh-1' },
      mode: 'queue',
      message: 'new generation message',
    })
    const executedAgain = await postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: preparedAgain.json.requestId,
      confirmToken: preparedAgain.json.confirmToken,
      message: 'new generation message',
    })

    expect(executedAgain.json).toEqual({ outcome: 'delivered' })
    expect(oldResume).toHaveBeenCalledTimes(1)
    expect(oldFollowup).toHaveBeenCalledTimes(1)
    expect(newResume).toHaveBeenCalledTimes(1)
    expect(newFollowup).toHaveBeenCalledTimes(1)
  })

  it('prevents a pending old generation from overwriting the replacement slot', async () => {
    const oldFollowup = vi.fn()
    const oldDispose = vi.fn(async () => {})
    let settleOld!: (handle: {
      agent: { followup: typeof oldFollowup; steer: ReturnType<typeof vi.fn> }
      dispose: typeof oldDispose
    }) => void
    const oldPending = new Promise<{
      agent: { followup: typeof oldFollowup; steer: ReturnType<typeof vi.fn> }
      dispose: typeof oldDispose
    }>((resolve) => {
      settleOld = resolve
    })
    const oldResume = vi.fn((_options: FakeResumeOptions) => oldPending)
    const { fake, prepared } = await prepareColdDsh(
      {
        agents: { get: () => undefined, resume: oldResume },
        sessionPersistence: absentPresetPersistence,
        agentDefaultModel: {
          currentSelection: () => ({ provider: 'provider', model: 'model' }),
        },
      },
      'old pending request',
    )
    const preparedNew = await postAction(fake.routes[0]!, {
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 'sess-dsh-1' },
      mode: 'queue',
      message: 'new request',
    })
    const oldExecution = postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: prepared.json.requestId,
      confirmToken: prepared.json.confirmToken,
      message: 'old pending request',
    })
    await vi.waitFor(() => expect(oldResume).toHaveBeenCalledTimes(1))
    const oldSignal = (
      oldResume.mock.calls[0]![0] as FakeResumeOptions
    ).signal

    const releasing = Promise.resolve(
      fake.disposeEffect('agent-sidecar: cold dsh resume services release'),
    )
    await fake.disposeEffect('agent-sidecar: agents binding release')
    expect(oldSignal?.aborted).toBe(true)

    const newFollowup = vi.fn()
    let newLoaded:
      | { followup: typeof newFollowup; steer: ReturnType<typeof vi.fn> }
      | undefined
    const newResume = vi.fn(async (options: FakeResumeOptions) => {
      runSetupSynchronously(options)
      newLoaded = { followup: newFollowup, steer: vi.fn() }
      return {
        agent: newLoaded,
        dispose: async () => {},
      }
    })
    fake.remountAgentServices({
      agents: { get: () => newLoaded, resume: newResume },
      sessionPersistence: absentPresetPersistence,
    })
    const newExecution = postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: preparedNew.json.requestId,
      confirmToken: preparedNew.json.confirmToken,
      message: 'new request',
    })
    await new Promise<void>((resolve) => setTimeout(resolve, 20))
    expect(newResume).not.toHaveBeenCalled()
    expect(newFollowup).not.toHaveBeenCalled()
    expect(oldFollowup).not.toHaveBeenCalled()

    settleOld({
      agent: { followup: oldFollowup, steer: vi.fn() },
      dispose: oldDispose,
    })
    await releasing
    await expect(newExecution).resolves.toMatchObject({
      json: { outcome: 'delivered' },
    })
    await expect(oldExecution).resolves.toMatchObject({
      json: { outcome: 'delivered' },
    })

    expect(oldDispose).toHaveBeenCalledTimes(1)
    expect(oldFollowup).not.toHaveBeenCalled()
    expect(oldResume).toHaveBeenCalledTimes(1)
    expect(newResume).toHaveBeenCalledTimes(1)
    expect(newFollowup).toHaveBeenCalledTimes(2)
  })

  it.each([
    {
      label: 'header preset',
      inspect: async () => ({
        meta: { agentPreset: 'SECRET-HEADER-PRESET' },
        events: [],
      }),
      agentPresets: false,
      status: 409,
      reason: 'dsh_preset_unsupported',
    },
    {
      label: 'latest preset selection',
      inspect: async () => ({
        meta: {},
        events: [
          { type: 'agent-preset/selected', data: {} },
          {
            type: 'agent-preset/selected',
            data: { agentPreset: 'SECRET-LATEST-PRESET' },
          },
        ],
      }),
      agentPresets: false,
      status: 409,
      reason: 'dsh_preset_unsupported',
    },
    {
      label: 'implicit default preset',
      inspect: async () => ({ meta: {}, events: [] }),
      agentPresets: true,
      status: 200,
      reason: undefined,
    },
    {
      label: 'unknown selected-event schema',
      inspect: async () => ({
        meta: {},
        events: [{ type: 'agent-preset/selected', data: {} }],
      }),
      agentPresets: false,
      status: 502,
      reason: 'executor_error',
    },
    {
      label: 'failed persistence inspection',
      inspect: async (): Promise<never> => {
        throw new Error('/SECRET/private/session/path')
      },
      agentPresets: false,
      status: 502,
      reason: 'executor_error',
    },
  ])(
    'handles cold $label without leaking lifecycle secrets',
    async ({ inspect, agentPresets, status, reason }) => {
      const resume = vi.fn(async (): Promise<never> => {
        throw new Error('resume must not be called')
      })
      const services: Record<string, unknown> = {
        agents: { get: () => undefined, resume },
        sessionPersistence: {
          list: async () => [{ id: 'sess-dsh-1' }],
          inspect,
        },
        agentDefaultModel: {
          currentSelection: () => ({
            provider: 'SECRET-PROVIDER',
            model: 'SECRET-MODEL',
          }),
        },
      }
      if (agentPresets) services['agentPresets'] = {}

      const message = 'SECRET-MESSAGE-BODY'
      const { fake, prepared } = await prepareColdDsh(services, message)

      expect(prepared.status).toBe(status)
      if (status === 200) {
        expect(prepared.json.reason).toBeUndefined()
        expect(prepared.json.confirmToken).toEqual(expect.any(String))
      } else {
        expect(prepared.json).toEqual({ reason })
        expect(prepared.json.confirmToken).toBeUndefined()
      }
      expect(resume).not.toHaveBeenCalled()
      const external = `${JSON.stringify(prepared.json)}${fake.logs.join('\n')}`
      for (const secret of [
        'SECRET-HEADER-PRESET',
        'SECRET-LATEST-PRESET',
        '/SECRET/private/session/path',
        'SECRET-PROVIDER',
        'SECRET-MODEL',
        ...(status === 200 ? [] : [message]),
      ]) {
        expect(external).not.toContain(secret)
      }
    },
  )

  it.each(['agents', 'sessionPersistence'] as const)(
    'rejects cold prepare with missing %s before issuing a token',
    async (missing) => {
      const resume = vi.fn(async (): Promise<never> => {
        throw new Error('resume must not be called')
      })
      const services: Record<string, unknown> = {
        agentDefaultModel: {
          currentSelection: () => ({ provider: 'provider', model: 'model' }),
        },
      }
      if (missing !== 'agents') {
        services['agents'] = { get: () => undefined, resume }
      }
      if (missing !== 'sessionPersistence') {
        services['sessionPersistence'] = absentPresetPersistence
      }

      const { prepared } = await prepareColdDsh(services)
      expect(prepared.status).toBe(502)
      expect(prepared.json).toEqual({ reason: 'dsh_agents_unavailable' })
      expect(prepared.json.confirmToken).toBeUndefined()
      expect(resume).not.toHaveBeenCalled()
    },
  )

  it.each([
    ['absent', null],
    [
      'throwing',
      {
        currentSelection: () => {
          throw new Error('default selection unavailable')
        },
      },
    ],
    ['partial', { currentSelection: () => ({ provider: 'provider-only' }) }],
    ['blank', { currentSelection: () => ({ provider: '   ', model: '\t' }) }],
  ] as const)(
    'rejects a cold target with %s host-default service before issuing a token',
    async (_case, modelService) => {
      const resume = vi.fn(async (): Promise<never> => {
        throw new Error('resume must not be called')
      })
      const services: Record<string, unknown> = {
        agents: { get: () => undefined, resume },
        sessionPersistence: absentPresetPersistence,
      }
      if (modelService !== null) services['agentDefaultModel'] = modelService
      const fake = createFakeCtx(services)
      const runtimeDir = await tempRuntimeDir()
      const server = await startMiniDaemon(join(runtimeDir, 'daemon.sock'), [DSH_SESSION_ROW])
      cleanups.push(
        () =>
          new Promise<void>((resolve) => {
            server.close(() => resolve())
          }),
      )
      apply(
        fake.ctx,
        Config({
          daemon: { policy: 'adopt-only' },
          sidecar: { runtimeDir },
          inject: { enabled: true },
        }),
      )
      cleanups.push(() => fake.disposeAll())

      await vi.waitFor(
        async () => {
          const { json } = await fetchState(fake.routes[0]!)
          expect(json.board.sessions).toHaveLength(1)
        },
        { timeout: 3000, interval: 25 },
      )

      const prepared = await postAction(fake.routes[0]!, {
        type: 'inject.prepare',
        target: { agent: 'dsh', sessionId: 'sess-dsh-1' },
        mode: 'queue',
        message: 'must not receive a token',
      })
      expect(prepared.status).toBe(409)
      expect(prepared.json).toEqual({ reason: 'dsh_model_unconfigured' })
      expect(prepared.json.confirmToken).toBeUndefined()
      expect(resume).not.toHaveBeenCalled()
    },
  )
})

// ---------------------------------------------------------------------------
// Settings wiring (M2 review F-1): the inject.enabled gate must read the
// LIVE settings value — a runtime commit through `scope.watch` flips both
// write-gated endpoints immediately, no restart, no reassembly — and the
// namespace must declare `applies: 'live'` (in the installed dsh-settings,
// `applies` is UI-badge metadata only; watchers fire regardless).
// ---------------------------------------------------------------------------

interface FakeSettings {
  service: {
    register(
      ns: string,
      schema: unknown,
      options?: { base?: unknown; applies?: 'live' | 'restart' },
    ): {
      get(): unknown
      watch(cb: (next: unknown, prev: unknown) => void): () => void
    }
  }
  registrations: Array<{ ns: string; applies?: 'live' | 'restart' }>
  /** Commit a new resolved value: swap, then notify watchers (dsh-settings shape). */
  commit(next: unknown): void
}

function makeFakeSettings(): FakeSettings {
  const watchers = new Set<(next: unknown, prev: unknown) => void>()
  const registrations: FakeSettings['registrations'] = []
  let value: unknown
  return {
    registrations,
    service: {
      register(ns, _schema, options) {
        registrations.push({
          ns,
          ...(options?.applies !== undefined ? { applies: options.applies } : {}),
        })
        value = options?.base
        return {
          get: () => value,
          watch(cb) {
            watchers.add(cb)
            return () => watchers.delete(cb)
          },
        }
      },
    },
    commit(next) {
      const prev = value
      value = next
      for (const cb of [...watchers]) cb(next, prev)
    },
  }
}

// ---------------------------------------------------------------------------
// M3 fusion wiring (T5.9): fusion reaches the routes, dsh-feed-less
// compositions degrade to sidecar-only sources, sessionQuery degrades per
// call, and the lazy `sessions` inject swaps a live feed into the holder.
// ---------------------------------------------------------------------------

describe('M3 fusion wiring', () => {
  it('wires fusion into the routes and degrades to sidecar-only without the sessions service', async () => {
    // Default fake ctx = composition without dsh-session: the lazy inject
    // callback never runs, so fusion serves from the sidecar source alone
    // — and the fiber must not pend (same posture as the agents inject).
    const fake = createFakeCtx()
    const runtimeDir = await tempRuntimeDir()
    const freshRow = {
      ...DSH_SESSION_ROW,
      // getProjectGroups windows on wall-clock recency (24h default).
      updated_at: Math.floor(Date.now() / 1000),
    }
    const server = await startMiniDaemon(join(runtimeDir, 'daemon.sock'), [freshRow])
    cleanups.push(
      () =>
        new Promise<void>((resolve) => {
          server.close(() => resolve())
        }),
    )
    apply(fake.ctx, Config({ daemon: { policy: 'adopt-only' }, sidecar: { runtimeDir } }))
    cleanups.push(() => fake.disposeAll())

    await vi.waitFor(
      async () => {
        const { json } = await fetchState(fake.routes[0]!)
        expect(json.board.sessions).toHaveLength(1)
      },
      { timeout: 3000, interval: 25 },
    )
    const route = fake.routes[0]!

    // projects: the real fusion groups the sidecar row (not 501).
    const projects = await fetchPath(route, 'projects')
    expect(projects.status).toBe(200)
    const groups = projects.json.groups as Array<{ project: string; agents: string[] }>
    expect(groups).toHaveLength(1)
    expect(groups[0]!.project).toBe('/tmp/proj')
    expect(groups[0]!.agents).toEqual(['dsh'])

    // lineage: sessionQuery is absent in this composition → honest 200
    // degradation through the full assembly.
    const lineage = await fetchPath(route, 'lineage/sess-dsh-1')
    expect(lineage.status).toBe(200)
    expect(lineage.json).toEqual({
      available: false,
      trace: null,
      reason: 'session_query_unavailable',
    })

    // timeline: the mini daemon knows no replay op (connection dropped) and
    // nothing is buffered — the board-known session still answers 200 with
    // an empty page and honest source provenance.
    const timeline = await fetchPath(route, 'session/sess-dsh-1/timeline')
    expect(timeline.status).toBe(200)
    expect(timeline.json.entries).toEqual([])
    expect((timeline.json.sources as Record<string, boolean>).sidecarReplay).toBe(false)

    // search: filter-only degradation over the unified view.
    const search = await fetchPath(route, 'search?q=demo')
    expect(search.status).toBe(200)
    expect(search.json.mode).toBe('filter-only')
    expect(search.json.items as unknown[]).toHaveLength(1)
  })

  it('binds the dsh event feed through the lazy sessions inject (live-only session resolves)', async () => {
    // `sessions` present: the inject callback swaps a feed-backed fusion
    // into the holder; in-process events then surface sessions the sidecar
    // has never observed on disk.
    const fake = createFakeCtx({ sessions: {} })
    const runtimeDir = await tempRuntimeDir()
    apply(fake.ctx, Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir } }))
    cleanups.push(() => fake.disposeAll())

    const live = {
      id: 'live-only-1',
      header: { createdAt: Date.now(), cwd: '/tmp/live' },
      events: [{ type: 'message/user', seq: 1, time: Date.now(), data: { text: 'hi' } }],
    }
    fake.emit('session/created', live)
    fake.emit('session/event', live, live.events[0])

    const detail = await fetchPath(fake.routes[0]!, 'session/live-only-1')
    expect(detail.status).toBe(200)
    expect(detail.json.session).toBeNull()
    const unified = detail.json.unified as { origin: string; live: boolean; project: string }
    expect(unified.origin).toBe('dsh-live')
    expect(unified.live).toBe(true)
    expect(unified.project).toBe('/tmp/live')
    const timeline = detail.json.timeline as {
      entries: Array<{ kind: string }>
      sources: Record<string, boolean>
    }
    expect(timeline.entries.map((e) => e.kind)).toEqual(['message/user'])
    expect(timeline.sources.dshLive).toBe(true)
  })

  it('uses production sessions.get for pre-subscription live sessions and degrades safely', async () => {
    const live = {
      id: 'already-live-1',
      header: { createdAt: Date.now(), cwd: '/tmp/already-live' },
      events: [{ type: 'message/user', seq: 7, time: Date.now(), data: { text: 'ready' } }],
    }
    let serviceThrows = false
    const get = vi.fn((sessionId: string) => {
      if (serviceThrows) throw new Error('sessions service generation departed')
      return sessionId === live.id ? live : undefined
    })
    const fake = createFakeCtx({ sessions: { get } })
    const runtimeDir = await tempRuntimeDir()
    apply(fake.ctx, Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir } }))
    cleanups.push(() => fake.disposeAll())

    // No synthetic session/created event: this traverses apply → route →
    // FusionQuery → the production DshEventFace.get assembly.
    const detail = await fetchPath(fake.routes[0]!, `session/${live.id}`)
    expect(detail.status).toBe(200)
    expect(get).toHaveBeenCalledWith(live.id)
    expect((detail.json.unified as { live: boolean }).live).toBe(true)
    expect(
      (detail.json.timeline as { entries: Array<{ seq: number }> }).entries.map(
        (entry) => entry.seq,
      ),
    ).toEqual([7])

    // A throwing/unavailable service never escapes through the route.
    serviceThrows = true
    const unavailable = await fetchPath(fake.routes[0]!, 'session/unavailable-live')
    expect(unavailable.status).toBe(404)

    // Once the lazy binding is disposed, its captured service is no longer
    // consulted and the holder continues with a sidecar-only generation.
    await fake.disposeEffect('agent-sidecar: fusion dsh feed release')
    const callsAfterDispose = get.mock.calls.length
    const afterDispose = await fetchPath(fake.routes[0]!, 'session/after-dispose')
    expect(afterDispose.status).toBe(404)
    expect(get).toHaveBeenCalledTimes(callsAfterDispose)
  })

  it('without the sessions service, dsh in-process events are simply not observed (no crash)', async () => {
    const fake = createFakeCtx()
    const runtimeDir = await tempRuntimeDir()
    apply(fake.ctx, Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir } }))
    cleanups.push(() => fake.disposeAll())

    // Nothing listens: the feed-less fusion must not have subscribed.
    fake.emit('session/created', { id: 'x', header: { createdAt: 1 }, events: [] })
    const detail = await fetchPath(fake.routes[0]!, 'session/x')
    expect(detail.status).toBe(404)
  })
})

// ---------------------------------------------------------------------------
// M3 analysis wiring (T5.10a): the engine reaches the routes with a
// fusion-assembled input, the analysis.enabled gate is read live, agents-less
// compositions degrade to 501, and dispose cancels in-flight sessions.
// ---------------------------------------------------------------------------

/** One `agents.create` call as observed by the analysis-path fake. */
interface RecordedCreate {
  sessionId: string
  agentOptions?: { provider?: string; model?: string; maxTokens?: number }
  meta?: { cwd?: string }
}

/**
 * Agents-registry fake for the analysis path: `create` returns a live
 * agent whose synchronous `followup` splice appends the user message plus
 * one canned assistant reply, so the engine's followup → whenIdle →
 * deriveMessages read-back observes a completed turn. Every create's
 * options are recorded so the A-1 agentOptions assembly is pinnable.
 */
function makeAnalysisAgents(): {
  agents: unknown
  followups: Array<{ content: Array<{ type: string; text?: string }>; source: unknown }>
  created: string[]
  createOptions: RecordedCreate[]
  disposed: string[]
} {
  const followups: Array<{ content: Array<{ type: string; text?: string }>; source: unknown }> = []
  const created: string[] = []
  const createOptions: RecordedCreate[] = []
  const disposed: string[] = []
  const agents = {
    get: () => undefined,
    resume: async (): Promise<never> => {
      throw new Error('resume must not be called by the analysis path')
    },
    create: async (options: RecordedCreate) => {
      created.push(options.sessionId)
      createOptions.push(options)
      const messages: Array<{ role: string; content: Array<{ type: string; text?: string }> }> = []
      return {
        agent: {
          session: { deriveMessages: () => messages, events: [] },
          followup: (message: {
            content: Array<{ type: string; text?: string }>
            source: unknown
          }) => {
            followups.push(message)
            messages.push({ role: 'user', content: [...message.content] })
            messages.push({
              role: 'assistant',
              content: [{ type: 'text', text: 'analysis says: all good' }],
            })
          },
          cancel: () => {},
          whenIdle: async () => {},
        },
        dispose: async () => {
          disposed.push(options.sessionId)
        },
      }
    },
  }
  return { agents, followups, created, createOptions, disposed }
}

/**
 * `ctx.agentDefaultModel` fake — the host default model selection the
 * analysis wiring falls back to when no explicit analysis.provider/model
 * is configured (same source dsh's headless/apiproxy entries read).
 */
const HOST_DEFAULT_MODEL = { provider: 'host-default-provider', model: 'host-default-model' }
const fakeDefaultModel = { currentSelection: () => ({ ...HOST_DEFAULT_MODEL }) }

describe('M3 analysis wiring (T5.10a)', () => {
  it('drives the engine with a fusion-assembled input for session and cross-agent targets', async () => {
    const { agents, followups, created } = makeAnalysisAgents()
    const fake = createFakeCtx({ agents, agentDefaultModel: fakeDefaultModel })
    const runtimeDir = await tempRuntimeDir()
    const freshRow = { ...DSH_SESSION_ROW, updated_at: Math.floor(Date.now() / 1000) }
    const server = await startMiniDaemon(join(runtimeDir, 'daemon.sock'), [freshRow])
    cleanups.push(
      () =>
        new Promise<void>((resolve) => {
          server.close(() => resolve())
        }),
    )
    apply(
      fake.ctx,
      Config({
        daemon: { policy: 'adopt-only' },
        sidecar: { runtimeDir },
        analysis: { enabled: true },
      }),
    )
    cleanups.push(() => fake.disposeAll())

    await vi.waitFor(
      async () => {
        const { json } = await fetchState(fake.routes[0]!)
        expect(json.board.sessions).toHaveLength(1)
      },
      { timeout: 3000, interval: 25 },
    )
    const route = fake.routes[0]!

    const requested = await postAction(route, {
      type: 'analysis.request',
      targetKind: 'session',
      targetId: 'sess-dsh-1',
      question: 'anything odd?',
    })
    expect(requested.status).toBe(200)
    expect(requested.json.outcome).toBe('completed')
    expect(requested.json.summary).toBe('analysis says: all good')
    expect(String(requested.json.analysisSessionId)).toMatch(/^agent-sidecar-analysis-/)
    expect(typeof requested.json.disclaimer).toBe('string')
    expect(created).toHaveLength(1)

    // The first followup message IS the primed prompt: read-only guidance
    // plus the fusion-assembled summary of the board row plus the question.
    const primedText = followups[0]!.content[0]!.text ?? ''
    expect(primedText).toContain('demo session') // title via fusion's unified view
    expect(primedText).toContain('[path]') // semantic payload redacts project paths
    expect(primedText).toContain('anything odd?') // question rides the head
    expect(followups[0]!.source).toEqual({ kind: 'plugin', plugin: 'agent-sidecar' })

    // Unknown session target: fusion resolves nothing → honest 404.
    const missing = await postAction(route, {
      type: 'analysis.request',
      targetKind: 'session',
      targetId: 'no-such-session',
    })
    expect(missing.status).toBe(404)
    expect(missing.json.reason).toBe('target_not_found')

    // Cross-agent overview also assembles from fusion (project groups).
    const cross = await postAction(route, {
      type: 'analysis.request',
      targetKind: 'cross-agent',
    })
    expect(cross.status).toBe(200)
    expect(created).toHaveLength(2)
    const crossText = followups[1]!.content[0]!.text ?? ''
    expect(crossText).toContain('[path]')
  })

  it('lists the session timeline newest-first in the analysis input (F5)', async () => {
    const { agents, followups } = makeAnalysisAgents()
    const fake = createFakeCtx({ sessions: {}, agents, agentDefaultModel: fakeDefaultModel })
    const runtimeDir = await tempRuntimeDir()
    apply(
      fake.ctx,
      Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir }, analysis: { enabled: true } }),
    )
    cleanups.push(() => fake.disposeAll())

    const live = {
      id: 'live-f5',
      header: { createdAt: Date.now() - 10_000, cwd: '/tmp/f5' },
      events: [
        { type: 'evt/older', seq: 0, time: Date.now() - 5_000, data: {} },
        { type: 'evt/newer', seq: 1, time: Date.now() - 1_000, data: {} },
      ],
    }
    fake.emit('session/created', live)
    fake.emit('session/event', live, live.events[0])
    fake.emit('session/event', live, live.events[1])

    const requested = await postAction(fake.routes[0]!, {
      type: 'analysis.request',
      targetKind: 'session',
      targetId: 'live-f5',
      question: 'what happened latest?',
    })
    expect(requested.status).toBe(200)
    const primedText = followups[0]!.content[0]!.text ?? ''
    // The engine's truncation keeps the HEAD: the question must ride the
    // head and the timeline must list newest events first, so overflow
    // sheds the oldest — least informative — lines (F5).
    const questionAt = primedText.indexOf('what happened latest?')
    const newerAt = primedText.indexOf('evt/newer')
    const olderAt = primedText.indexOf('evt/older')
    expect(questionAt).toBeGreaterThanOrEqual(0)
    expect(newerAt).toBeGreaterThanOrEqual(0)
    expect(olderAt).toBeGreaterThanOrEqual(0)
    expect(questionAt).toBeLessThan(newerAt)
    expect(newerAt).toBeLessThan(olderAt)
  })

  it('keeps the newest events, says how many it dropped, and stays under the char cap', async () => {
    const { agents, followups } = makeAnalysisAgents()
    const fake = createFakeCtx({ sessions: {}, agents, agentDefaultModel: fakeDefaultModel })
    const runtimeDir = await tempRuntimeDir()
    apply(
      fake.ctx,
      Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir }, analysis: { enabled: true } }),
    )
    cleanups.push(() => fake.disposeAll())

    // The daemon already snips event text at 120 chars, so this is the
    // worst line a real timeline can produce.
    const base = Date.now() - 600_000
    const events = Array.from({ length: 90 }, (_, i) => ({
      type: 'evt/tool',
      seq: i,
      time: base + i * 1_000,
      data: { text: `e${i} `.padEnd(120, 'x') },
    }))
    const live = {
      id: 'live-bounds',
      header: { createdAt: base, cwd: '/tmp/bounds' },
      events,
    }
    fake.emit('session/created', live)
    for (const event of events) fake.emit('session/event', live, event)

    const requested = await postAction(fake.routes[0]!, {
      type: 'analysis.request',
      targetKind: 'session',
      targetId: 'live-bounds',
      question: 'q'.repeat(4000),
    })
    expect(requested.status).toBe(200)
    const primedText = followups[0]!.content[0]!.text ?? ''

    // A digest the char cap had to amputate would drop events without
    // saying so, and the prompt would still call itself un-truncated.
    const listed = primedText.match(/- \[\d{4}-/gu) ?? []
    expect(listed).toHaveLength(24)
    // Fusion knows older events exist but not how many, so the digest says
    // that much and no more.
    expect(primedText).toContain('older events omitted')
    expect(primedText).not.toContain('已截断 / truncated')
    // Newest kept, oldest shed.
    expect(primedText).toContain('seq=89')
    expect(primedText).not.toContain('seq=0 ')
  })

  it('assembles agentOptions from the host default model selection (A-1 fallback)', async () => {
    const { agents, createOptions } = makeAnalysisAgents()
    const fake = createFakeCtx({ agents, agentDefaultModel: fakeDefaultModel })
    const runtimeDir = await tempRuntimeDir()
    // analysis.provider/model left at their '' defaults → fallback branch.
    apply(
      fake.ctx,
      Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir }, analysis: { enabled: true } }),
    )
    cleanups.push(() => fake.disposeAll())

    const requested = await postAction(fake.routes[0]!, {
      type: 'analysis.request',
      targetKind: 'cross-agent',
    })
    expect(requested.status).toBe(200)
    expect(requested.json.outcome).toBe('completed')
    expect(createOptions).toHaveLength(1)
    expect(createOptions[0]!.agentOptions).toEqual(HOST_DEFAULT_MODEL)
    // The persona's {{cwd}} variable reads session.header.cwd, populated
    // only through meta.cwd (headless-precedent create shape).
    expect(createOptions[0]!.meta?.cwd).toBe(process.cwd())
  })

  it('lets explicit analysis.provider/model config win over the host default (A-1)', async () => {
    const { agents, createOptions } = makeAnalysisAgents()
    const fake = createFakeCtx({ agents, agentDefaultModel: fakeDefaultModel })
    const runtimeDir = await tempRuntimeDir()
    apply(
      fake.ctx,
      Config({
        daemon: { policy: 'off' },
        sidecar: { runtimeDir },
        analysis: { enabled: true, provider: 'explicit-provider', model: 'explicit-model' },
      }),
    )
    cleanups.push(() => fake.disposeAll())

    const requested = await postAction(fake.routes[0]!, {
      type: 'analysis.request',
      targetKind: 'cross-agent',
    })
    expect(requested.status).toBe(200)
    expect(createOptions).toHaveLength(1)
    expect(createOptions[0]!.agentOptions).toEqual({
      provider: 'explicit-provider',
      model: 'explicit-model',
    })
  })

  it('pre-rejects analysis.request with 403 when no model source exists (A-1)', async () => {
    // agents bound but NO agentDefaultModel service and no explicit
    // analysis.provider/model: an agent created here would have no model
    // ({{model}} assembly error, empty summary) — the routes now refuse
    // honestly before any session is created.
    const { agents, created } = makeAnalysisAgents()
    const fake = createFakeCtx({ agents })
    const runtimeDir = await tempRuntimeDir()
    apply(
      fake.ctx,
      Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir }, analysis: { enabled: true } }),
    )
    cleanups.push(() => fake.disposeAll())

    const { status, json } = await postAction(fake.routes[0]!, {
      type: 'analysis.request',
      targetKind: 'cross-agent',
    })
    expect(status).toBe(403)
    expect(json.reason).toBe('analysis_model_unconfigured')
    expect(created).toHaveLength(0)

    // A partial explicit override (model without provider) is not a model
    // source either: the pair falls back, and with no host default the
    // request stays pre-rejected.
    const partial = createFakeCtx({ agents })
    apply(
      partial.ctx,
      Config({
        daemon: { policy: 'off' },
        sidecar: { runtimeDir },
        analysis: { enabled: true, model: 'model-without-provider' },
      }),
    )
    cleanups.push(() => partial.disposeAll())
    const partialReply = await postAction(partial.routes[0]!, {
      type: 'analysis.request',
      targetKind: 'cross-agent',
    })
    expect(partialReply.status).toBe(403)
    expect(partialReply.json.reason).toBe('analysis_model_unconfigured')
  })

  it('refuses analysis actions with 403 while analysis.enabled is false (default)', async () => {
    const { agents, created } = makeAnalysisAgents()
    const fake = createFakeCtx({ agents })
    const runtimeDir = await tempRuntimeDir()
    // inject.enabled=true on purpose: the inject gate must not open the
    // analysis gate (independent parallel gates).
    apply(
      fake.ctx,
      Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir }, inject: { enabled: true } }),
    )
    cleanups.push(() => fake.disposeAll())

    const { status, json } = await postAction(fake.routes[0]!, {
      type: 'analysis.request',
      targetKind: 'cross-agent',
    })
    expect(status).toBe(403)
    expect(json.reason).toBe('analysis_disabled')
    expect(created).toHaveLength(0)
  })

  it('degrades analysis honestly to 501 without the agents service', async () => {
    // Default fake ctx = composition without dsh-agent: the plugin loads,
    // the gate passes, and the availability probe answers 501 (no crash).
    const fake = createFakeCtx()
    const runtimeDir = await tempRuntimeDir()
    apply(
      fake.ctx,
      Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir }, analysis: { enabled: true } }),
    )
    cleanups.push(() => fake.disposeAll())

    const { status, json } = await postAction(fake.routes[0]!, {
      type: 'analysis.request',
      targetKind: 'cross-agent',
    })
    expect(status).toBe(501)
    expect(json.reason).toBe('analysis_unavailable')
  })

  it('reads the analysis gate live through a settings commit', async () => {
    const settings = makeFakeSettings()
    const { agents } = makeAnalysisAgents()
    const fake = createFakeCtx({
      settings: settings.service,
      agents,
      agentDefaultModel: fakeDefaultModel,
    })
    const runtimeDir = await tempRuntimeDir()
    const offConfig = Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir } })
    apply(fake.ctx, offConfig)
    cleanups.push(() => fake.disposeAll())
    const route = fake.routes[0]!
    const envelope = { type: 'analysis.request', targetKind: 'cross-agent' }

    const closed = await postAction(route, envelope)
    expect(closed.status).toBe(403)
    expect(closed.json.reason).toBe('analysis_disabled')

    // Runtime flip to true: the gate opens immediately and the request
    // reaches the REAL engine + agents fake end-to-end.
    settings.commit(
      Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir }, analysis: { enabled: true } }),
    )
    const open = await postAction(route, envelope)
    expect(open.status).toBe(200)
    expect(open.json.outcome).toBe('completed')

    // …and back to false: closed again, no restart.
    settings.commit(offConfig)
    const reclosed = await postAction(route, envelope)
    expect(reclosed.status).toBe(403)
    expect(reclosed.json.reason).toBe('analysis_disabled')
  })

  it('cancels in-flight analysis sessions when the plugin disposes', async () => {
    const { agents, created, disposed } = makeAnalysisAgents()
    const fake = createFakeCtx({ agents, agentDefaultModel: fakeDefaultModel })
    const runtimeDir = await tempRuntimeDir()
    apply(
      fake.ctx,
      Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir }, analysis: { enabled: true } }),
    )

    const requested = await postAction(fake.routes[0]!, {
      type: 'analysis.request',
      targetKind: 'cross-agent',
    })
    expect(requested.status).toBe(200)
    expect(created).toHaveLength(1)
    // Completed requests keep the session alive for follow-ups…
    expect(disposed).toHaveLength(0)

    // …until the plugin unloads: the effect disposer stops what is left.
    await fake.disposeAll()
    expect(disposed).toEqual(created)
  })
})

describe('settings wiring (M2 review F-1)', () => {
  it('declares applies=live and flips both write gates on a runtime watch commit', async () => {
    const settings = makeFakeSettings()
    const fake = createFakeCtx({ settings: settings.service })
    const runtimeDir = await tempRuntimeDir()
    const offConfig = Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir } })
    apply(fake.ctx, offConfig)
    cleanups.push(() => fake.disposeAll())

    expect(settings.registrations).toEqual([{ ns: 'agent-sidecar', applies: 'live' }])

    const prepareBody = {
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 'missing' },
      mode: 'queue',
      message: 'ping',
    }
    const executeBody = {
      type: 'inject.execute',
      requestId: 'req-x',
      confirmToken: 'f'.repeat(32),
      message: 'ping',
    }
    const route = fake.routes[0]!

    // inject.enabled=false at entry: both endpoints refuse at the write gate.
    const closedPrepare = await postAction(route, prepareBody)
    expect(closedPrepare.status).toBe(403)
    expect(closedPrepare.json.reason).toBe('inject_disabled')
    const closedExecute = await postAction(route, executeBody)
    expect(closedExecute.status).toBe(403)
    expect(closedExecute.json.reason).toBe('inject_disabled')

    // Runtime flip to true: both endpoints now pass the gate and reach the
    // REAL gateway, failing on its own later checks — proof the 403 gate
    // read the live value.
    settings.commit(
      Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir }, inject: { enabled: true } }),
    )
    const openPrepare = await postAction(route, prepareBody)
    expect(openPrepare.status).toBe(404)
    expect(openPrepare.json.reason).toBe('target_not_found')
    const openExecute = await postAction(route, executeBody)
    expect(openExecute.status).toBe(401)
    expect(openExecute.json.errorCode).toBe('token_missing')

    // …and back to false: the gate closes just as immediately.
    const reclosedPrepare = await postAction(route, prepareBody)
    settings.commit(offConfig)
    const finalPrepare = await postAction(route, prepareBody)
    expect(reclosedPrepare.status).toBe(404) // still open right before the commit
    expect(finalPrepare.status).toBe(403)
    expect(finalPrepare.json.reason).toBe('inject_disabled')
    const finalExecute = await postAction(route, executeBody)
    expect(finalExecute.status).toBe(403)
    expect(finalExecute.json.reason).toBe('inject_disabled')
  })

  it('names the settings keys it bakes at apply time instead of ignoring them', async () => {
    // The daemon/sidecar/stream values are read once, while assembling the
    // client and supervisor. A settings edit to those never takes effect —
    // not even after a plugin reload, since the reload reads the entry config
    // again — so the plugin has to say which keys it dropped and where they
    // do belong, rather than accept an edit that does nothing.
    const settings = makeFakeSettings()
    const fake = createFakeCtx({ settings: settings.service })
    const runtimeDir = await tempRuntimeDir()
    const entry = Config({ daemon: { policy: 'off' }, sidecar: { runtimeDir } })
    apply(fake.ctx, entry)
    cleanups.push(() => fake.disposeAll())

    const before = fake.logs.length
    settings.commit(Config({ ...entry, inject: { enabled: true } }))
    expect(fake.logs.slice(before).join('\n')).not.toMatch(/apply time/)

    settings.commit(
      Config({
        daemon: { policy: 'adopt-only', backoffLimit: 7 },
        sidecar: { runtimeDir: `${runtimeDir}-elsewhere`, command: ['other-sidecar'] },
        stream: { reconcileIdleMs: 30000 },
      }),
    )
    const warned = fake.logs.slice(before).join('\n')
    expect(warned).toMatch(/apply time/)
    for (const key of [
      'daemon.policy',
      'daemon.backoffLimit',
      'sidecar.command',
      'sidecar.runtimeDir',
      'stream.reconcileIdleMs',
    ]) {
      expect(warned).toContain(key)
    }
    // The live keys are honoured, so naming them here would be the opposite
    // mistake: telling a user an edit was dropped when it was applied.
    expect(warned).not.toContain('inject.enabled')
  })
})
