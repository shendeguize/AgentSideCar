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
  /** Run collected effect disposers in reverse registration order. */
  disposeAll(): Promise<void>
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
  const noop = (): void => {}
  const logger = Object.assign(() => logger, {
    info: noop,
    warn: noop,
    error: noop,
    debug: noop,
  })
  const ctx = {
    logger,
    effect(execute: () => () => unknown, _label?: string) {
      const disposer = execute()
      disposers.push(disposer)
      return disposer
    },
    inject(deps: string[], callback: (ctx: unknown) => void) {
      if (!deps.every((dep) => dep in services)) return undefined
      const injected = Object.assign(Object.create(ctx as object), services)
      callback(injected)
      return undefined
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
    disposeAll: async () => {
      for (const disposer of [...disposers].reverse()) {
        await disposer()
      }
      disposers.length = 0
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
    expect(config.ui.timeWindowHours).toBe(24)
    expect(config.ui.showDead).toBe(false)
    expect(config.skill.provide).toBe(false)
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

  it('loads without the agents service and degrades the dsh path honestly', async () => {
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

    // prepare is store-backed (agents-free) and still issues a confirmation…
    const prepared = await postAction(fake.routes[0]!, {
      type: 'inject.prepare',
      target: { agent: 'dsh', sessionId: 'sess-dsh-1' },
      mode: 'queue',
      message: 'ping',
    })
    expect(prepared.status).toBe(200)
    expect(typeof prepared.json.confirmToken).toBe('string')

    // …and a dsh-target execute fails honestly (agents unavailable → resume
    // rejects) instead of pending or crashing the route.
    const executed = await postAction(fake.routes[0]!, {
      type: 'inject.execute',
      requestId: prepared.json.requestId,
      confirmToken: prepared.json.confirmToken,
      message: 'ping',
    })
    expect(executed.status).toBe(502)
    expect(executed.json.outcome).toBe('failed')
    expect(executed.json.errorCode).toBe('session_not_found')
    expect(String(executed.json.detail)).toContain('not available')
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
    const fake = createFakeCtx({ agents })
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
  })
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
})
