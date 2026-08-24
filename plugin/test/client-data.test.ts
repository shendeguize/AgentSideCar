/**
 * Unit tests for the browser-half data layer (client/api.ts + client/sse.ts).
 * Node environment with injected fakes throughout: no real network, DOM,
 * or wall-clock timers.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  API_PREFIX,
  ApiError,
  DEFAULT_TIMEOUT_MS,
  fetchSession,
  fetchState,
  isApiError,
  postAction,
  type AbortControllerLike,
  type FetchLike,
  type RequestInitLike,
  type ResponseLike,
  type SessionView,
  type StateSnapshot,
} from '../src/client/api.ts'
import {
  STREAM_PATH,
  StateStream,
  type EventSourceMessageLike,
  type PollFn,
  type StateStreamOptions,
} from '../src/client/sse.ts'
import { API_PREFIX as SERVER_API_PREFIX } from '../src/routes.ts'

// ---------------------------------------------------------------------------
// Fakes.
// ---------------------------------------------------------------------------

class FakeAbortSignal {
  aborted = false
  private readonly listeners = new Set<() => void>()

  addEventListener(_type: 'abort', listener: () => void): void {
    this.listeners.add(listener)
  }

  removeEventListener(_type: 'abort', listener: () => void): void {
    this.listeners.delete(listener)
  }

  get listenerCount(): number {
    return this.listeners.size
  }

  fire(): void {
    for (const listener of [...this.listeners]) listener()
  }
}

class FakeAbortController implements AbortControllerLike {
  readonly signal = new FakeAbortSignal()
  aborts = 0

  abort(): void {
    this.aborts += 1
    if (this.signal.aborted) return
    this.signal.aborted = true
    this.signal.fire()
  }
}

function jsonResponse(status: number, body: unknown): ResponseLike {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  }
}

interface SeenRequest {
  url: string
  init: RequestInitLike
}

/** Fetch fake that records calls and answers from a fixed response queue. */
function captureFetch(responses: ResponseLike[]): { fetch: FetchLike; seen: SeenRequest[] } {
  const seen: SeenRequest[] = []
  const fetch: FetchLike = (url, init) => {
    seen.push({ url, init })
    const next = responses.shift()
    if (next === undefined) throw new Error('unexpected extra fetch call')
    return Promise.resolve(next)
  }
  return { fetch, seen }
}

/** Never resolves on its own; rejects when the passed signal aborts. */
const hangingFetch: FetchLike = (_url, init) =>
  new Promise((_resolve, reject) => {
    init.signal?.addEventListener('abort', () => reject(new Error('aborted')))
  })

function makeSessionView(working: boolean): SessionView {
  return {
    agent: 'claude',
    session_id: 'sess-1',
    status: working ? 'working' : 'waiting',
    title: 'demo session',
    project: '/tmp/demo',
    updated_at: 1_724_000_000,
    last_event: null,
    gap: false,
  }
}

function makeSnapshot(working: boolean): StateSnapshot {
  return {
    daemon: {
      state: 'adopted',
      lastPing: { pid: 42, version: '1.0.0', http: { enabled: false } },
    },
    board: {
      sessions: [makeSessionView(working)],
      streamHealth: 'ok',
      lastReconcileAt: 1_724_000_000_000,
    },
    capabilities: { inject: false },
  }
}

class FakeEventSource {
  static instances: FakeEventSource[] = []

  readyState = 0 // CONNECTING
  closed = false
  private readonly listeners = new Map<string, Set<(ev: EventSourceMessageLike) => void>>()

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (ev: EventSourceMessageLike) => void): void {
    let set = this.listeners.get(type)
    if (set === undefined) {
      set = new Set()
      this.listeners.set(type, set)
    }
    set.add(listener)
  }

  close(): void {
    this.closed = true
    this.readyState = 2
  }

  emit(type: string, ev: EventSourceMessageLike = {}): void {
    for (const listener of [...(this.listeners.get(type) ?? [])]) listener(ev)
  }

  emitOpen(): void {
    this.readyState = 1
    this.emit('open')
  }

  emitState(data: unknown): void {
    this.emit('state', { data })
  }

  emitError(opts: { closed?: boolean } = {}): void {
    this.readyState = opts.closed === true ? 2 : 0
    this.emit('error')
  }
}

