/**
 * Unit tests for the host-half sidecar bridge (T1.2).
 *
 * A mock daemon speaking the real wire protocol (verified against
 * sidecar/daemon.py) runs on a Unix socket in a temp directory:
 * single-line JSON requests, one JSONL response per ping/status,
 * subscribe ack followed by a JSONL event stream.
 */

import { mkdtempSync, rmSync } from 'node:fs'
import { createServer, type Server, type Socket } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  Reconciler,
  SidecarDaemonError,
  SidecarSocketClient,
  type DropReason,
  type ReconcilerClient,
  type SessionRow,
  type SidecarEvent,
  type StatusSnapshot,
  type SubscribeHandlers,
  type Subscription,
} from '../src/bridge.ts'
import { SessionStore } from '../src/session-store.ts'

// ---------------------------------------------------------------------------
// Helpers.
// ---------------------------------------------------------------------------

function deferred<T = void>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => {
    resolve = r
  })
  return { promise, resolve }
}

function row(overrides: Partial<SessionRow> = {}): SessionRow {
  return {
    agent: 'claude',
    session_id: 's1',
    project: '/tmp/project',
    transcript: '/tmp/project/transcript.jsonl',
    updated_at: 100,
    title: 'demo session',
    status: 'idle',
    extra: {},
    parent_id: null,
    ...overrides,
  }
}

function ev(overrides: Partial<SidecarEvent> = {}): SidecarEvent {
  return {
    ts: '2026-08-24T00:00:00+00:00',
    agent: 'claude',
    session_id: 's1',
    kind: 'assistant',
    text: 'hello',
    extra: {},
    ...overrides,
  }
}

interface MockDaemonOptions {
  pingBehavior?: 'reply' | 'silent'
  statusBehavior?: 'reply' | 'error'
  statusSessions?: unknown[]
  /**
   * Per-request `replay` behavior: an object is written as the response
   * line, `'silent'` never answers (timeout path), `'close'` half-closes
   * without a response. Unset → the connection is destroyed (like an old
   * daemon that knows no replay op).
   */
  replayHandler?: (request: Record<string, unknown>) => unknown
}

interface MockDaemon {
  socketPath: string
  subscribeRequests: Array<Record<string, unknown>>
  replayRequests: Array<Record<string, unknown>>
  pushEvent(event: unknown): void
  pushRaw(raw: string): void
  killSubscribers(): void
  close(): Promise<void>
}

