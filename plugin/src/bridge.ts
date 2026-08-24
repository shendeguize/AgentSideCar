/**
 * Sidecar Unix-socket bridge (host half, transport layer only).
 *
 * Pure `node:net`; deliberately free of any cordis/dsh import so the
 * protocol client stays testable in isolation and reusable outside the
 * plugin context.
 *
 * Protocol source of truth (verified against sidecar source, not docs):
 * - Requests are single-line JSON `{"op":"ping"|"status"|"subscribe"}`
 *   terminated by `\n` (`sidecar/daemon.py` `_handle_client`).
 * - `ping`/`status` answer with exactly one JSON line. The official client
 *   (`sidecar/client.py`) opens one fresh connection per op and closes it
 *   after the response; we mirror that semantic.
 * - `subscribe` answers with an ack line `{"ok":true,"op":"subscribe"}`
 *   and then streams JSONL event objects until either side disconnects
 *   (`sidecar/daemon.py` `_serve_subscription`). The per-subscriber queue
 *   is bounded (256, drop-oldest) and drops are NOT signalled on the wire
 *   (`sidecar/bus.py`), which is why the stream is a trigger signal only;
 *   `status` snapshots remain the source of truth (design §4.b / ADR-2).
 * - Daemon-declared errors arrive as `{"ok":false,"error":{code,message}}`.
 *
 * @module
 */

import { createConnection, type Socket } from 'node:net'

// ---------------------------------------------------------------------------
// Wire types (mirroring sidecar/model.py and sidecar/daemon.py responses).
// ---------------------------------------------------------------------------

/** Health of the subscribe stream as observed by the host. */
export type StreamHealth = 'ok' | 'degraded' | 'unknown'

/** HTTP listener details advertised by `ping` (daemon `_http_ping_payload`). */
export interface HttpPingInfo {
  enabled: boolean
  host?: string
  port?: number
}

/** Typed `ping` response (pid/version/http self-description). */
export interface PingInfo {
  pid: number
  version: string
  http: HttpPingInfo
}

/** One session row from a `status` snapshot (`Session.to_dict`). */
export interface SessionRow {
  agent: string
  session_id: string
  project: string
  transcript: string
  updated_at: number
  title: string
  status: string
  extra: Record<string, unknown>
  parent_id: string | null
}

/** One normalized event from the subscribe stream (`Event.to_dict`). */
export interface SidecarEvent {
  ts: string
  agent: string
  session_id: string
  kind: string
  text: string
  extra: Record<string, unknown>
}

/** Parsed `status` response. */
export interface StatusSnapshot {
  sessions: SessionRow[]
  scanErrors: Array<Record<string, unknown>>
  tailErrors: Array<Record<string, unknown>>
  diagnostics: Array<Record<string, unknown>>
}

/**
 * Client-side drop reasons. The daemon never signals its own queue drops
 * (`sidecar/bus.py` keeps `dropped` server-internal), so these only cover
 * lines this client had to discard to protect itself.
 */
export type DropReason = 'line_too_long' | 'invalid_json' | 'invalid_event'

/** Callbacks for one subscribe stream. Never invoked synchronously from `subscribe()`. */
export interface SubscribeHandlers {
  onEvent(ev: SidecarEvent): void
  /** Called once after the daemon ack has been validated. */
  onReady?(): void
  onDrop?(reason: DropReason): void
  /** Called exactly once when the stream ends (error, disconnect, or `close()`). */
  onClose?(err?: Error): void
}

/** Handle for one live subscription. */
export interface Subscription {
  close(): void
}

export interface SidecarSocketClientOptions {
  /** Absolute path to `daemon.sock` (default runtime dir is `~/.agent_sidecar`, redirectable via AGENT_SIDECAR_RUNTIME_DIR — resolved by the caller). */
  socketPath: string
  /** Connect/handshake/response bound; the stream idles unbounded after the subscribe ack, matching sidecar/client.py. */
  timeoutMs?: number
  /** Bound for a single JSONL line (sidecar caps responses at 32 MiB). */
  maxLineBytes?: number
}