function makeSseStream(overrides: Partial<StateStreamOptions> = {}): StateStream {
  return new StateStream({
    url: STREAM_PATH,
    mode: 'sse',
    eventSourceFactory: (url) => new FakeEventSource(url),
    errorThreshold: 2,
    ...overrides,
  })
}

function makePollStream(
  pollFn: PollFn,
  overrides: Partial<StateStreamOptions> = {},
): { stream: StateStream; controllers: FakeAbortController[] } {
  const controllers: FakeAbortController[] = []
  const stream = new StateStream({
    url: STREAM_PATH,
    mode: 'poll',
    pollFn,
    createAbortController: () => {
      const controller = new FakeAbortController()
      controllers.push(controller)
      return controller
    },
    ...overrides,
  })
  return { stream, controllers }
}

beforeEach(() => {
  vi.useFakeTimers()
  FakeEventSource.instances = []
})

afterEach(() => {
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// Contract mirror.
// ---------------------------------------------------------------------------

describe('contract mirror', () => {
  it('client API_PREFIX matches the routes.ts constant', () => {
    expect(API_PREFIX).toBe(SERVER_API_PREFIX)
    expect(STREAM_PATH).toBe(`${SERVER_API_PREFIX}/stream`)
  })
})

// ---------------------------------------------------------------------------
// api.ts — fetch wrappers.
// ---------------------------------------------------------------------------

describe('fetchState', () => {
  it('GETs <prefix>/state and returns the parsed snapshot', async () => {
    const snapshot = makeSnapshot(false)
    const { fetch, seen } = captureFetch([jsonResponse(200, snapshot)])
    const controller = new FakeAbortController()
    const result = await fetchState({ fetch, createAbortController: () => controller })
    expect(result).toEqual(snapshot)
    expect(seen).toHaveLength(1)
    expect(seen[0]!.url).toBe('/plugins/agent-sidecar/api/state')
    expect(seen[0]!.init.method).toBe('GET')
    expect(seen[0]!.init.signal).toBe(controller.signal)
    expect(vi.getTimerCount()).toBe(0) // deadline timer cleaned up
  })

  it('rejects with kind timeout after the default 15s and aborts the fetch', async () => {
    const controller = new FakeAbortController()
    const promise = fetchState({
      fetch: hangingFetch,
      createAbortController: () => controller,
    })
    const expectation = expect(promise).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'timeout',
      reason: 'request_timeout',
      status: null,
    })
    await vi.advanceTimersByTimeAsync(DEFAULT_TIMEOUT_MS - 1)
    expect(controller.aborts).toBe(0)
    await vi.advanceTimersByTimeAsync(1)
    await expectation
    expect(controller.aborts).toBe(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('honors a custom timeoutMs', async () => {
    const controller = new FakeAbortController()
    const promise = fetchState({
      fetch: hangingFetch,
      createAbortController: () => controller,
      timeoutMs: 1_000,
    })
    const expectation = expect(promise).rejects.toMatchObject({ kind: 'timeout' })
    await vi.advanceTimersByTimeAsync(1_000)
    await expectation
    expect(controller.aborts).toBe(1)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('surfaces the server {reason} envelope on non-2xx', async () => {
    const { fetch } = captureFetch([jsonResponse(403, { reason: 'origin_mismatch' })])
    await expect(
      fetchState({ fetch, createAbortController: () => new FakeAbortController() }),
    ).rejects.toMatchObject({ kind: 'http', status: 403, reason: 'origin_mismatch' })
    expect(vi.getTimerCount()).toBe(0)
  })

  it('falls back to http_<status> when the error body is not JSON', async () => {
    const broken: ResponseLike = {
      ok: false,
      status: 500,
      json: () => Promise.reject(new Error('not json')),
    }
    await expect(
      fetchState({
        fetch: () => Promise.resolve(broken),
        createAbortController: () => new FakeAbortController(),
      }),
    ).rejects.toMatchObject({ kind: 'http', status: 500, reason: 'http_500' })
  })

  it('rejects with kind parse when a 2xx body is not JSON', async () => {
    const broken: ResponseLike = {
      ok: true,
      status: 200,
      json: () => Promise.reject(new Error('bad json')),
    }
    await expect(
      fetchState({
        fetch: () => Promise.resolve(broken),
        createAbortController: () => new FakeAbortController(),
      }),
    ).rejects.toMatchObject({ kind: 'parse', reason: 'invalid_json', status: 200 })
  })

  it('normalizes fetch rejections to kind network and keeps the cause', async () => {
    const boom = new Error('ECONNREFUSED')
    const error: unknown = await fetchState({
      fetch: () => Promise.reject(boom),
      createAbortController: () => new FakeAbortController(),
    }).catch((e: unknown) => e)
    expect(isApiError(error)).toBe(true)
    expect(error).toMatchObject({ kind: 'network', reason: 'network_error', status: null })
    expect((error as ApiError).cause).toBe(boom)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('maps caller-side aborts to kind aborted and unhooks the external signal', async () => {
    const external = new FakeAbortController()
    const inner = new FakeAbortController()
    const promise = fetchState({
      fetch: hangingFetch,
      signal: external.signal,
      createAbortController: () => inner,
    })
    const expectation = expect(promise).rejects.toMatchObject({
      kind: 'aborted',
      reason: 'request_aborted',
    })
    external.abort()
    await expectation
    expect(inner.aborts).toBe(1)
    expect(external.signal.listenerCount).toBe(0)
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('fetchSession', () => {
  it('URL-encodes the session id and returns the detail body', async () => {
    const detail = {
      session: makeSessionView(false),
      timeline: null,
      timelineNote: 'timeline_not_available_until_m3',
    }
    const { fetch, seen } = captureFetch([jsonResponse(200, detail)])
    const result = await fetchSession('a/b c#1', {
      fetch,
      createAbortController: () => new FakeAbortController(),
    })
    expect(result).toEqual(detail)
    expect(seen[0]!.url).toBe(`${API_PREFIX}/session/a%2Fb%20c%231`)
    expect(seen[0]!.init.method).toBe('GET')
  })

  it('maps 404 to the session_not_found envelope', async () => {
    const { fetch } = captureFetch([jsonResponse(404, { reason: 'session_not_found' })])
    await expect(
      fetchSession('nope', { fetch, createAbortController: () => new FakeAbortController() }),
    ).rejects.toMatchObject({ kind: 'http', status: 404, reason: 'session_not_found' })
  })
})

describe('postAction', () => {
  it('POSTs the envelope verbatim as JSON with the guard-mandated content-type', async () => {
    const { fetch, seen } = captureFetch([
      jsonResponse(501, { reason: 'not_implemented_until_m2' }),
    ])
    const envelope = {
      requestId: 'req-1',
      method: 'inject.prepare',
      args: { target: 'sess-1' },
    }
    await expect(
      postAction(envelope, { fetch, createAbortController: () => new FakeAbortController() }),
    ).rejects.toMatchObject({ kind: 'http', status: 501, reason: 'not_implemented_until_m2' })
    expect(seen).toHaveLength(1) // transport layer never retries
    expect(seen[0]!.url).toBe(`${API_PREFIX}/action`)
    expect(seen[0]!.init.method).toBe('POST')
    expect(seen[0]!.init.headers).toEqual({ 'content-type': 'application/json' })
    expect(JSON.parse(seen[0]!.init.body!)).toEqual(envelope)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('maps the 403 write gate to a normal http error without retrying', async () => {
    const { fetch, seen } = captureFetch([jsonResponse(403, { reason: 'inject_disabled' })])
    await expect(
      postAction(
        { requestId: 'r', method: 'inject.prepare' },
        { fetch, createAbortController: () => new FakeAbortController() },
      ),
    ).rejects.toMatchObject({ kind: 'http', status: 403, reason: 'inject_disabled' })
    expect(seen).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
// sse.ts — StateStream, SSE mode.
// ---------------------------------------------------------------------------

describe('StateStream (sse mode)', () => {
  it('connects on start and reports connecting → open', () => {
    const stream = makeSseStream()
    const statuses: string[] = []
    stream.onStatus((s) => statuses.push(s))
    stream.start()
    expect(FakeEventSource.instances).toHaveLength(1)
    expect(FakeEventSource.instances[0]!.url).toBe(STREAM_PATH)
    FakeEventSource.instances[0]!.emitOpen()
    expect(statuses).toEqual(['connecting', 'open'])
    stream.stop()
  })

  it('parses state frames into snapshots and honors unsubscribe', () => {
    const stream = makeSseStream()
    const snapshot = makeSnapshot(true)
    const first: StateSnapshot[] = []
    const second: StateSnapshot[] = []
    const offFirst = stream.onSnapshot((s) => first.push(s))
    stream.onSnapshot((s) => second.push(s))
    stream.start()
    const es = FakeEventSource.instances[0]!
    es.emitOpen()
    es.emitState(JSON.stringify(snapshot))
    expect(first).toEqual([snapshot])
    expect(second).toEqual([snapshot])
    offFirst()
    es.emitState(JSON.stringify(snapshot))
    expect(first).toHaveLength(1)
    expect(second).toHaveLength(2)
    stream.stop()
  })

  it('ignores malformed and non-string state frames without breaking', () => {
    const stream = makeSseStream()
    const snaps: StateSnapshot[] = []
    stream.onSnapshot((s) => snaps.push(s))
    stream.start()
    const es = FakeEventSource.instances[0]!
    es.emitOpen()
    es.emitState('{ not json')
    es.emitState(42)
    es.emitState(undefined)
    es.emitState(JSON.stringify('scalar'))
    expect(snaps).toHaveLength(0)
    es.emitState(JSON.stringify(makeSnapshot(false)))
    expect(snaps).toHaveLength(1)
    stream.stop()
  })

  it('tolerates errors up to the threshold, then degrades and rebuilds', () => {
    const stream = makeSseStream() // errorThreshold: 2
    const statuses: string[] = []
    stream.onStatus((s) => statuses.push(s))
    stream.start()
    const es1 = FakeEventSource.instances[0]!
    es1.emitOpen()
    es1.emitError() // 1 ≤ 2 → native auto-reconnect, connecting
    es1.emitError() // 2 ≤ 2 → still connecting (deduped)
    expect(statuses).toEqual(['connecting', 'open', 'connecting'])
    expect(es1.closed).toBe(false)
    expect(vi.getTimerCount()).toBe(0)
    es1.emitError() // 3 > 2 → degraded, manual rebuild takes over
    expect(statuses).toEqual(['connecting', 'open', 'connecting', 'degraded'])
    expect(es1.closed).toBe(true)
    expect(FakeEventSource.instances).toHaveLength(1)
    vi.advanceTimersByTime(4_999)
    expect(FakeEventSource.instances).toHaveLength(1)
    vi.advanceTimersByTime(1) // 5s base backoff
    expect(FakeEventSource.instances).toHaveLength(2)
    stream.stop()
  })

  it('recovers to open after a rebuild and resets the error/backoff counters', () => {
    const stream = makeSseStream()
    const statuses: string[] = []
    stream.onStatus((s) => statuses.push(s))
    stream.start()
    const es1 = FakeEventSource.instances[0]!
    es1.emitOpen()
    es1.emitError()
    es1.emitError()
    es1.emitError()
    vi.advanceTimersByTime(5_000)
    const es2 = FakeEventSource.instances[1]!
    es2.emitOpen()
    expect(statuses.at(-1)).toBe('open')
    // Counters were reset: a single new error only means "connecting".
    es2.emitError()
    expect(statuses.at(-1)).toBe('connecting')
    expect(es2.closed).toBe(false)
    stream.stop()
  })

  it('degrades immediately on readyState CLOSED, even below the threshold', () => {
    const stream = makeSseStream({ errorThreshold: 99 })
    const statuses: string[] = []
    stream.onStatus((s) => statuses.push(s))
    stream.start()
    const es1 = FakeEventSource.instances[0]!
    es1.emitOpen()
    es1.emitError({ closed: true })
    expect(statuses.at(-1)).toBe('degraded')
    expect(es1.closed).toBe(true)
    vi.advanceTimersByTime(5_000)
    expect(FakeEventSource.instances).toHaveLength(2)
    stream.stop()
  })

  it('doubles the rebuild delay up to the 30s cap and never gives up', () => {
    const stream = makeSseStream({ errorThreshold: 0 })
    stream.start()
    const kill = (idx: number): void => {
      FakeEventSource.instances[idx]!.emitError()
    }
    kill(0) // attempt 0 → 5s
    vi.advanceTimersByTime(5_000)
    expect(FakeEventSource.instances).toHaveLength(2)
    kill(1) // attempt 1 → 10s
    vi.advanceTimersByTime(9_999)
    expect(FakeEventSource.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeEventSource.instances).toHaveLength(3)
    kill(2) // attempt 2 → 20s
    vi.advanceTimersByTime(20_000)
    expect(FakeEventSource.instances).toHaveLength(4)
    kill(3) // attempt 3 → 40s capped at 30s
    vi.advanceTimersByTime(29_999)
    expect(FakeEventSource.instances).toHaveLength(4)
    vi.advanceTimersByTime(1)
    expect(FakeEventSource.instances).toHaveLength(5)
    kill(4) // stays at the cap, no melt-down
    vi.advanceTimersByTime(30_000)
    expect(FakeEventSource.instances).toHaveLength(6)
    // Recovery resets the ladder back to the base delay.
    FakeEventSource.instances[5]!.emitOpen()
    kill(5)
    vi.advanceTimersByTime(5_000)
    expect(FakeEventSource.instances).toHaveLength(7)
    stream.stop()
  })

  it('stop tears down the instance, timers, and callbacks with no leaks', () => {
    const stream = makeSseStream({ errorThreshold: 0 })
    const statuses: string[] = []
    const snaps: StateSnapshot[] = []
    stream.onStatus((s) => statuses.push(s))
    stream.onSnapshot((s) => snaps.push(s))
    stream.start()
    const es1 = FakeEventSource.instances[0]!
    es1.emitError() // → degraded + pending rebuild timer
    expect(vi.getTimerCount()).toBe(1)
    stream.stop()
    expect(vi.getTimerCount()).toBe(0)
    expect(es1.closed).toBe(true)
    const statusCountBefore = statuses.length
    es1.emitState(JSON.stringify(makeSnapshot(false)))
    es1.emitOpen()
    expect(snaps).toHaveLength(0)
    expect(statuses.length).toBe(statusCountBefore)
    vi.advanceTimersByTime(60_000)
    expect(FakeEventSource.instances).toHaveLength(1) // no rebuild after stop
  })
})

// ---------------------------------------------------------------------------
// sse.ts — StateStream, poll mode.
// ---------------------------------------------------------------------------

describe('StateStream (poll mode)', () => {
  it('polls immediately on start and passes an abortable signal', async () => {
    const seen: Array<{ signal?: unknown }> = []
    const pollFn: PollFn = (opts) => {
      seen.push(opts)
      return Promise.resolve(makeSnapshot(false))
    }
    const { stream, controllers } = makePollStream(pollFn)
    const snaps: StateSnapshot[] = []
    const statuses: string[] = []
    stream.onSnapshot((s) => snaps.push(s))
    stream.onStatus((s) => statuses.push(s))
    stream.start()
    expect(seen).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(0)
    expect(snaps).toHaveLength(1)
    expect(statuses).toEqual(['connecting', 'open'])
    expect(seen[0]!.signal).toBe(controllers[0]!.signal)
    stream.stop()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('runs the 3s cadence while working and 15s while idle, switching live', async () => {
    let working = true
    const pollFn = vi.fn(() => Promise.resolve(makeSnapshot(working)))
    const { stream } = makePollStream(pollFn)
    stream.start() // call 1 → working → active cadence
    await vi.advanceTimersByTimeAsync(0)
    expect(pollFn).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2_999)
    expect(pollFn).toHaveBeenCalledTimes(1)
    working = false
    await vi.advanceTimersByTimeAsync(1) // call 2 at 3s → idle → idle cadence
    expect(pollFn).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(14_999)
    expect(pollFn).toHaveBeenCalledTimes(2)
    working = true
    await vi.advanceTimersByTimeAsync(1) // call 3 at 15s → working again
    expect(pollFn).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(3_000) // active cadence restored
    expect(pollFn).toHaveBeenCalledTimes(4)
    stream.stop()
  })

  it('pauses fetching while hidden and resumes at the next visible tick', async () => {
    let visible = true
    const pollFn = vi.fn(() => Promise.resolve(makeSnapshot(true)))
    const { stream } = makePollStream(pollFn, { visible: () => visible })
    stream.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(pollFn).toHaveBeenCalledTimes(1)
    visible = false
    await vi.advanceTimersByTimeAsync(3_000)
    expect(pollFn).toHaveBeenCalledTimes(1) // gated
    await vi.advanceTimersByTimeAsync(3_000)
    expect(pollFn).toHaveBeenCalledTimes(1) // still gated; ticks keep checking
    visible = true
    await vi.advanceTimersByTimeAsync(3_000)
    expect(pollFn).toHaveBeenCalledTimes(2) // natural tick resumes fetching
    stream.stop()
  })

  it('pollNow() fetches immediately on visibility resume and reschedules cleanly', async () => {
    let visible = true
    const pollFn = vi.fn(() => Promise.resolve(makeSnapshot(true)))
    const { stream } = makePollStream(pollFn, { visible: () => visible })
    stream.start()
    await vi.advanceTimersByTimeAsync(0)
    visible = false
    await vi.advanceTimersByTimeAsync(3_000)
    expect(pollFn).toHaveBeenCalledTimes(1)
    visible = true
    stream.pollNow() // the visibilitychange hook
    expect(pollFn).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(0)
    // The pending tick was cancelled: next fetch only after a full interval.
    await vi.advanceTimersByTimeAsync(2_999)
    expect(pollFn).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(pollFn).toHaveBeenCalledTimes(3)
    stream.stop()
  })

  it('reports degraded on poll failure and recovers to open on the next success', async () => {
    let fail = true
    const pollFn = vi.fn(() =>
      fail ? Promise.reject(new Error('boom')) : Promise.resolve(makeSnapshot(false)),
    )
    const { stream } = makePollStream(pollFn)
    const statuses: string[] = []
    stream.onStatus((s) => statuses.push(s))
    stream.start()
    await vi.advanceTimersByTimeAsync(0)
    expect(statuses).toEqual(['connecting', 'degraded'])
    fail = false
    await vi.advanceTimersByTimeAsync(15_000) // no snapshot yet → idle cadence
    expect(statuses).toEqual(['connecting', 'degraded', 'open'])
    stream.stop()
  })

  it('stop aborts the in-flight fetch, clears timers, and ignores late results', async () => {
    let release: ((snapshot: StateSnapshot) => void) | null = null
    const pollFn: PollFn = () =>
      new Promise((resolve) => {
        release = resolve
      })
    const { stream, controllers } = makePollStream(pollFn)
    const snaps: StateSnapshot[] = []
    stream.onSnapshot((s) => snaps.push(s))
    stream.start()
    expect(controllers).toHaveLength(1)
    stream.stop()
    expect(controllers[0]!.aborts).toBe(1)
    expect(vi.getTimerCount()).toBe(0)
    release!(makeSnapshot(false))
    await vi.advanceTimersByTimeAsync(0)
    expect(snaps).toHaveLength(0)
    expect(vi.getTimerCount()).toBe(0)
  })

  it('stop is idempotent and start after stop stays inert', () => {
    const pollFn = vi.fn(() => Promise.resolve(makeSnapshot(false)))
    const { stream } = makePollStream(pollFn)
    stream.start()
    stream.stop()
    stream.stop()
    stream.start()
    expect(pollFn).toHaveBeenCalledTimes(1)
    expect(vi.getTimerCount()).toBe(0)
  })
})
