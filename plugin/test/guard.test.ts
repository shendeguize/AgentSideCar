/**
 * Unit tests for the five-layer route guard (design doc §4.f / §8).
 * Pure mock IncomingMessage shapes; no cordis/dsh, no sockets.
 */

import { describe, expect, it } from 'vitest'
import type { IncomingMessage } from 'node:http'
import {
  guardRequest,
  guardUpgrade,
  guardWriteAction,
  hostIsLoopback,
  isLoopbackAddress,
  type GuardableRequest,
  type GuardVerdict,
} from '../src/guard.ts'

interface MockReqInit {
  method?: string
  url?: string
  remoteAddress?: string | undefined
  headers?: Record<string, string | string[] | undefined>
}

/** Baseline: a legit same-machine browser GET that must pass every layer. */
function mockReq(init: MockReqInit = {}): GuardableRequest {
  const headers: IncomingMessage['headers'] = {
    host: '127.0.0.1:3000',
    ...init.headers,
  }
  return {
    method: init.method ?? 'GET',
    url: init.url ?? '/plugins/agent-sidecar/api/state',
    headers,
    socket: {
      remoteAddress: 'remoteAddress' in init ? init.remoteAddress : '127.0.0.1',
    } as IncomingMessage['socket'],
  }
}

function expectForbidden(verdict: GuardVerdict, reason: string): void {
  expect(verdict).toEqual({ ok: false, status: 403, reason })
}

describe('isLoopbackAddress', () => {
  it('accepts the whole 127.0.0.0/8 block, ::1, and IPv4-mapped loopback', () => {
    expect(isLoopbackAddress('127.0.0.1')).toBe(true)
    expect(isLoopbackAddress('127.255.0.7')).toBe(true)
    expect(isLoopbackAddress('::1')).toBe(true)
    expect(isLoopbackAddress('::ffff:127.0.0.1')).toBe(true)
    expect(isLoopbackAddress('::FFFF:127.9.8.7')).toBe(true)
  })

  it('rejects everything else, including unparsable input (fail closed)', () => {
    expect(isLoopbackAddress(undefined)).toBe(false)
    expect(isLoopbackAddress('')).toBe(false)
    expect(isLoopbackAddress('192.168.1.5')).toBe(false)
    expect(isLoopbackAddress('10.0.0.2')).toBe(false)
    expect(isLoopbackAddress('8.8.8.8')).toBe(false)
    expect(isLoopbackAddress('::2')).toBe(false)
    expect(isLoopbackAddress('::ffff:10.0.0.1')).toBe(false)
    expect(isLoopbackAddress('127.0.0')).toBe(false)
    expect(isLoopbackAddress('127.0.0.999')).toBe(false)
    expect(isLoopbackAddress('127.evil.com')).toBe(false)
  })
})

describe('hostIsLoopback', () => {
  it('accepts localhost / 127.x / [::1] authorities, with or without port', () => {
    expect(hostIsLoopback('localhost')).toBe(true)
    expect(hostIsLoopback('localhost:3000')).toBe(true)
    expect(hostIsLoopback('LOCALHOST:3000')).toBe(true) // Host is case-insensitive
    expect(hostIsLoopback('127.0.0.1')).toBe(true) // default port elided
    expect(hostIsLoopback('127.0.0.1:8080')).toBe(true)
    expect(hostIsLoopback('127.5.5.5:8080')).toBe(true)
    expect(hostIsLoopback('[::1]')).toBe(true)
    expect(hostIsLoopback('[::1]:8080')).toBe(true)
    expect(hostIsLoopback('[::ffff:127.0.0.1]:8080')).toBe(true)
  })

  it('rejects missing, non-loopback, and malformed authorities', () => {
    expect(hostIsLoopback(undefined)).toBe(false)
    expect(hostIsLoopback('')).toBe(false)
    expect(hostIsLoopback('evil.com')).toBe(false)
    expect(hostIsLoopback('evil.com:3000')).toBe(false)
    expect(hostIsLoopback('192.168.1.5:3000')).toBe(false) // intranet IP
    expect(hostIsLoopback('[2001:db8::1]:3000')).toBe(false)
    expect(hostIsLoopback('localhost.evil.com')).toBe(false)
    expect(hostIsLoopback('127.0.0.1.evil.com')).toBe(false)
    expect(hostIsLoopback('localhost:notaport')).toBe(false)
    expect(hostIsLoopback('localhost:0')).toBe(false)
    expect(hostIsLoopback('localhost:99999')).toBe(false)
    expect(hostIsLoopback('[::1')).toBe(false) // unclosed bracket
    expect(hostIsLoopback('::1:8080')).toBe(false) // unbracketed IPv6 is invalid in Host
  })
})