/** Matches `DEFAULT_TIMEOUT = 1.0` in sidecar/client.py. */
export const DEFAULT_TIMEOUT_MS = 1000
/** Matches `MAX_RESPONSE_BYTES` in sidecar/client.py. */
export const DEFAULT_MAX_LINE_BYTES = 32 * 1024 * 1024

// ---------------------------------------------------------------------------
// Parsing helpers.
// ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function parseHttpPingInfo(value: unknown): HttpPingInfo | null {
  if (value === null || value === undefined) return { enabled: false }
  if (!isRecord(value) || typeof value['enabled'] !== 'boolean') return null
  if (value['enabled'] === false) return { enabled: false }
  const host = value['host']
  const port = value['port']
  if (typeof host !== 'string' || host === '') return null
  if (typeof port !== 'number' || !Number.isInteger(port) || port < 1 || port > 65535) return null
  return { enabled: true, host, port }
}

function parsePingInfo(value: unknown): PingInfo | null {
  if (!isRecord(value) || value['ok'] !== true || value['op'] !== 'ping') return null
  const pid = value['pid']
  if (typeof pid !== 'number' || !Number.isInteger(pid) || pid <= 0) return null
  const rawVersion = value['version']
  let version: string
  if (rawVersion === null || rawVersion === undefined) version = ''
  else if (typeof rawVersion === 'string') version = rawVersion
  else return null
  const http = parseHttpPingInfo(value['http'])
  if (http === null) return null
  return { pid, version, http }
}

/**
 * Normalize one raw status row. Rows without a usable `session_id` are
 * skipped by the caller (the daemon model guarantees the field, so this
 * only defends against wire corruption).
 */
function parseSessionRow(value: unknown): SessionRow | null {
  if (!isRecord(value)) return null
  const sessionId = value['session_id']
  if (typeof sessionId !== 'string' || sessionId === '') return null
  const updatedAt = value['updated_at']
  return {
    agent: typeof value['agent'] === 'string' ? value['agent'] : '',
    session_id: sessionId,
    project: typeof value['project'] === 'string' ? value['project'] : '',
    transcript: typeof value['transcript'] === 'string' ? value['transcript'] : '',
    updated_at: typeof updatedAt === 'number' && Number.isFinite(updatedAt) ? updatedAt : 0,
    title: typeof value['title'] === 'string' ? value['title'] : '',
    status: typeof value['status'] === 'string' ? value['status'] : 'idle',
    extra: isRecord(value['extra']) ? value['extra'] : {},
    parent_id: typeof value['parent_id'] === 'string' ? value['parent_id'] : null,
  }
}

function parseRecordList(value: unknown): Array<Record<string, unknown>> | null {
  if (value === undefined) return []
  if (!Array.isArray(value)) return null
  const out: Array<Record<string, unknown>> = []
  for (const item of value) {
    if (!isRecord(item)) return null
    out.push(item)
  }
  return out
}

function parseStatusSnapshot(value: unknown): StatusSnapshot | null {
  if (!isRecord(value) || value['ok'] !== true) return null
  const rawSessions = value['sessions']
  if (!Array.isArray(rawSessions)) return null
  const sessions: SessionRow[] = []
  for (const raw of rawSessions) {
    // Mirror sidecar/client.py: a non-object row invalidates the response.
    if (!isRecord(raw)) return null
    const row = parseSessionRow(raw)
    if (row !== null) sessions.push(row)
  }
  const scanErrors = parseRecordList(value['scan_errors'])
  const tailErrors = parseRecordList(value['tail_errors'])
  if (scanErrors === null || tailErrors === null) return null
  const diagnostics = parseRecordList(value['diagnostics'])
  return { sessions, scanErrors, tailErrors, diagnostics: diagnostics ?? [] }
}

function parseEvent(value: Record<string, unknown>): SidecarEvent | null {
  const ts = value['ts']
  const agent = value['agent']
  const sessionId = value['session_id']
  const kind = value['kind']
  const text = value['text']
  if (
    typeof ts !== 'string' ||
    typeof agent !== 'string' ||
    typeof sessionId !== 'string' ||
    typeof kind !== 'string' ||
    typeof text !== 'string'
  ) {
    return null
  }
  return {
    ts,
    agent,
    session_id: sessionId,
    kind,
    text,
    extra: isRecord(value['extra']) ? value['extra'] : {},
  }
}

