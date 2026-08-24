/**
 * Self-contained request guard for the plugin's self-registered routes.
 *
 * dsh's webServer has no authentication layer and its `/api` trust fence
 * (connection package) does not cover plugin-registered routes, so every
 * route this plugin opens must carry its own guard (design doc §4.f / §8).
 *
 * The five layers defend against *browser-mediated* attacks (CSRF, DNS
 * rebinding, cross-site requests). They deliberately do NOT claim to stop a
 * local process that opens its own TCP connection to loopback — that is the
 * structural trust posture of the unauthenticated dsh webServer (ADR-8).
 *
 * This module is pure `node:http` types on purpose: no cordis/dsh imports,
 * so it stays unit-testable with plain mock objects.
 *
 * @module
 */

import type { IncomingMessage } from 'node:http'

/** Dynamic knobs consulted by the write-action gate (layer 5). */
export interface GuardOptions {
  /**
   * Read the `inject.enabled` setting at call time (live setting — must not
   * be snapshotted at plugin startup).
   */
  allowWriteActions(): boolean
}

/** Outcome of a guard evaluation; `status`/`reason` map onto the HTTP reply. */
export type GuardVerdict =
  | { ok: true }
  | { ok: false; status: number; reason: string }

/** The minimal request surface the guard reads; mock-friendly for tests. */
export type GuardableRequest = Pick<
  IncomingMessage,
  'method' | 'headers' | 'socket' | 'url'
>

const OK: GuardVerdict = { ok: true }

const forbid = (reason: string): GuardVerdict => ({ ok: false, status: 403, reason })

/** Methods whose body is a state-changing payload (layer 4 media-type gate). */
const BODY_METHODS = new Set(['POST', 'PUT', 'PATCH'])

/**
 * True when `addr` (a `socket.remoteAddress` value) is a loopback address:
 * IPv4 `127.0.0.0/8`, IPv6 `::1`, or the IPv4-mapped form `::ffff:127.x.y.z`
 * that Node reports on dual-stack listeners. Anything unparsable is `false`
 * (fail closed).
 */
export function isLoopbackAddress(addr: string | undefined): boolean {
  if (!addr) return false
  let candidate = addr.trim().toLowerCase()
  if (candidate.startsWith('::ffff:')) candidate = candidate.slice('::ffff:'.length)
  if (candidate === '::1') return true
  return isLoopbackIpv4(candidate)
}

/** Strict dotted-quad check for `127.0.0.0/8`. */
function isLoopbackIpv4(candidate: string): boolean {
  const parts = candidate.split('.')
  if (parts.length !== 4) return false
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part) || Number(part) > 255) return false
  }
  return parts[0] === '127'
}

/** Parsed `host[:port]` authority; `host` is lowercased, IPv6 keeps brackets. */
interface Authority {
  host: string
  /** Explicit port digits, or undefined when the header omitted the port. */
  port: string | undefined
}

/**
 * Parse an authority string (`Host` header shape). Returns undefined for
 * anything malformed: empty, bad brackets, non-numeric or out-of-range port,
 * stray colons. Node keeps only the first `Host` header on duplicates, so a
 * single string is the full input space here.
 */