describe('guardRequest layer 1 — remoteAddress must be loopback', () => {
  it('rejects non-loopback peers with 403', () => {
    for (const remoteAddress of ['192.168.1.5', '10.0.0.2', '8.8.8.8', '::2', '::ffff:10.0.0.1']) {
      expectForbidden(guardRequest(mockReq({ remoteAddress })), 'remote_not_loopback')
    }
  })

  it('rejects a missing remoteAddress (fail closed)', () => {
    expectForbidden(guardRequest(mockReq({ remoteAddress: undefined })), 'remote_not_loopback')
  })

  it('allows IPv4-mapped loopback (dual-stack listener) and plain ::1', () => {
    expect(guardRequest(mockReq({ remoteAddress: '::ffff:127.0.0.1' }))).toEqual({ ok: true })
    expect(guardRequest(mockReq({ remoteAddress: '::1' }))).toEqual({ ok: true })
    expect(guardRequest(mockReq({ remoteAddress: '127.0.0.53' }))).toEqual({ ok: true })
  })
})

describe('guardRequest layer 2 — Host must be a loopback authority', () => {
  it('rejects forged Hosts (DNS rebinding) with 403', () => {
    for (const host of ['evil.com', 'evil.com:3000', '192.168.1.5:3000', '[2001:db8::1]:3000']) {
      expectForbidden(guardRequest(mockReq({ headers: { host } })), 'host_not_loopback')
    }
  })

  it('rejects a missing Host header with 403', () => {
    expectForbidden(guardRequest(mockReq({ headers: { host: undefined } })), 'host_not_loopback')
  })

  it('allows loopback authorities: with port, without port, any case, IPv6', () => {
    for (const host of ['127.0.0.1:3000', '127.0.0.1', 'localhost:3000', 'LocalHost:3000', '[::1]:3000']) {
      expect(guardRequest(mockReq({ headers: { host } }))).toEqual({ ok: true })
    }
  })
})