function daemonError(value: Record<string, unknown>): Error {
  const error = value['error']
  if (isRecord(error)) {
    const code = String(error['code'] ?? 'daemon_error')
    const message = String(error['message'] ?? code)
    return new Error(`${code}: ${message}`)
  }
  return new Error(String(error ?? 'daemon_error'))
}

// ---------------------------------------------------------------------------
// Bounded JSONL line splitter.
// ---------------------------------------------------------------------------

const NEWLINE = 0x0a
const EMPTY = Buffer.alloc(0)

/**
 * Splits a byte stream into newline-terminated lines with a hard size
 * bound. An over-long line is discarded (signalled once via `onOverflow`)
 * and the splitter resynchronizes at the next newline, so one oversized
 * record cannot take down the whole stream or balloon memory.
 */
class LineBuffer {
  private pending: Buffer = EMPTY
  private dropping = false

  constructor(
    private readonly maxBytes: number,
    private readonly onLine: (line: Buffer) => void,
    private readonly onOverflow: () => void,
  ) {}

  push(chunk: Buffer): void {
    this.pending = this.pending.length === 0 ? chunk : Buffer.concat([this.pending, chunk])
    for (;;) {
      const idx = this.pending.indexOf(NEWLINE)
      if (idx < 0) {
        if (this.pending.length > this.maxBytes) {
          this.pending = EMPTY
          if (!this.dropping) {
            this.dropping = true
            this.onOverflow()
          }
        }
        return
      }
      const line = this.pending.subarray(0, idx)
      this.pending = this.pending.subarray(idx + 1)
      if (this.dropping) {
        // Tail of a line that already overflowed; resynchronize silently.
        this.dropping = false
        continue
      }
      if (line.length > this.maxBytes) {
        this.onOverflow()
        continue
      }
      this.onLine(line)
    }
  }
}

// ---------------------------------------------------------------------------
// Socket client.
// ---------------------------------------------------------------------------

/**
 * Minimal daemon client: one fresh connection per op (matching the
 * semantics of `sidecar/client.py`), single-line JSON requests, JSONL
 * responses, bounded reads. All request/response failures resolve to
 * `null` instead of throwing — the caller (Reconciler/Supervisor) owns
 * the health policy.
 */
export class SidecarSocketClient {
  readonly socketPath: string
  readonly timeoutMs: number
  readonly maxLineBytes: number

  constructor(opts: SidecarSocketClientOptions) {
    this.socketPath = opts.socketPath
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS
    this.maxLineBytes = opts.maxLineBytes ?? DEFAULT_MAX_LINE_BYTES
    if (this.timeoutMs <= 0 || this.maxLineBytes <= 0) {
      throw new RangeError('client bounds are invalid')
    }
  }

  /** `ping` op; `null` on refusal, timeout, or an invalid/error response. */
  async ping(): Promise<PingInfo | null> {
    return parsePingInfo(await this.requestLine('ping'))
  }

  /** `status` op; `null` on refusal, timeout, or an invalid/error response. */
  async status(): Promise<StatusSnapshot | null> {
    return parseStatusSnapshot(await this.requestLine('status'))
  }