function parseAuthority(raw: string | undefined): Authority | undefined {
  if (typeof raw !== 'string') return undefined
  const value = raw.trim().toLowerCase()
  if (!value) return undefined

  let host: string
  let portPart: string | undefined
  if (value.startsWith('[')) {
    const close = value.indexOf(']')
    if (close <= 1) return undefined
    host = value.slice(0, close + 1)
    const rest = value.slice(close + 1)
    if (rest) {
      if (!rest.startsWith(':')) return undefined
      portPart = rest.slice(1)
    }
  } else {
    const colon = value.indexOf(':')
    if (colon === -1) {
      host = value
    } else {
      host = value.slice(0, colon)
      portPart = value.slice(colon + 1)
      if (portPart.includes(':')) return undefined // unbracketed IPv6 in Host is invalid
    }
    if (!host || /[\s/@#?\\]/.test(host)) return undefined
  }

  if (portPart !== undefined) {
    if (!/^\d{1,5}$/.test(portPart)) return undefined
    const num = Number(portPart)
    if (num < 1 || num > 65535) return undefined
  }
  return { host, port: portPart }
}

/** True when a parsed authority host names loopback. */
function authorityIsLoopback(host: string): boolean {
  if (host === 'localhost') return true
  if (host.startsWith('[') && host.endsWith(']')) {
    return isLoopbackAddress(host.slice(1, -1))
  }
  return isLoopbackIpv4(host)
}

/**
 * True when the `Host` header names a loopback authority: `localhost`, an
 * IPv4 `127.0.0.0/8` literal, or a bracketed loopback IPv6 literal — each
 * optionally with a port. Missing/malformed headers are `false` (fail
 * closed; this is the DNS-rebinding gate).
 */
export function hostIsLoopback(hostHeader: string | undefined): boolean {
  const authority = parseAuthority(hostHeader)
  if (!authority) return false
  return authorityIsLoopback(authority.host)
}

/**
 * Same-origin check between an `Origin` header value and the request's
 * `Host` authority. Scheme may be http or https; host must match exactly
 * (WHATWG-normalized: lowercase, IPv6 canonical bracketed form) and the
 * effective ports must agree. A `Host` without a port accepts either
 * scheme-default origin port (80/443), covering default-port elision.
 */
function originMatchesAuthority(origin: string, authority: Authority): boolean {
  let url: URL
  try {
    url = new URL(origin)
  } catch {
    return false // includes the opaque `Origin: null`
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return false

  if (url.hostname.toLowerCase() !== authority.host) return false

  const originPort = url.port || (url.protocol === 'https:' ? '443' : '80')
  if (authority.port !== undefined) return originPort === authority.port
  return originPort === '80' || originPort === '443'
}

/** Reject when any (possibly `, `-joined multi-value) entry is `cross-site`. */
function declaresCrossSite(secFetchSite: string | string[] | undefined): boolean {
  if (secFetchSite === undefined) return false
  const values = Array.isArray(secFetchSite) ? secFetchSite : [secFetchSite]
  return values.some((value) =>
    value.split(',').some((entry) => entry.trim().toLowerCase() === 'cross-site'),
  )
}

/** Layers 1-3: remote loopback, Host authority, Origin/sec-fetch-site. */
function guardReachability(req: {
  headers: IncomingMessage['headers']
  socket: IncomingMessage['socket']
}): GuardVerdict {
  // Layer 1 — transport: only loopback peers, even if dsh binds 0.0.0.0.
  if (!isLoopbackAddress(req.socket?.remoteAddress ?? undefined)) {
    return forbid('remote_not_loopback')
  }

  // Layer 2 — Host must be a loopback authority (DNS-rebinding defence).
  const hostHeader = req.headers.host
  const authority =
    typeof hostHeader === 'string' ? parseAuthority(hostHeader) : undefined
  if (!authority || !authorityIsLoopback(authority.host)) {
    return forbid('host_not_loopback')
  }

  // Layer 3 — Origin, when present, must be same-origin with Host.
  const origin = req.headers.origin
  if (origin !== undefined) {
    // Duplicate Origin headers (joined or arrayed by Node) never parse as a
    // single valid origin — fail closed.
    if (Array.isArray(origin) || !originMatchesAuthority(origin, authority)) {
      return forbid('origin_mismatch')
    }
  }
  if (declaresCrossSite(req.headers['sec-fetch-site'])) {
    return forbid('cross_site')
  }

  return OK
}

/**
 * Full HTTP-route guard, layers 1-4 in order:
 *
 * 1. `socket.remoteAddress` must be loopback → else 403;
 * 2. `Host` must be a loopback authority → else 403;
 * 3. `Origin` (when present) must be same-origin with Host, and
 *    `sec-fetch-site: cross-site` is explicitly refused → else 403;
 * 4. POST/PUT/PATCH must carry `content-type: application/json` (charset
 *    parameter allowed) → else 415.
 *
 * Layer 5 (the write-action gate) is {@link guardWriteAction}: routes call
 * it only for state-changing actions, chaining this verdict through.
 *
 * @param req - the incoming request (or a structural mock in tests).
 * @param _opts - reserved; layers 1-4 need no dynamic settings today.
 */
export function guardRequest(
  req: GuardableRequest,
  _opts?: GuardOptions,
): GuardVerdict {
  const reachability = guardReachability(req)
  if (!reachability.ok) return reachability

  // Layer 4 — CSRF mitigation: body-bearing methods must be JSON, which
  // forces a CORS preflight and blocks cross-site "simple" form posts.
  const method = (req.method ?? '').toUpperCase()
  if (BODY_METHODS.has(method)) {
    const contentType = req.headers['content-type']
    const mime =
      typeof contentType === 'string'
        ? contentType.split(';', 1)[0]?.trim().toLowerCase()
        : undefined
    if (mime !== 'application/json') {
      return { ok: false, status: 415, reason: 'unsupported_media_type' }
    }
  }

  return OK
}

/**
 * Layer 5 — write-action gate. Chains an earlier verdict (typically from
 * {@link guardRequest}) and then requires `inject.enabled` to be on, read
 * live via {@link GuardOptions.allowWriteActions}. The one-time confirmToken
 * check is the M2 inject gateway's job, not this layer's.
 *
 * @param verdictCtx - verdict from the preceding layers; failures pass through.
 * @param opts - dynamic settings source; gate is closed when it says so.
 */
export function guardWriteAction(
  verdictCtx: GuardVerdict,
  opts: GuardOptions,
): GuardVerdict {
  if (!verdictCtx.ok) return verdictCtx
  if (!opts.allowWriteActions()) return forbid('inject_disabled')
  return OK
}

/**
 * WS upgrade guard: layers 1-3 only (an upgrade has no JSON body to gate).
 * A failing verdict means the caller must destroy the socket.
 */
export function guardUpgrade(
  req: Pick<IncomingMessage, 'headers' | 'socket'>,
): GuardVerdict {
  return guardReachability(req)
}