describe('guardRequest layer 3 — Origin / sec-fetch-site', () => {
  it('allows a same-origin Origin (http and https schemes both fine)', () => {
    expect(
      guardRequest(mockReq({ headers: { host: '127.0.0.1:3000', origin: 'http://127.0.0.1:3000' } })),
    ).toEqual({ ok: true })
    expect(
      guardRequest(mockReq({ headers: { host: '127.0.0.1:3000', origin: 'https://127.0.0.1:3000' } })),
    ).toEqual({ ok: true })
    expect(
      guardRequest(mockReq({ headers: { host: 'localhost:8080', origin: 'http://LOCALHOST:8080' } })),
    ).toEqual({ ok: true })
  })

  it('normalizes default ports on both sides', () => {
    // Host omits the port: either scheme-default origin port is same-origin.
    expect(
      guardRequest(mockReq({ headers: { host: 'localhost', origin: 'http://localhost' } })),
    ).toEqual({ ok: true })
    expect(
      guardRequest(mockReq({ headers: { host: 'localhost', origin: 'https://localhost' } })),
    ).toEqual({ ok: true })
    // Explicit :80 in Host vs elided default port in the origin, and vice versa.
    expect(
      guardRequest(mockReq({ headers: { host: 'localhost:80', origin: 'http://localhost' } })),
    ).toEqual({ ok: true })
    expect(
      guardRequest(mockReq({ headers: { host: 'localhost', origin: 'http://localhost:80' } })),
    ).toEqual({ ok: true })
    // Non-default origin port against a portless Host is NOT same-origin.
    expectForbidden(
      guardRequest(mockReq({ headers: { host: 'localhost', origin: 'http://localhost:3000' } })),
      'origin_mismatch',
    )
  })

  it('matches bracketed IPv6 origins via WHATWG canonical form', () => {
    expect(
      guardRequest(mockReq({ headers: { host: '[::1]:3000', origin: 'http://[::1]:3000' } })),
    ).toEqual({ ok: true })
    // URL canonicalizes the long form down to ::1.
    expect(
      guardRequest(
        mockReq({ headers: { host: '[::1]:3000', origin: 'http://[0:0:0:0:0:0:0:1]:3000' } }),
      ),
    ).toEqual({ ok: true })
  })

  it('rejects cross-origin, wrong-port, wrong-scheme, and opaque origins with 403', () => {
    const cases: Array<[host: string, origin: string]> = [
      ['127.0.0.1:3000', 'http://evil.com'],
      ['127.0.0.1:3000', 'http://evil.com:3000'],
      ['127.0.0.1:3000', 'http://127.0.0.1:9999'],
      ['127.0.0.1:3000', 'http://localhost:3000'], // host string must match exactly
      ['127.0.0.1:3000', 'chrome-extension://abcdef'],
      ['127.0.0.1:3000', 'null'], // opaque origin (sandboxed iframe / file://)
      ['127.0.0.1:3000', 'not a url'],
    ]
    for (const [host, origin] of cases) {
      expectForbidden(guardRequest(mockReq({ headers: { host, origin } })), 'origin_mismatch')
    }
  })

  it('rejects duplicate Origin headers surfaced as an array (fail closed)', () => {
    expectForbidden(
      guardRequest(
        mockReq({ headers: { origin: ['http://127.0.0.1:3000', 'http://evil.com'] } }),
      ),
      'origin_mismatch',
    )
  })

  it('allows a missing Origin (same-origin fetch and non-browser clients omit it)', () => {
    expect(guardRequest(mockReq())).toEqual({ ok: true })
  })

  it('explicitly refuses sec-fetch-site: cross-site with 403', () => {
    expectForbidden(
      guardRequest(mockReq({ headers: { 'sec-fetch-site': 'cross-site' } })),
      'cross_site',
    )
    expectForbidden(
      guardRequest(mockReq({ headers: { 'sec-fetch-site': 'Cross-Site' } })),
      'cross_site',
    )
    // Node joins duplicate non-singleton headers with ", " — any entry counts.
    expectForbidden(
      guardRequest(mockReq({ headers: { 'sec-fetch-site': 'same-origin, cross-site' } })),
      'cross_site',
    )
  })

  it('allows non-cross-site sec-fetch-site values', () => {
    for (const value of ['same-origin', 'same-site', 'none']) {
      expect(guardRequest(mockReq({ headers: { 'sec-fetch-site': value } }))).toEqual({ ok: true })
    }
  })
})

