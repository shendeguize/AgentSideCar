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

function createFakeCtx(): FakeCtx {
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
          stdout: undefined,
          stderr: undefined,
          collected: {},
          done: new Promise<never>(() => {}),
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
 */
function startMiniDaemon(socketPath: string): Promise<Server> {
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
        socket.end(`${JSON.stringify({ ok: true, op: 'status', sessions: [] })}\n`)
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

  it('leaves no timers behind once every disposer has run', async () => {
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
