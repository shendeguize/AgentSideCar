/**
 * dsh session dispose: the executor over the optional host sessions
 * member, and the `dsh.dispose` slice of the action dispatcher.
 *
 * The behaviours worth pinning are all about refusing to pretend:
 * - a host without a dispose member is `unsupported`, never a thrown call
 *   at the moment the operator confirms;
 * - host error text never reaches the response (S8) — outcomes are a fixed
 *   vocabulary;
 * - unlike `archive.*`, dispose DOES sit behind the inject write gate: it
 *   ends a live session, which is the largest agent-state mutation on offer.
 */

import { createServer, request as httpRequest, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createDshDisposer,
  type SessionsDisposeFace,
} from '../src/dsh-dispose.ts'
import { SessionStore } from '../src/session-store.ts'
import { DaemonSupervisor } from '../src/supervisor.ts'
import {
  API_PREFIX,
  createRoutes,
  type DshDisposeApi,
  type InjectGatewayApi,
  type Routes,
} from '../src/routes.ts'

const UNUSED_GATEWAY: InjectGatewayApi = {
  prepare: () => {
    throw new Error('inject must not be reached in dispose route tests')
  },
  execute: () => {
    throw new Error('inject must not be reached in dispose route tests')
  },
}

function makeSupervisor(): DaemonSupervisor {
  return new DaemonSupervisor(
    {
      ping: async () => null,
      spawnDaemon: () => {
        throw new Error('spawn must not be reached in dispose route tests')
      },
      detectLaunchAgent: async () => false,
      log: () => {},
    },
    { policy: 'off' },
  )
}

// ---------------------------------------------------------------------------
// executor
// ---------------------------------------------------------------------------

describe('dsh disposer', () => {
  it('reports unsupported when the host sessions service has no dispose', async () => {
    const service: SessionsDisposeFace = { get: () => ({}) }
    const disposer = createDshDisposer({ resolve: () => service })

    expect(disposer.available()).toBe(false)
    expect(await disposer.dispose('s1')).toEqual({ outcome: 'unsupported', sessionId: 's1' })
  })

  it('reports unsupported while no sessions generation is bound', async () => {
    const disposer = createDshDisposer({ resolve: () => null })

    expect(disposer.available()).toBe(false)
    expect((await disposer.dispose('s1')).outcome).toBe('unsupported')
  })

  it('re-resolves the service per call, so a swapped generation is honoured', async () => {
    const first = vi.fn()
    const second = vi.fn()
    let current: SessionsDisposeFace = { dispose: first }
    const disposer = createDshDisposer({ resolve: () => current })

    await disposer.dispose('s1')
    current = { dispose: second }
    await disposer.dispose('s2')

    expect(first).toHaveBeenCalledExactlyOnceWith('s1')
    expect(second).toHaveBeenCalledExactlyOnceWith('s2')
  })

  it('awaits an async dispose and reports the session as ended', async () => {
    const disposer = createDshDisposer({
      resolve: () => ({ dispose: () => Promise.resolve() }),
    })

    expect(disposer.available()).toBe(true)
    expect(await disposer.dispose('s1')).toEqual({ outcome: 'disposed', sessionId: 's1' })
  })

  it('answers not_found for a session the host no longer knows', async () => {
    const dispose = vi.fn()
    const disposer = createDshDisposer({
      resolve: () => ({ dispose, get: () => undefined }),
    })

    // Already gone is the state the operator asked for, and disposing an
    // unknown id would only invite a host-side throw.
    expect(await disposer.dispose('gone')).toEqual({ outcome: 'not_found', sessionId: 'gone' })
    expect(dispose).not.toHaveBeenCalled()
  })

  it('still disposes when the host exposes no lookup to pre-check with', async () => {
    const dispose = vi.fn()
    const disposer = createDshDisposer({ resolve: () => ({ dispose }) })

    expect((await disposer.dispose('s1')).outcome).toBe('disposed')
    expect(dispose).toHaveBeenCalledExactlyOnceWith('s1')
  })

  it('keeps host error text out of the outcome and out of the log meta', async () => {
    const seen: Array<Record<string, unknown> | undefined> = []
    const disposer = createDshDisposer({
      resolve: () => ({
        dispose: () => {
          throw new Error('/Users/someone/.dsh/sessions/secret-prompt.json is locked')
        },
      }),
      log: (_level, _msg, meta) => { seen.push(meta) },
    })

    expect(await disposer.dispose('s1')).toEqual({ outcome: 'failed', sessionId: 's1' })
    expect(JSON.stringify(seen)).not.toContain('secret-prompt')
    expect(seen[0]).toEqual({ error: 'Error' })
  })

  it('bounds a hanging host call and calls the result a timeout, not a failure', async () => {
    vi.useFakeTimers()
    try {
      const disposer = createDshDisposer({
        resolve: () => ({ dispose: () => new Promise<void>(() => {}) }),
        timeoutMs: 1000,
      })

      const pending = disposer.dispose('s1')
      await vi.advanceTimersByTimeAsync(1000)

      // The session may well still be alive; "timeout" says so, where
      // "failed" would tell the operator to stop checking.
      expect(await pending).toEqual({ outcome: 'timeout', sessionId: 's1' })
    } finally {
      vi.useRealTimers()
    }
  })

  it('treats a throwing resolver as an absent capability', async () => {
    const disposer = createDshDisposer({
      resolve: () => { throw new Error('service departed mid-lookup') },
    })

    expect(disposer.available()).toBe(false)
    expect((await disposer.dispose('s1')).outcome).toBe('unsupported')
  })

  it('rejects a blank session id as a programming error, not an outcome', async () => {
    const disposer = createDshDisposer({ resolve: () => ({ dispose: () => {} }) })

    await expect(disposer.dispose('')).rejects.toThrowError(RangeError)
  })
})