/** Mock daemon mirroring sidecar/daemon.py request handling. */
async function startMockDaemon(opts: MockDaemonOptions = {}): Promise<MockDaemon> {
  const dir = mkdtempSync(join(tmpdir(), 'sidecar-'))
  const socketPath = join(dir, 'daemon.sock')
  const connections = new Set<Socket>()
  const subscribers = new Set<Socket>()
  const subscribeRequests: Array<Record<string, unknown>> = []
  const replayRequests: Array<Record<string, unknown>> = []

  const server: Server = createServer((socket) => {
    connections.add(socket)
    socket.on('error', () => {})
    socket.on('close', () => {
      connections.delete(socket)
      subscribers.delete(socket)
    })
    let pending = ''
    socket.on('data', (chunk) => {
      pending += chunk.toString('utf8')
      let idx: number
      while ((idx = pending.indexOf('\n')) >= 0) {
        const line = pending.slice(0, idx)
        pending = pending.slice(idx + 1)
        let request: unknown
        try {
          request = JSON.parse(line)
        } catch {
          continue
        }
        const op =
          typeof request === 'object' && request !== null
            ? (request as Record<string, unknown>)['op']
            : undefined
        if (op === 'ping') {
          if ((opts.pingBehavior ?? 'reply') === 'reply') {
            socket.write(
              JSON.stringify({
                ok: true,
                op: 'ping',
                pid: 4242,
                version: '9.9.9',
                http: { enabled: false },
              }) + '\n',
            )
          }
        } else if (op === 'status') {
          if ((opts.statusBehavior ?? 'reply') === 'error') {
            socket.write(
              JSON.stringify({ ok: false, error: { code: 'daemon_error', message: 'boom' } }) +
                '\n',
            )
          } else {
            socket.write(
              JSON.stringify({
                ok: true,
                op: 'status',
                sessions: opts.statusSessions ?? [],
                scan_errors: [],
                tail_errors: [],
                diagnostics: [],
              }) + '\n',
            )
          }
        } else if (op === 'subscribe') {
          const envelope = request as Record<string, unknown>
          subscribeRequests.push(envelope)
          subscribers.add(socket)
          // Mirror the daemon ack: echo a sorted agents filter when given.
          const agents = envelope['agents']
          socket.write(
            JSON.stringify({
              ok: true,
              op: 'subscribe',
              ...(Array.isArray(agents) ? { agents: [...agents].sort() } : {}),
            }) + '\n',
          )
        } else if (op === 'replay') {
          const envelope = request as Record<string, unknown>
          replayRequests.push(envelope)
          const behavior = opts.replayHandler?.(envelope)
          if (behavior === 'silent') {
            // no response: the client's replay timeout owns this path
          } else if (behavior === 'close') {
            socket.end()
          } else if (behavior === undefined) {
            socket.destroy()
          } else {
            socket.write(JSON.stringify(behavior) + '\n')
          }
        }
      }
    })
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(socketPath, () => resolve())
  })

  return {
    socketPath,
    subscribeRequests,
    replayRequests,
    pushEvent(event: unknown): void {
      for (const socket of subscribers) socket.write(JSON.stringify(event) + '\n')
    },
    pushRaw(raw: string): void {
      for (const socket of subscribers) socket.write(raw)
    },
    killSubscribers(): void {
      for (const socket of subscribers) socket.destroy()
    },
    async close(): Promise<void> {
      for (const socket of connections) socket.destroy()
      await new Promise<void>((resolve) => server.close(() => resolve()))
      rmSync(dir, { recursive: true, force: true })
    },
  }
}

/** Deterministic ReconcilerClient stub (fake-timer friendly, no sockets). */
class StubClient implements ReconcilerClient {
  statusCalls = 0
  subscribeCalls = 0
  sessions: SessionRow[] = []
  /** Simulates "daemon socket absent / not ready": status resolves null. */
  failStatus = false
  autoReady = true
  handlers: SubscribeHandlers | null = null

  async status(): Promise<StatusSnapshot | null> {
    this.statusCalls += 1
    if (this.failStatus) return null
    return { sessions: this.sessions, scanErrors: [], tailErrors: [], diagnostics: [] }
  }

  subscribe(handlers: SubscribeHandlers): Subscription {
    this.subscribeCalls += 1
    this.handlers = handlers
    if (this.autoReady) {
      queueMicrotask(() => handlers.onReady?.())
    } else {
      queueMicrotask(() => handlers.onClose?.(new Error('connection refused')))
    }
    return {
      close: () => {
        queueMicrotask(() => handlers.onClose?.())
      },
    }
  }
}

// ---------------------------------------------------------------------------
// SidecarSocketClient (real sockets, real timers).
// ---------------------------------------------------------------------------