  /**
   * Open a subscribe stream: write the op, validate the ack, then deliver
   * each JSONL event through `handlers.onEvent`. After the ack the
   * connection may idle indefinitely (no timeout), matching
   * sidecar/client.py which disables its socket timeout post-handshake.
   */
  subscribe(handlers: SubscribeHandlers): Subscription {
    let closed = false
    let ready = false
    const socket: Socket = createConnection({ path: this.socketPath })

    const finish = (err?: Error): void => {
      if (closed) return
      closed = true
      socket.destroy()
      // Defer so `close()` calls from inside subscribe setup can never
      // observe a synchronous onClose.
      queueMicrotask(() => handlers.onClose?.(err))
    }

    const lines = new LineBuffer(
      this.maxLineBytes,
      (line) => {
        if (closed) return
        let value: unknown
        try {
          value = JSON.parse(line.toString('utf8'))
        } catch {
          if (!ready) {
            finish(new Error('daemon returned an invalid subscribe acknowledgement'))
            return
          }
          handlers.onDrop?.('invalid_json')
          return
        }
        if (!ready) {
          if (isRecord(value) && value['ok'] === true && value['op'] === 'subscribe' && !('error' in value)) {
            ready = true
            socket.setTimeout(0)
            handlers.onReady?.()
          } else if (isRecord(value) && value['ok'] === false) {
            finish(daemonError(value))
          } else {
            finish(new Error('daemon returned an invalid subscribe acknowledgement'))
          }
          return
        }
        if (!isRecord(value)) {
          handlers.onDrop?.('invalid_event')
          return
        }
        if (value['ok'] === false) {
          finish(daemonError(value))
          return
        }
        const event = parseEvent(value)
        if (event === null) {
          handlers.onDrop?.('invalid_event')
          return
        }
        handlers.onEvent(event)
      },
      () => {
        if (!closed) handlers.onDrop?.('line_too_long')
      },
    )

    socket.setTimeout(this.timeoutMs)
    socket.once('timeout', () => {
      if (!ready) finish(new Error('subscribe handshake timed out'))
    })
    socket.once('error', (err: Error) => finish(err))
    socket.once('close', () => finish())
    socket.once('connect', () => {
      socket.write('{"op":"subscribe"}\n')
    })
    socket.on('data', (chunk: Buffer) => lines.push(chunk))

    return { close: () => finish() }
  }

  /** Send one single-line JSON request and read one JSONL response line. */
  private requestLine(op: 'ping' | 'status'): Promise<unknown> {
    return new Promise<unknown>((resolve) => {
      let settled = false
      const socket: Socket = createConnection({ path: this.socketPath })
      const finish = (value: unknown): void => {
        if (settled) return
        settled = true
        socket.destroy()
        resolve(value)
      }
      const lines = new LineBuffer(
        this.maxLineBytes,
        (line) => {
          let value: unknown
          try {
            value = JSON.parse(line.toString('utf8'))
          } catch {
            value = null
          }
          finish(value)
        },
        () => finish(null),
      )
      socket.setTimeout(this.timeoutMs)
      socket.once('timeout', () => finish(null))
      socket.once('error', () => finish(null))
      socket.once('close', () => finish(null))
      socket.once('connect', () => {
        socket.write(JSON.stringify({ op }) + '\n')
      })
      socket.on('data', (chunk: Buffer) => lines.push(chunk))
    })
  }
}

// ---------------------------------------------------------------------------
// Reconciler: snapshots are truth, the stream is a trigger (ADR-2).
// ---------------------------------------------------------------------------

/** What the Reconciler needs from the session cache (structurally satisfied by SessionStore). */
export interface ReconcilerStore {
  applySnapshot(rows: SessionRow[]): void
  applyEvent(ev: SidecarEvent): void
  setStreamHealth(health: StreamHealth): void
  hasWorkingSessions(): boolean
}

/** What the Reconciler needs from the client (structurally satisfied by SidecarSocketClient). */
export interface ReconcilerClient {
  status(): Promise<StatusSnapshot | null>
  subscribe(handlers: SubscribeHandlers): Subscription
}

export interface ReconcilerOptions {
  /** Snapshot cadence while any session is `working`. */
  activeMs?: number
  /** Snapshot cadence otherwise. */
  idleMs?: number
  /** Debounce for the event-triggered early reconcile. */
  debounceMs?: number
  /** First reconnect delay after the subscribe stream drops. */
  reconnectMinMs?: number
  /** Reconnect delay cap (bounded backoff, never circuit-broken). */
  reconnectMaxMs?: number
}

export const DEFAULT_ACTIVE_MS = 2000
export const DEFAULT_IDLE_MS = 10000
export const DEFAULT_DEBOUNCE_MS = 200
export const DEFAULT_RECONNECT_MIN_MS = 1000
export const DEFAULT_RECONNECT_MAX_MS = 30000

