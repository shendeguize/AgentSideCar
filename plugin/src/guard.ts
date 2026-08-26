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

/**
 * The minimal request surface the guard reads; mock-friendly for tests.
 *
 * `rawHeaders` is optional because host-framework adapters may expose only
 * normalized headers. Real Node IncomingMessages carry it, and callers should
 * preserve it so duplicate security-sensitive fields can be counted before
 * Node's first-wins/join behavior loses information.
 */
export type GuardableRequest = Pick<
  IncomingMessage,
  'method' | 'headers' | 'socket' | 'url'
> & {
  rawHeaders?: readonly string[]
}

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
  const candidate = addr.trim().toLowerCase()
  if (isLoopbackIpv4(candidate)) return true
  const canonical = canonicalizeIpv6(candidate)
  return canonical !== undefined && canonicalIpv6IsLoopback(canonical)
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

/**
 * Canonicalize an IPv6 literal using Node's WHATWG URL parser. The returned
 * value has no brackets (for example long-form loopback becomes `::1`).
 */
function canonicalizeIpv6(candidate: string): string | undefined {
  try {
    const hostname = new URL(`http://[${candidate}]/`).hostname.toLowerCase()
    if (!hostname.startsWith('[') || !hostname.endsWith(']')) return undefined
    return hostname.slice(1, -1)
  } catch {
    return undefined
  }
}

/** True for canonical `::1` and the existing IPv4-mapped loopback contract. */
function canonicalIpv6IsLoopback(candidate: string): boolean {
  if (candidate === '::1') return true
  const mapped = /^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/.exec(candidate)
  if (mapped === null) return false
  const high = Number.parseInt(mapped[1]!, 16)
  return high >>> 8 === 127
}

/** Parsed `host[:port]` authority in canonical tuple form. */
interface Authority {
  /** Lowercase hostname; IPv6 is WHATWG-canonical and bracketed. */
  host: string
  /** Normalized decimal port, or undefined when the header omitted it. */
  port: number | undefined
}

/** Commas, arrays and line breaks make a normalized-header fallback ambiguous. */
function headerValueIsAmbiguous(value: string): boolean {
  return value.includes(',') || /[\r\n]/.test(value)
}

/**
 * Parse an authority string (`Host` header shape). Returns undefined for
 * anything malformed: empty, bad brackets, non-numeric or out-of-range port,
 * stray colons, or list/newline ambiguity.
 */
function parseAuthority(raw: string | undefined): Authority | undefined {
  if (typeof raw !== 'string' || headerValueIsAmbiguous(raw)) return undefined
  const value = raw.trim().toLowerCase()
  if (!value) return undefined

  let host: string
  let portPart: string | undefined
  if (value.startsWith('[')) {
    const close = value.indexOf(']')
    if (close <= 1) return undefined
    const canonical = canonicalizeIpv6(value.slice(1, close))
    if (canonical === undefined) return undefined
    host = `[${canonical}]`
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
    if (isLoopbackIpv4(host)) {
      host = host
        .split('.')
        .map((part) => String(Number(part)))
        .join('.')
    }
  }

  let port: number | undefined
  if (portPart !== undefined) {
    if (!/^\d+$/.test(portPart)) return undefined
    const significant = portPart.replace(/^0+/, '') || '0'
    if (significant.length > 5) return undefined
    port = Number(significant)
    if (port < 1 || port > 65535) return undefined
  }
  return { host, port }
}