describe('SidecarSocketClient', () => {
  let daemon: MockDaemon | null = null

  afterEach(async () => {
    if (daemon !== null) {
      await daemon.close()
      daemon = null
    }
  })

  it('ping returns typed daemon info on success', async () => {
    daemon = await startMockDaemon()
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    expect(await client.ping()).toEqual({
      pid: 4242,
      version: '9.9.9',
      http: { enabled: false },
    })
  })

  it('ping resolves null when the daemon never answers (timeout)', async () => {
    daemon = await startMockDaemon({ pingBehavior: 'silent' })
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath, timeoutMs: 200 })
    expect(await client.ping()).toBeNull()
  })

  it('ping resolves null when the connection is refused', async () => {
    const client = new SidecarSocketClient({
      socketPath: join(tmpdir(), `sidecar-absent-${process.pid}-${Date.now()}.sock`),
      timeoutMs: 200,
    })
    expect(await client.ping()).toBeNull()
  })

  it('status parses the sessions snapshot', async () => {
    daemon = await startMockDaemon({
      statusSessions: [
        row({ session_id: 'a', status: 'working', updated_at: 100 }),
        {
          agent: 'dsh',
          session_id: 'b',
          project: '/tmp/other',
          transcript: '/tmp/other/session.jsonl.zstd',
          updated_at: 200.5,
          title: 'dsh run',
          status: 'waiting',
          extra: { seq: 7 },
          parent_id: 'root',
        },
      ],
    })
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    const snapshot = await client.status()
    expect(snapshot).not.toBeNull()
    expect(snapshot?.sessions).toHaveLength(2)
    expect(snapshot?.sessions[1]).toEqual({
      agent: 'dsh',
      session_id: 'b',
      project: '/tmp/other',
      transcript: '/tmp/other/session.jsonl.zstd',
      updated_at: 200.5,
      title: 'dsh run',
      status: 'waiting',
      extra: { seq: 7 },
      parent_id: 'root',
    })
    expect(snapshot?.scanErrors).toEqual([])
    expect(snapshot?.tailErrors).toEqual([])
  })

  it('keeps raw daemon local/remote provenance through eligibility only', async () => {
    daemon = await startMockDaemon({
      statusSessions: [
        row({ session_id: 'plain-local' }),
        row({ session_id: 'fleet-local', host: 'local' }),
        row({ session_id: 'fleet-remote', host: 'edge-a' }),
        row({ session_id: 'flag-remote', extra: { remote: true } }),
      ],
    })
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    const snapshot = await client.status()
    expect(snapshot?.sessions).toHaveLength(4)

    const store = new SessionStore()
    store.applySnapshot(snapshot?.sessions ?? [])
    const views = new Map(
      store.getBoardState().sessions.map((session) => [session.session_id, session]),
    )
    expect(views.get('plain-local')?.inject_eligibility).toEqual({
      allowed: true,
      reason: 'eligible',
    })
    expect(views.get('fleet-local')?.inject_eligibility).toEqual({
      allowed: true,
      reason: 'eligible',
    })
    for (const sessionId of ['fleet-remote', 'flag-remote']) {
      expect(views.get(sessionId)?.inject_eligibility).toEqual({
        allowed: false,
        reason: 'remote_session',
      })
    }
    for (const view of views.values()) {
      expect(view).not.toHaveProperty('extra')
      expect(view).not.toHaveProperty('parent_id')
      expect(view).not.toHaveProperty('host')
    }
  })

  it('retains malformed injection fields as disabled board rows', async () => {
    const base = row()
    daemon = await startMockDaemon({
      statusSessions: [
        { ...base, session_id: 'unknown-status', status: 'paused' },
        { ...base, session_id: 'malformed-status', status: 7 },
        { ...base, session_id: 'malformed-extra', extra: [] },
        { ...base, session_id: 'malformed-parent', parent_id: { id: 'secret' } },
      ],
    })
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    const snapshot = await client.status()
    expect(snapshot?.sessions.map((session) => session.session_id)).toEqual([
      'unknown-status',
      'malformed-status',
      'malformed-extra',
      'malformed-parent',
    ])

    const store = new SessionStore()
    store.applySnapshot(snapshot?.sessions ?? [])
    const views = store.getBoardState().sessions
    expect(views).toHaveLength(4)
    expect(views.find((view) => view.session_id === 'unknown-status')?.status).toBe('paused')
    expect(views.find((view) => view.session_id === 'malformed-status')?.status).toBe(
      '<invalid>',
    )
    for (const view of views) {
      expect(view.inject_eligibility).toEqual({
        allowed: false,
        reason: 'invalid_session',
      })
      expect(view).not.toHaveProperty('extra')
      expect(view).not.toHaveProperty('parent_id')
    }
  })

  it('status resolves null on a daemon-declared error', async () => {
    daemon = await startMockDaemon({ statusBehavior: 'error' })
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    expect(await client.status()).toBeNull()
  })

  it('subscribe delivers events and reports disconnect via onClose', async () => {
    daemon = await startMockDaemon()
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    const events: SidecarEvent[] = []
    let closeCalls = 0
    const ready = deferred()
    const gotTwo = deferred()
    const closed = deferred()

    const subscription = client.subscribe({
      onReady: () => ready.resolve(),
      onEvent: (event) => {
        events.push(event)
        if (events.length === 2) gotTwo.resolve()
      },
      onClose: () => {
        closeCalls += 1
        closed.resolve()
      },
    })

    await ready.promise
    daemon.pushEvent(ev({ ts: 't1', kind: 'assistant', text: 'hello' }))
    daemon.pushEvent(ev({ ts: 't2', kind: 'tool_call', text: 'ls -la', extra: { seq: 2 } }))
    await gotTwo.promise

    expect(events[0]).toEqual(ev({ ts: 't1', kind: 'assistant', text: 'hello' }))
    expect(events[1]?.extra).toEqual({ seq: 2 })

    daemon.killSubscribers()
    await closed.promise
    subscription.close()
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(closeCalls).toBe(1)
  })

  it('replay returns a parsed page and forwards session/cursor/limit on the wire', async () => {
    daemon = await startMockDaemon({
      replayHandler: (request) => ({
        ok: true,
        op: 'replay',
        session_id: request['session_id'],
        agent: 'dsh',
        after_seq: request['after_seq'],
        events: [
          ev({ agent: 'dsh', kind: 'user', text: 'ask', extra: { seq: 3 } }),
          ev({ agent: 'dsh', kind: 'assistant', text: 'answer', extra: { seq: 4 } }),
        ],
        count: 2,
        last_seq: 4,
        truncated: true,
      }),
    })
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    const page = await client.replay('s1', 2, 2)

    expect(daemon.replayRequests).toEqual([
      { op: 'replay', session_id: 's1', after_seq: 2, limit: 2 },
    ])
    expect(page.sessionId).toBe('s1')
    expect(page.agent).toBe('dsh')
    expect(page.afterSeq).toBe(2)
    expect(page.events.map((e) => e.text)).toEqual(['ask', 'answer'])
    expect(page.count).toBe(2)
    expect(page.lastSeq).toBe(4)
    expect(page.truncated).toBe(true)
  })

  it('replay omits the limit field when unset and defaults after_seq to 0', async () => {
    daemon = await startMockDaemon({
      replayHandler: (request) => ({
        ok: true,
        op: 'replay',
        session_id: request['session_id'],
        agent: 'claude',
        after_seq: 0,
        events: [],
        count: 0,
        last_seq: null,
        truncated: false,
      }),
    })
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    const page = await client.replay('s1')

    expect(daemon.replayRequests).toEqual([{ op: 'replay', session_id: 's1', after_seq: 0 }])
    expect(page.events).toEqual([])
    expect(page.lastSeq).toBeNull()
    expect(page.truncated).toBe(false)
  })

  it('replay rejects with the daemon error code verbatim', async () => {
    daemon = await startMockDaemon({
      replayHandler: () => ({
        ok: false,
        error: { code: 'unknown_session', message: 'no session nope' },
      }),
    })
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    const failure = await client.replay('nope').then(
      () => null,
      (err: unknown) => err,
    )
    expect(failure).toBeInstanceOf(SidecarDaemonError)
    expect((failure as SidecarDaemonError).code).toBe('unknown_session')
    expect((failure as SidecarDaemonError).message).toContain('no session nope')
  })

  it('replay rejects with coded transport errors (refused / closed / timeout / bad payload)', async () => {
    const codeOf = async (promise: Promise<unknown>): Promise<string> =>
      promise.then(
        () => 'resolved',
        (err: unknown) => (err instanceof SidecarDaemonError ? err.code : 'other'),
      )

    const refused = new SidecarSocketClient({
      socketPath: join(tmpdir(), `sidecar-absent-${process.pid}-${Date.now()}.sock`),
      replayTimeoutMs: 200,
    })
    expect(await codeOf(refused.replay('s1'))).toBe('connection_failed')

    daemon = await startMockDaemon({ replayHandler: () => 'close' })
    const closed = new SidecarSocketClient({ socketPath: daemon.socketPath })
    expect(await codeOf(closed.replay('s1'))).toBe('connection_closed')
    await daemon.close()

    daemon = await startMockDaemon({ replayHandler: () => 'silent' })
    const silent = new SidecarSocketClient({
      socketPath: daemon.socketPath,
      replayTimeoutMs: 200,
    })
    expect(await codeOf(silent.replay('s1'))).toBe('timeout')
    await daemon.close()

    daemon = await startMockDaemon({
      replayHandler: () => ({ ok: true, op: 'replay', session_id: 's1', agent: 'dsh', events: 'nope' }),
    })
    const malformed = new SidecarSocketClient({ socketPath: daemon.socketPath })
    expect(await codeOf(malformed.replay('s1'))).toBe('invalid_response')
  })

  it('replay rejects programmer misuse locally without opening a socket', async () => {
    daemon = await startMockDaemon()
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    await expect(client.replay('')).rejects.toThrow(RangeError)
    await expect(client.replay('s1', -1)).rejects.toThrow(RangeError)
    await expect(client.replay('s1', 1.5)).rejects.toThrow(RangeError)
    await expect(client.replay('s1', 0, 0)).rejects.toThrow(RangeError)
    expect(daemon.replayRequests).toEqual([])
  })

  it('subscribe forwards the agents allowlist on the wire and still streams', async () => {
    daemon = await startMockDaemon()
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    const events: SidecarEvent[] = []
    const ready = deferred()
    const gotEvent = deferred()

    const subscription = client.subscribe(
      {
        onReady: () => ready.resolve(),
        onEvent: (event) => {
          events.push(event)
          gotEvent.resolve()
        },
      },
      { agents: ['dsh', 'claude'] },
    )

    await ready.promise
    expect(daemon.subscribeRequests).toEqual([
      { op: 'subscribe', agents: ['dsh', 'claude'] },
    ])
    daemon.pushEvent(ev({ agent: 'dsh', text: 'filtered stream' }))
    await gotEvent.promise
    expect(events[0]?.text).toBe('filtered stream')
    subscription.close()
  })

  it('subscribe without options keeps the bare wire request (back-compat)', async () => {
    daemon = await startMockDaemon()
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    const ready = deferred()
    const subscription = client.subscribe({
      onReady: () => ready.resolve(),
      onEvent: () => {},
    })
    await ready.promise
    expect(daemon.subscribeRequests).toEqual([{ op: 'subscribe' }])
    subscription.close()
  })

  it('subscribe throws synchronously on an invalid agents filter', async () => {
    daemon = await startMockDaemon()
    const client = new SidecarSocketClient({ socketPath: daemon.socketPath })
    const handlers = { onEvent: () => {} }
    expect(() => client.subscribe(handlers, { agents: [] })).toThrow(RangeError)
    expect(() => client.subscribe(handlers, { agents: ['dsh', ''] })).toThrow(RangeError)
    expect(daemon.subscribeRequests).toEqual([])
  })

  it('subscribe drops oversized or malformed lines without dying', async () => {
    daemon = await startMockDaemon()
    const client = new SidecarSocketClient({
      socketPath: daemon.socketPath,
      maxLineBytes: 1024,
    })
    const drops: DropReason[] = []
    const events: SidecarEvent[] = []
    const ready = deferred()
    const gotEvent = deferred()

    const subscription = client.subscribe({
      onReady: () => ready.resolve(),
      onEvent: (event) => {
        events.push(event)
        gotEvent.resolve()
      },
      onDrop: (reason) => drops.push(reason),
    })

    await ready.promise
    daemon.pushRaw('x'.repeat(5000) + '\n')
    daemon.pushRaw('not-json\n')
    daemon.pushEvent(ev({ text: 'still alive' }))
    await gotEvent.promise

    expect(drops).toContain('line_too_long')
    expect(drops).toContain('invalid_json')
    expect(events).toHaveLength(1)
    expect(events[0]?.text).toBe('still alive')
    subscription.close()
  })
})