/**
 * Dual-cadence status reconciliation plus subscribe-stream supervision:
 * - `status` snapshots run on an active (any working session) or idle
 *   cadence and are applied as the authoritative full state.
 * - each subscribe event is folded into the store as a hint and schedules
 *   one debounced early reconcile.
 * - a dropped stream marks `streamHealth=degraded` and reconnects with
 *   bounded exponential backoff (1s doubling to a 30s cap, retrying
 *   forever); a validated ack restores `streamHealth=ok` and resets the
 *   backoff.
 */
export class Reconciler {
  private readonly activeMs: number
  private readonly idleMs: number
  private readonly debounceMs: number
  private readonly reconnectMinMs: number
  private readonly reconnectMaxMs: number

  private running = false
  private backoffMs: number
  private pollTimer: ReturnType<typeof setTimeout> | null = null
  private kickTimer: ReturnType<typeof setTimeout> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private subscription: Subscription | null = null
  private reconcileInFlight = false
  private reconcileQueued = false

  constructor(
    private readonly client: ReconcilerClient,
    private readonly store: ReconcilerStore,
    opts: ReconcilerOptions = {},
  ) {
    this.activeMs = opts.activeMs ?? DEFAULT_ACTIVE_MS
    this.idleMs = opts.idleMs ?? DEFAULT_IDLE_MS
    this.debounceMs = opts.debounceMs ?? DEFAULT_DEBOUNCE_MS
    this.reconnectMinMs = opts.reconnectMinMs ?? DEFAULT_RECONNECT_MIN_MS
    this.reconnectMaxMs = opts.reconnectMaxMs ?? DEFAULT_RECONNECT_MAX_MS
    this.backoffMs = this.reconnectMinMs
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.backoffMs = this.reconnectMinMs
    this.openSubscription()
    void this.reconcileNow()
  }

  stop(): void {
    if (!this.running) return
    this.running = false
    if (this.pollTimer !== null) clearTimeout(this.pollTimer)
    if (this.kickTimer !== null) clearTimeout(this.kickTimer)
    if (this.reconnectTimer !== null) clearTimeout(this.reconnectTimer)
    this.pollTimer = null
    this.kickTimer = null
    this.reconnectTimer = null
    const subscription = this.subscription
    this.subscription = null
    subscription?.close()
  }

  private async reconcileNow(): Promise<void> {
    if (!this.running) return
    if (this.reconcileInFlight) {
      this.reconcileQueued = true
      return
    }
    this.reconcileInFlight = true
    try {
      const snapshot = await this.client.status()
      if (this.running && snapshot !== null) {
        this.store.applySnapshot(snapshot.sessions)
      }
    } finally {
      this.reconcileInFlight = false
    }
    if (!this.running) return
    if (this.reconcileQueued) {
      this.reconcileQueued = false
      void this.reconcileNow()
      return
    }
    this.scheduleNext()
  }

  private scheduleNext(): void {
    if (this.pollTimer !== null) clearTimeout(this.pollTimer)
    const delay = this.store.hasWorkingSessions() ? this.activeMs : this.idleMs
    this.pollTimer = setTimeout(() => {
      this.pollTimer = null
      void this.reconcileNow()
    }, delay)
  }

  /** Schedule one debounced early reconcile (subscribe events are hints). */
  private kick(): void {
    if (!this.running || this.kickTimer !== null) return
    this.kickTimer = setTimeout(() => {
      this.kickTimer = null
      void this.reconcileNow()
    }, this.debounceMs)
  }

  private openSubscription(): void {
    if (!this.running) return
    this.subscription = this.client.subscribe({
      onReady: () => {
        if (!this.running) return
        this.backoffMs = this.reconnectMinMs
        this.store.setStreamHealth('ok')
      },
      onEvent: (ev) => {
        if (!this.running) return
        this.store.applyEvent(ev)
        this.kick()
      },
      onDrop: () => {
        // A discarded line means missed information: reconcile early.
        this.kick()
      },
      onClose: () => {
        this.subscription = null
        if (!this.running) return
        this.store.setStreamHealth('degraded')
        const delay = this.backoffMs
        this.backoffMs = Math.min(this.backoffMs * 2, this.reconnectMaxMs)
        this.reconnectTimer = setTimeout(() => {
          this.reconnectTimer = null
          this.openSubscription()
        }, delay)
      },
    })
  }
}