/** True when a parsed authority host names loopback. */
function authorityIsLoopback(host: string): boolean {
  if (host === 'localhost') return true
  if (host.startsWith('[') && host.endsWith(']')) {
    return canonicalIpv6IsLoopback(host.slice(1, -1))
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
 * Same-origin check between an `Origin` header value and the cleartext HTTP
 * request's `Host` authority. The scheme must be http; host must match exactly
 * (WHATWG-normalized: lowercase, IPv6 canonical bracketed form) and the
 * effective ports must agree. A portless `Host` has effective port 80.
 */
function originMatchesAuthority(origin: string, authority: Authority): boolean {
  if (headerValueIsAmbiguous(origin)) return false
  let url: URL
  try {
    url = new URL(origin)
  } catch {
    return false // includes the opaque `Origin: null`
  }
  if (url.protocol !== 'http:') return false

  if (url.hostname.toLowerCase() !== authority.host) return false

  const originPort = url.port === '' ? 80 : Number(url.port)
  if (!Number.isInteger(originPort) || originPort < 1 || originPort > 65535) return false
  const authorityPort = authority.port ?? 80
  return originPort === authorityPort
}

/** Reject when any (possibly `, `-joined multi-value) entry is `cross-site`. */
function declaresCrossSite(secFetchSite: string | string[] | undefined): boolean {
  if (secFetchSite === undefined) return false
  const values = Array.isArray(secFetchSite) ? secFetchSite : [secFetchSite]
  return values.some((value) =>
    value.split(',').some((entry) => entry.trim().toLowerCase() === 'cross-site'),
  )
}

/** Return all case-insensitive raw-header values, or null for a malformed list. */
function rawHeaderValues(rawHeaders: readonly string[], wantedName: string): string[] | null {
  if (rawHeaders.length % 2 !== 0) return null
  const values: string[] = []
  for (let index = 0; index < rawHeaders.length; index += 2) {
    const name = rawHeaders[index]
    const value = rawHeaders[index + 1]
    if (name === undefined || value === undefined) return null
    if (name.toLowerCase() === wantedName) values.push(value)
  }
  return values
}

type ReachabilityRequest = Pick<GuardableRequest, 'headers' | 'socket' | 'rawHeaders'>

/** Layers 1-3: remote loopback, Host authority, Origin/sec-fetch-site. */
function guardReachability(req: ReachabilityRequest): GuardVerdict {
  // Layer 1 — transport: only loopback peers, even if dsh binds 0.0.0.0.
  if (!isLoopbackAddress(req.socket?.remoteAddress ?? undefined)) {
    return forbid('remote_not_loopback')
  }

  // Layer 2 — exactly one case-insensitive raw Host is required when Node's
  // lossless header list is available. The normalized fallback rejects
  // arrays/list joins/newlines instead of relying on first-wins behavior.
  let hostHeader: string | undefined
  if (req.rawHeaders !== undefined) {
    const values = rawHeaderValues(req.rawHeaders, 'host')
    if (values === null || values.length !== 1) return forbid('host_not_loopback')
    hostHeader = values[0]
  } else {
    const normalized = req.headers.host
    hostHeader =
      typeof normalized === 'string' && !headerValueIsAmbiguous(normalized)
        ? normalized
        : undefined
  }
  const authority = parseAuthority(hostHeader)
  if (!authority || !authorityIsLoopback(authority.host)) {
    return forbid('host_not_loopback')
  }

  // Layer 3 — explicit cross-site metadata takes precedence over Origin
  // validation, then Origin (when present) must be same-origin with Host.
  if (declaresCrossSite(req.headers['sec-fetch-site'])) {
    return forbid('cross_site')
  }

  let origin: string | undefined
  if (req.rawHeaders !== undefined) {
    const values = rawHeaderValues(req.rawHeaders, 'origin')
    if (values === null || values.length > 1) return forbid('origin_mismatch')
    origin = values[0]
  } else {
    const normalized = req.headers.origin
    if (Array.isArray(normalized)) return forbid('origin_mismatch')
    origin = normalized
  }
  if (origin !== undefined && !originMatchesAuthority(origin, authority)) {
    return forbid('origin_mismatch')
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
  req: Pick<IncomingMessage, 'headers' | 'socket'> & {
    rawHeaders?: readonly string[]
  },
): GuardVerdict {
  return guardReachability(req)
}