// ---------------------------------------------------------------------------
// route
// ---------------------------------------------------------------------------

interface Harness {
  base: URL
  routes: Routes
  server: Server
  inject: { enabled: boolean }
  calls: string[]
  close(): Promise<void>
}

const harnesses: Harness[] = []

afterEach(async () => {
  for (const harness of harnesses.splice(0)) await harness.close()
})

async function startHarness(
  init: { withDispose?: boolean; available?: boolean; injectEnabled?: boolean } = {},
): Promise<Harness> {
  const supervisor = makeSupervisor()
  const inject = { enabled: init.injectEnabled ?? true }
  const calls: string[] = []
  const dispose: DshDisposeApi = {
    available: () => init.available ?? true,
    dispose: async (sessionId) => {
      calls.push(sessionId)
      return { outcome: 'disposed', sessionId }
    },
  }
  const routes = createRoutes({
    store: new SessionStore(),
    supervisor,
    guardOptions: { allowWriteActions: () => inject.enabled },
    injectGateway: UNUSED_GATEWAY,
    ...(init.withDispose === false ? {} : { dispose }),
    log: () => {},
  })
  const server = createServer((req, res) => {
    routes.handle(req, res).catch(() => {
      if (!res.headersSent) {
        res.writeHead(400)
        res.end()
      }
    })
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address() as AddressInfo
  const harness: Harness = {
    base: new URL(`http://127.0.0.1:${port}`),
    routes,
    server,
    inject,
    calls,
    close: async () => {
      routes.dispose()
      await new Promise<void>((resolve) => {
        server.close(() => resolve())
        server.closeAllConnections()
      })
      await supervisor.stop()
    },
  }
  harnesses.push(harness)
  return harness
}

function post(
  base: URL,
  path: string,
  envelope: unknown,
): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      {
        host: base.hostname,
        port: base.port,
        path: `${API_PREFIX}${path}`,
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        agent: false,
      },
      (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk: string) => { body += chunk })
        res.on('end', () => resolve({ status: res.statusCode ?? 0, body }))
      },
    )
    req.on('error', reject)
    req.write(JSON.stringify(envelope))
    req.end()
  })
}

function get(base: URL, path: string): Promise<{ status: number; body: string }> {
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      {
        host: base.hostname,
        port: base.port,
        path: `${API_PREFIX}${path}`,
        method: 'GET',
        agent: false,
      },
      (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk: string) => { body += chunk })
        res.on('end', () => resolve({ status: res.statusCode ?? 0, body }))
      },
    )
    req.on('error', reject)
    req.end()
  })
}

describe('POST action dsh.dispose', () => {
  it('forwards the session id and answers with the outcome', async () => {
    const harness = await startHarness()

    const reply = await post(harness.base, '/action', {
      type: 'dsh.dispose',
      sessionId: 'sess-1',
    })

    expect(reply.status).toBe(200)
    expect(JSON.parse(reply.body)).toEqual({ outcome: 'disposed' })
    expect(harness.calls).toEqual(['sess-1'])
  })

  it('sits behind the inject write gate, unlike the archive actions', async () => {
    const harness = await startHarness({ injectEnabled: false })

    const reply = await post(harness.base, '/action', {
      type: 'dsh.dispose',
      sessionId: 'sess-1',
    })

    expect(reply.status).toBe(403)
    expect(harness.calls).toEqual([])
  })

  it('answers 501 when no sessions service can end anything', async () => {
    const absent = await startHarness({ withDispose: false })
    expect((await post(absent.base, '/action', { type: 'dsh.dispose', sessionId: 's' })).status)
      .toBe(501)

    // Bound but currently incapable (service departed) reads the same way.
    const incapable = await startHarness({ available: false })
    const reply = await post(incapable.base, '/action', { type: 'dsh.dispose', sessionId: 's' })
    expect(reply.status).toBe(501)
    expect(JSON.parse(reply.body)).toEqual({ reason: 'dispose_unavailable' })
    expect(incapable.calls).toEqual([])
  })

  it('rejects a blank or missing session id before the host is touched', async () => {
    const harness = await startHarness()

    for (const envelope of [
      { type: 'dsh.dispose' },
      { type: 'dsh.dispose', sessionId: '' },
      { type: 'dsh.dispose', sessionId: 42 },
    ]) {
      const reply = await post(harness.base, '/action', envelope)
      expect(reply.status).toBe(400)
    }
    expect(harness.calls).toEqual([])
  })
})

describe('GET state dispose capability', () => {
  it('is true only with an available executor behind an open write gate', async () => {
    const open = await startHarness()
    expect(JSON.parse((await get(open.base, '/state')).body).capabilities)
      .toEqual({ inject: true, dispose: true })

    // Same executor, closed gate: the UI must not offer a 403.
    const gated = await startHarness({ injectEnabled: false })
    expect(JSON.parse((await get(gated.base, '/state')).body).capabilities)
      .toEqual({ inject: false, dispose: false })

    const absent = await startHarness({ withDispose: false })
    expect(JSON.parse((await get(absent.base, '/state')).body).capabilities)
      .toEqual({ inject: true, dispose: false })
  })
})