// ---------------------------------------------------------------------------
// Reconciler (stub client, fake timers).
// ---------------------------------------------------------------------------

describe('Reconciler', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  const OPTS = {
    activeMs: 2000,
    idleMs: 10000,
    debounceMs: 200,
    reconnectMinMs: 1000,
    reconnectMaxMs: 30000,
  }

  it('switches between the active and idle cadence based on working sessions', async () => {
    const client = new StubClient()
    client.sessions = [row({ status: 'working' })]
    const store = new SessionStore()
    const reconciler = new Reconciler(client, store, OPTS)

    reconciler.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(client.statusCalls).toBe(1)
    expect(store.getBoardState().streamHealth).toBe('ok')

    await vi.advanceTimersByTimeAsync(2000)
    expect(client.statusCalls).toBe(2)

    // The daemon reports the session idle; this poll still runs on the
    // active cadence, and only afterwards does the store turn idle.
    client.sessions = [row({ status: 'waiting' })]
    await vi.advanceTimersByTimeAsync(2000)
    expect(client.statusCalls).toBe(3)

    await vi.advanceTimersByTimeAsync(2000)
    expect(client.statusCalls).toBe(3)
    await vi.advanceTimersByTimeAsync(8000)
    expect(client.statusCalls).toBe(4)

    reconciler.stop()
  })

  it('a stream event triggers one debounced early reconcile', async () => {
    const client = new StubClient()
    client.sessions = [row({ status: 'waiting' })]
    const store = new SessionStore()
    const reconciler = new Reconciler(client, store, OPTS)

    reconciler.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(client.statusCalls).toBe(1)

    client.handlers?.onEvent(ev({ kind: 'tool_call', text: 'first' }))
    client.handlers?.onEvent(ev({ kind: 'assistant', text: 'second' }))
    await vi.advanceTimersByTimeAsync(200)
    expect(client.statusCalls).toBe(2)

    const view = store.getBoardState().sessions[0]
    expect(view?.last_event?.kind).toBe('assistant')

    // No further early reconcile pending; the next poll follows the cadence.
    await vi.advanceTimersByTimeAsync(9999)
    expect(client.statusCalls).toBe(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(client.statusCalls).toBe(3)

    reconciler.stop()
  })

  it('reconnects with bounded backoff (1s to 30s cap) and marks the stream degraded', async () => {
    const client = new StubClient()
    client.autoReady = false
    const store = new SessionStore()
    const reconciler = new Reconciler(client, store, OPTS)

    reconciler.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(client.subscribeCalls).toBe(1)
    expect(store.getBoardState().streamHealth).toBe('degraded')

    await vi.advanceTimersByTimeAsync(999)
    expect(client.subscribeCalls).toBe(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(client.subscribeCalls).toBe(2)
    await vi.advanceTimersByTimeAsync(2000)
    expect(client.subscribeCalls).toBe(3)
    await vi.advanceTimersByTimeAsync(4000)
    expect(client.subscribeCalls).toBe(4)
    await vi.advanceTimersByTimeAsync(8000)
    expect(client.subscribeCalls).toBe(5)
    await vi.advanceTimersByTimeAsync(16000)
    expect(client.subscribeCalls).toBe(6)

    // Capped at 30s from here on; never circuit-broken.
    await vi.advanceTimersByTimeAsync(30000)
    expect(client.subscribeCalls).toBe(7)
    await vi.advanceTimersByTimeAsync(30000)
    expect(client.subscribeCalls).toBe(8)
    expect(store.getBoardState().streamHealth).toBe('degraded')

    // Recovery: a validated ack restores health and resets the backoff.
    client.autoReady = true
    await vi.advanceTimersByTimeAsync(30000)
    expect(client.subscribeCalls).toBe(9)
    expect(store.getBoardState().streamHealth).toBe('ok')

    client.autoReady = false
    client.handlers?.onClose?.(new Error('stream dropped'))
    await vi.advanceTimersByTimeAsync(0)
    expect(store.getBoardState().streamHealth).toBe('degraded')
    await vi.advanceTimersByTimeAsync(1000)
    expect(client.subscribeCalls).toBe(10)

    reconciler.stop()
  })

  it('retries a failed reconcile on a short doubling backoff, never a full idle wait (M1 ②)', async () => {
    const client = new StubClient()
    client.failStatus = true
    const store = new SessionStore()
    const reconciler = new Reconciler(client, store, OPTS)

    reconciler.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(client.statusCalls).toBe(1) // initial reconcile failed (no daemon yet)

    // Short backoff ladder: 250 → 500 → 1000 → 2000 → 4000 → 8000ms…
    await vi.advanceTimersByTimeAsync(249)
    expect(client.statusCalls).toBe(1)
    await vi.advanceTimersByTimeAsync(1) // t=250
    expect(client.statusCalls).toBe(2)
    await vi.advanceTimersByTimeAsync(500) // t=750
    expect(client.statusCalls).toBe(3)
    await vi.advanceTimersByTimeAsync(1000) // t=1750
    expect(client.statusCalls).toBe(4)
    await vi.advanceTimersByTimeAsync(2000) // t=3750
    expect(client.statusCalls).toBe(5)
    await vi.advanceTimersByTimeAsync(4000) // t=7750
    expect(client.statusCalls).toBe(6)
    await vi.advanceTimersByTimeAsync(8000) // t=15750
    expect(client.statusCalls).toBe(7)

    // …then capped at the idle cadence: no extra steady-state load.
    await vi.advanceTimersByTimeAsync(9999)
    expect(client.statusCalls).toBe(7)
    await vi.advanceTimersByTimeAsync(1) // t=25750
    expect(client.statusCalls).toBe(8)

    // Recovery resets the streak: snapshot lands, cadence returns to idle.
    client.failStatus = false
    client.sessions = [row()]
    await vi.advanceTimersByTimeAsync(10000) // t=35750
    expect(client.statusCalls).toBe(9)
    expect(store.getBoardState().sessions).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(9999)
    expect(client.statusCalls).toBe(9)
    await vi.advanceTimersByTimeAsync(1)
    expect(client.statusCalls).toBe(10)

    reconciler.stop()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('reconcileNow() (supervisor hand-off) snapshots immediately and supersedes the pending retry', async () => {
    const client = new StubClient()
    client.failStatus = true
    const store = new SessionStore()
    const reconciler = new Reconciler(client, store, OPTS)

    // Cold start: the first status races the daemon socket and loses.
    reconciler.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(client.statusCalls).toBe(1)
    expect(store.getBoardState().sessions).toHaveLength(0)

    // The daemon becomes reachable (ADOPTED/HOSTED are ping-gated); the
    // supervisor hand-off reconciles with zero further timer advancement.
    client.failStatus = false
    client.sessions = [row()]
    await reconciler.reconcileNow()
    expect(client.statusCalls).toBe(2)
    expect(store.getBoardState().sessions).toHaveLength(1)

    // The short-backoff retry pending at t=250 was superseded by the
    // success reschedule: the next poll runs on the idle cadence.
    await vi.advanceTimersByTimeAsync(250)
    expect(client.statusCalls).toBe(2)
    await vi.advanceTimersByTimeAsync(9749) // t=9999
    expect(client.statusCalls).toBe(2)
    await vi.advanceTimersByTimeAsync(1) // t=10000
    expect(client.statusCalls).toBe(3)

    reconciler.stop()
    expect(vi.getTimerCount()).toBe(0)

    // Stopped reconciler: the hand-off is a safe no-op.
    await reconciler.reconcileNow()
    expect(client.statusCalls).toBe(3)
    expect(vi.getTimerCount()).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// SessionStore.
// ---------------------------------------------------------------------------

describe('SessionStore', () => {
  it('applySnapshot fully replaces the session set and sorts by recency', () => {
    const store = new SessionStore()
    store.applySnapshot([
      row({ session_id: 'a', updated_at: 100 }),
      row({ session_id: 'b', updated_at: 200 }),
    ])
    let state = store.getBoardState()
    expect(state.sessions.map((s) => s.session_id)).toEqual(['b', 'a'])
    expect(state.lastReconcileAt).not.toBeNull()
    expect(store.hasWorkingSessions()).toBe(false)

    store.applySnapshot([row({ session_id: 'b', updated_at: 300, status: 'working' })])
    state = store.getBoardState()
    expect(state.sessions).toHaveLength(1)
    expect(state.sessions[0]?.session_id).toBe('b')
    expect(store.hasWorkingSessions()).toBe(true)
  })

  it('events refresh the last-event summary without touching status', () => {
    const store = new SessionStore()
    store.applySnapshot([row({ session_id: 'a', status: 'waiting' })])
    store.applyEvent(ev({ session_id: 'a', kind: 'assistant', text: 'y'.repeat(500) }))

    const view = store.getBoardState().sessions[0]
    expect(view?.status).toBe('waiting')
    expect(view?.last_event?.kind).toBe('assistant')
    expect(view?.last_event?.text.length).toBeLessThanOrEqual(160)
    expect(view?.gap).toBe(false)
  })

  it('flags a seq gap until the next snapshot reconciliation clears it', () => {
    const store = new SessionStore()
    const dshRow = row({ agent: 'dsh', session_id: 'd1' })
    const dshEv = (extra: Record<string, unknown>): SidecarEvent =>
      ev({ agent: 'dsh', session_id: 'd1', extra })

    store.applySnapshot([dshRow])
    store.applyEvent(dshEv({ seq: 1 }))
    store.applyEvent(dshEv({ seq: 2 }))
    expect(store.getBoardState().sessions[0]?.gap).toBe(false)

    store.applyEvent(dshEv({ seq: 5 }))
    expect(store.getBoardState().sessions[0]?.gap).toBe(true)

    store.applySnapshot([dshRow])
    expect(store.getBoardState().sessions[0]?.gap).toBe(false)

    // The seq cursor survives the snapshot: 6 follows 5 without a gap.
    store.applyEvent(dshEv({ seq: 6 }))
    expect(store.getBoardState().sessions[0]?.gap).toBe(false)
  })

  it('events without a valid integer seq never create a false gap', () => {
    const store = new SessionStore()
    store.applySnapshot([row({ session_id: 's1' })])

    store.applyEvent(ev({ extra: { seq: 1 } }))
    store.applyEvent(ev({ extra: {} }))
    store.applyEvent(ev({ extra: { seq: '9' } }))
    store.applyEvent(ev({ extra: { seq: 2.5 } }))
    store.applyEvent(ev({ extra: { seq: true } }))
    store.applyEvent(ev({ extra: { seq: 2 } }))

    expect(store.getBoardState().sessions[0]?.gap).toBe(false)
  })

  it('never evaluates or stores forged row getters and prototypes', () => {
    const store = new SessionStore()
    let getterReads = 0
    const accessorExtra: Record<string, unknown> = {}
    Object.defineProperty(accessorExtra, 'remote', {
      enumerable: true,
      get() {
        getterReads += 1
        return false
      },
    })
    const customPrototype = Object.assign(Object.create({ inherited: true }), row({
      session_id: 'forged-prototype',
    })) as SessionRow

    store.applySnapshot([
      row({ session_id: 'accessor-extra', extra: accessorExtra }),
      customPrototype,
    ])

    expect(getterReads).toBe(0)
    expect(store.getBoardState().sessions).toEqual([
      expect.objectContaining({
        session_id: 'accessor-extra',
        inject_eligibility: { allowed: false, reason: 'invalid_session' },
      }),
    ])
  })

  it('keeps event hints invisible until the session appears in a snapshot', () => {
    const store = new SessionStore()
    store.applySnapshot([])
    store.applyEvent(ev({ session_id: 'ghost', text: 'early bird' }))
    expect(store.getBoardState().sessions).toHaveLength(0)

    store.applySnapshot([row({ session_id: 'ghost' })])
    const view = store.getBoardState().sessions[0]
    expect(view?.last_event?.text).toBe('early bird')
  })

  it('onChange notifies on mutations and supports unsubscribe', () => {
    const store = new SessionStore()
    let calls = 0
    const off = store.onChange(() => {
      calls += 1
    })

    store.applySnapshot([])
    expect(calls).toBe(1)
    store.setStreamHealth('degraded')
    expect(calls).toBe(2)
    store.setStreamHealth('degraded')
    expect(calls).toBe(2)
    expect(store.getBoardState().streamHealth).toBe('degraded')

    off()
    store.applySnapshot([])
    expect(calls).toBe(2)
  })
})
