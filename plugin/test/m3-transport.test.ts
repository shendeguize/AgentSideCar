/**
 * Unit tests for the M3 deep-query transport (client/m3-transport.ts):
 * path/query construction, body passthrough, and the normalized ApiError
 * taxonomy. Node environment with injected fakes; no real network.
 */

import { describe, expect, it } from 'vitest'
import {
  API_PREFIX,
  isApiError,
  type AbortControllerLike,
  type FetchLike,
  type RequestInitLike,
  type ResponseLike,
} from '../src/client/api.ts'
import { fetchLineage, fetchProjects, fetchSearch } from '../src/client/m3-transport.ts'

class FakeAbortSignal {
  aborted = false
  private readonly listeners = new Set<() => void>()
  addEventListener(_type: 'abort', listener: () => void): void {
    this.listeners.add(listener)
  }
  removeEventListener(_type: 'abort', listener: () => void): void {
    this.listeners.delete(listener)
  }
  fire(): void {
    for (const listener of [...this.listeners]) listener()
  }
}

class FakeAbortController implements AbortControllerLike {
  readonly signal = new FakeAbortSignal()
  abort(): void {
    if (this.signal.aborted) return
    this.signal.aborted = true
    this.signal.fire()
  }
}

function jsonResponse(status: number, body: unknown): ResponseLike {
  return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) }
}

interface SeenRequest {
  url: string
  init: RequestInitLike
}

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

const deps = { createAbortController: (): AbortControllerLike => new FakeAbortController() }

describe('fetchLineage', () => {
  it('dials GET lineage/<id> (escaped) and passes the body through', async () => {
    const body = { available: true, trace: null, reason: null }
    const { fetch, seen } = captureFetch([jsonResponse(200, body)])
    const result = await fetchLineage('dsh/one two', { fetch, ...deps })
    expect(seen[0]?.url).toBe(`${API_PREFIX}/lineage/dsh%2Fone%20two`)
    expect(seen[0]?.init.method).toBe('GET')
    expect(result).toEqual(body)
  })

  it('maps a non-ok reason envelope to ApiError (501 fusion_not_wired)', async () => {
    const { fetch } = captureFetch([jsonResponse(501, { reason: 'fusion_not_wired' })])
    const err = await fetchLineage('x', { fetch, ...deps }).catch((e: unknown) => e)
    expect(isApiError(err)).toBe(true)
    if (isApiError(err)) {
      expect(err.kind).toBe('http')
      expect(err.reason).toBe('fusion_not_wired')
      expect(err.status).toBe(501)
    }
  })
})

describe('fetchSearch', () => {
  it('encodes q/project/limit and passes the body through', async () => {
    const body = { mode: 'full-text', query: 'x y', project: '/p', items: [] }
    const { fetch, seen } = captureFetch([jsonResponse(200, body)])
    const result = await fetchSearch({ q: 'x y', project: '/p', limit: 5, fetch, ...deps })
    expect(seen[0]?.url).toBe(`${API_PREFIX}/search?q=x+y&project=%2Fp&limit=5`)
    expect(result).toEqual(body)
  })

  it('omits blank q and null project from the query string', async () => {
    const { fetch, seen } = captureFetch([
      jsonResponse(200, { mode: 'filter-only', query: '', project: '/p', items: [] }),
    ])
    await fetchSearch({ q: '  ', project: '/p', fetch, ...deps })
    expect(seen[0]?.url).toBe(`${API_PREFIX}/search?project=%2Fp`)
  })

  it('rejects with the server reason on 400', async () => {
    const { fetch } = captureFetch([jsonResponse(400, { reason: 'invalid_request' })])
    const err = await fetchSearch({ q: 'x', fetch, ...deps }).catch((e: unknown) => e)
    expect(isApiError(err) && err.reason === 'invalid_request').toBe(true)
  })
})

describe('fetchProjects', () => {
  it('dials GET projects and passes the groups through', async () => {
    const body = { groups: [{ project: '/p', agents: ['dsh'], sessions: [], lastActivityAt: 1 }] }
    const { fetch, seen } = captureFetch([jsonResponse(200, body)])
    const result = await fetchProjects({ fetch, ...deps })
    expect(seen[0]?.url).toBe(`${API_PREFIX}/projects`)
    expect(result).toEqual(body)
  })

  it('normalizes a thrown fetch into a network ApiError', async () => {
    const boom: FetchLike = () => Promise.reject(new Error('offline'))
    const err = await fetchProjects({ fetch: boom, ...deps }).catch((e: unknown) => e)
    expect(isApiError(err)).toBe(true)
    if (isApiError(err)) {
      expect(err.kind).toBe('network')
      expect(err.reason).toBe('network_error')
    }
  })

  it('times out through the injected timer and aborts the controller', async () => {
    const controller = new FakeAbortController()
    const hanging: FetchLike = (_url, init) =>
      new Promise((_resolve, reject) => {
        init.signal?.addEventListener('abort', () => reject(new Error('aborted')))
      })
    let fireTimeout: (() => void) | null = null
    const err = await fetchProjects({
      fetch: hanging,
      createAbortController: () => controller,
      setTimeout: (fn) => {
        fireTimeout = fn
        // Fire on the next microtask so the fetch is already pending.
        void Promise.resolve().then(() => fireTimeout?.())
        return 0
      },
      clearTimeout: () => {},
    }).catch((e: unknown) => e)
    expect(isApiError(err)).toBe(true)
    if (isApiError(err)) {
      expect(err.kind).toBe('timeout')
      expect(err.reason).toBe('request_timeout')
    }
    expect(controller.signal.aborted).toBe(true)
  })
})