describe('guardRequest layer 4 — JSON body gate on POST/PUT/PATCH', () => {
  const jsonHeaders = { 'content-type': 'application/json' }

  it('rejects non-JSON bodies with 415', () => {
    for (const method of ['POST', 'PUT', 'PATCH']) {
      const verdict = guardRequest(
        mockReq({ method, headers: { 'content-type': 'text/plain' } }),
      )
      expect(verdict).toEqual({ ok: false, status: 415, reason: 'unsupported_media_type' })
    }
  })

  it('rejects a missing content-type on POST with 415', () => {
    expect(guardRequest(mockReq({ method: 'POST' }))).toEqual({
      ok: false,
      status: 415,
      reason: 'unsupported_media_type',
    })
  })

  it('rejects cross-site "simple request" form content types with 415', () => {
    for (const contentType of [
      'application/x-www-form-urlencoded',
      'multipart/form-data; boundary=x',
      'text/plain; charset=utf-8',
    ]) {
      expect(
        guardRequest(mockReq({ method: 'POST', headers: { 'content-type': contentType } })),
      ).toEqual({ ok: false, status: 415, reason: 'unsupported_media_type' })
    }
  })

  it('allows application/json, with charset parameter and any case', () => {
    expect(guardRequest(mockReq({ method: 'POST', headers: jsonHeaders }))).toEqual({ ok: true })
    expect(
      guardRequest(
        mockReq({ method: 'POST', headers: { 'content-type': 'application/json; charset=utf-8' } }),
      ),
    ).toEqual({ ok: true })
    expect(
      guardRequest(
        mockReq({ method: 'POST', headers: { 'content-type': 'Application/JSON; charset=UTF-8' } }),
      ),
    ).toEqual({ ok: true })
  })

  it('does not apply the media-type gate to GET (or other bodyless methods)', () => {
    expect(guardRequest(mockReq({ method: 'GET', headers: { 'content-type': 'text/plain' } }))).toEqual(
      { ok: true },
    )
    expect(guardRequest(mockReq({ method: 'DELETE' }))).toEqual({ ok: true })
    expect(guardRequest(mockReq({ method: 'HEAD' }))).toEqual({ ok: true })
  })

  it('still enforces layers 1-3 on JSON POSTs', () => {
    expectForbidden(
      guardRequest(mockReq({ method: 'POST', remoteAddress: '10.0.0.2', headers: jsonHeaders })),
      'remote_not_loopback',
    )
    expectForbidden(
      guardRequest(mockReq({ method: 'POST', headers: { ...jsonHeaders, host: 'evil.com' } })),
      'host_not_loopback',
    )
  })
})

describe('guardWriteAction — layer 5 write-action gate', () => {
  const passed: GuardVerdict = { ok: true }

  it('returns 403 inject_disabled while allowWriteActions() is false', () => {
    const verdict = guardWriteAction(passed, { allowWriteActions: () => false })
    expectForbidden(verdict, 'inject_disabled')
  })

  it('passes when allowWriteActions() is true', () => {
    expect(guardWriteAction(passed, { allowWriteActions: () => true })).toEqual({ ok: true })
  })

  it('reads the setting live on every call (no snapshot)', () => {
    let enabled = false
    const opts = { allowWriteActions: () => enabled }
    expectForbidden(guardWriteAction(passed, opts), 'inject_disabled')
    enabled = true
    expect(guardWriteAction(passed, opts)).toEqual({ ok: true })
  })

  it('passes an already-failed verdict through unchanged', () => {
    const failed: GuardVerdict = { ok: false, status: 415, reason: 'unsupported_media_type' }
    expect(guardWriteAction(failed, { allowWriteActions: () => true })).toBe(failed)
  })
})

describe('guardUpgrade — WS upgrade runs layers 1-3 only', () => {
  it('rejects a non-loopback peer', () => {
    expectForbidden(guardUpgrade(mockReq({ remoteAddress: '192.168.1.5' })), 'remote_not_loopback')
  })

  it('rejects a forged or missing Host', () => {
    expectForbidden(guardUpgrade(mockReq({ headers: { host: 'evil.com:3000' } })), 'host_not_loopback')
    expectForbidden(guardUpgrade(mockReq({ headers: { host: undefined } })), 'host_not_loopback')
  })

  it('rejects a cross-origin Origin and sec-fetch-site: cross-site', () => {
    expectForbidden(
      guardUpgrade(mockReq({ headers: { origin: 'http://evil.com' } })),
      'origin_mismatch',
    )
    expectForbidden(
      guardUpgrade(mockReq({ headers: { 'sec-fetch-site': 'cross-site' } })),
      'cross_site',
    )
  })

  it('allows a legitimate loopback upgrade with same-origin Origin', () => {
    expect(
      guardUpgrade(mockReq({ headers: { host: '127.0.0.1:3000', origin: 'http://127.0.0.1:3000' } })),
    ).toEqual({ ok: true })
  })

  it('does not apply the layer-4 media-type gate (no body on upgrades)', () => {
    expect(
      guardUpgrade(mockReq({ method: 'GET', headers: { 'content-type': 'text/plain' } })),
    ).toEqual({ ok: true })
  })
})
