/**
 * Sidecar Unix-socket bridge (host half, transport layer only).
 *
 * Pure `node:net`; deliberately free of any cordis/dsh import so the
 * protocol client stays testable in isolation and reusable outside the
 * plugin context.
 *
 * Protocol source of truth (verified against sidecar source, not docs):
 * - Requests are single-line JSON
 *   `{"op":"ping"|"status"|"replay"|"subscribe"}` terminated by `\n`
 *   (`sidecar/daemon.py` `_handle_client`).
 * - `ping`/`status`/`replay` answer with exactly one JSON line. The official
 *   client (`sidecar/client.py`) opens one fresh connection per op and
 *   closes it after the response; we mirror that semantic.
 * - `replay {session_id, after_seq, limit}` (T5.2) answers one bounded page
 *   `{events, last_seq, truncated, count, agent, ...}` sourced from the
 *   session adapter's own transcript replay (daemon `_replay_response`):
 *   a compressed-stream decode for dsh, a line-ordinal scan of the
 *   append-only JSONL for the other agents. Unlike ping/status, {@link
 *   SidecarSocketClient.replay} REJECTS with a coded
 *   {@link SidecarDaemonError} instead of resolving null: the daemon error
 *   vocabulary (`unknown_session` / `replay_unsupported` / `replay_failed`
 *   / `invalid_request`) must reach the caller verbatim so the fusion
 *   layer can degrade honestly (design §4.b.2).
 * - `subscribe` answers with an ack line `{"ok":true,"op":"subscribe"}`
 *   and then streams JSONL event objects until either side disconnects
 *   (`sidecar/daemon.py` `_serve_subscription`). An optional
 *   `{"agents":[...]}` allowlist asks the daemon to stream only those
 *   agents' events (server-side filter, daemon `_parse_subscribe_agents`);
 *   the ack then echoes the sorted list. The per-subscriber queue is
 *   bounded (256, drop-oldest) and drops are NOT signalled on the wire
 *   (`sidecar/bus.py`), which is why the stream is a trigger signal only;
 *   `status` snapshots remain the source of truth (design §4.b / ADR-2).
 * - Daemon-declared errors arrive as `{"ok":false,"error":{code,message}}`.
 *
 * @module
 */

import { createConnection, type Socket } from 'node:net'
import { sanitizeSessionExtra } from './inject-eligibility.ts'

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
  /**
   * The `__version__` in the daemon's own source tree — what it would load
   * if restarted. Absent when the daemon cannot read its source (a packaged
   * form), which is not the same as "matches".
   */
  sourceVersion?: string
  /**
   * The daemon's tree no longer matches the code it loaded. Between
   * releases the version cannot move, so this is the only drift signal.
   */
  sourceChanged?: boolean
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
  /** Fleet/merge provenance; consumed by eligibility and never projected to the board wire. */
  host?: string
  remote?: boolean
  source?: string
  remote_alias?: string
  remote_host?: string
  /** Set only when injection-sensitive wire fields could not be represented safely. */
  invalid_session?: true
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

/** One archived session identity from the daemon registry (`archive.py`). */
export interface ArchiveEntry {
  agent: string
  session_id: string
  /** Epoch seconds of the archive decision. */
  archived_at: number
  reason: string
}

/** A session row the daemon is currently hiding from the board. */
export type ArchivedSessionRow = SessionRow & {
  archived_at: number
  archive_reason: string
}

/** Archive policy advertised by `status` (daemon `_status_response`). */
export interface ArchivePolicy {
  auto: boolean
  autoAfterSeconds: number
  defaultIdleSeconds: number
}

/** Parsed `archive_preview` response: the candidate set plus its token. */
export interface ArchivePreview {
  idleSeconds: number
  statuses: string[]
  candidates: SessionRow[]
  count: number
  /** Single-use confirmation token that `archive_apply` requires. */
  token: string
}

/** Parsed `archive_apply` response. */
export interface ArchiveApplyResult {
  archived: ArchiveEntry[]
  count: number
  requested: number
}

/** Parsed `unarchive` response. */
export interface UnarchiveResult {
  released: Array<{ agent: string; session_id: string }>
  count: number
}

/** Parsed `archive_list` response. */
export interface ArchiveListResult {
  entries: ArchiveEntry[]
  sessions: ArchivedSessionRow[]
  count: number
}

/** Parsed `status` response. */
export interface StatusSnapshot {
  sessions: SessionRow[]
  /** Sessions hidden by the archive registry; empty on pre-archive daemons. */
  archived: ArchivedSessionRow[]
  /** Null when the daemon predates the archive policy field. */
  archivePolicy: ArchivePolicy | null
  scanErrors: Array<Record<string, unknown>>
  tailErrors: Array<Record<string, unknown>>
  diagnostics: Array<Record<string, unknown>>
}

/** One parsed page of the `replay` op (daemon `_replay_response`). */
export interface ReplayPage {
  sessionId: string
  agent: string
  /** The request cursor this page starts after. */
  afterSeq: number
  events: SidecarEvent[]
  /** Daemon-reported event count of this page. */
  count: number
  /** Highest raw-record seq seen by the daemon; the next-page cursor. */
  lastSeq: number | null
  /** True when the daemon hit the page limit (more records may exist). */
  truncated: boolean
}

/**
 * Coded failure of a request/response op. `code` carries the daemon error
 * vocabulary verbatim (`invalid_request`, `unknown_session`,
 * `replay_unsupported`, `replay_failed`, ...) or one of the client-side
 * transport codes: `timeout`, `connection_failed`, `connection_closed`,
 * `invalid_response`.
 */
export class SidecarDaemonError extends Error {
  readonly code: string

  constructor(code: string, detail: string) {
    super(`${code}: ${detail}`)
    this.name = 'SidecarDaemonError'
    this.code = code
  }
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

/** Per-stream options of {@link SidecarSocketClient.subscribe}. */
export interface SubscribeOptions {
  /**
   * Optional agent allowlist forwarded on the wire
   * (`{"op":"subscribe","agents":[...]}`); the daemon then streams only
   * events from those agents. Omitting it keeps the full stream. An empty
   * list or empty names throw a RangeError (mirrors sidecar/client.py).
   */
  agents?: readonly string[]
}

export interface SidecarSocketClientOptions {
  /** Absolute path to `daemon.sock` (default runtime dir is `~/.agent_sidecar`, redirectable via AGENT_SIDECAR_RUNTIME_DIR — resolved by the caller). */
  socketPath: string
  /** Connect/handshake/response bound; the stream idles unbounded after the subscribe ack, matching sidecar/client.py. */
  timeoutMs?: number
  /** Response bound for the `replay` op (bounded transcript decode is slower than ping/status). */
  replayTimeoutMs?: number
  /** Bound for a single JSONL line (sidecar caps responses at 32 MiB). */
  maxLineBytes?: number
}

/** Matches `DEFAULT_TIMEOUT = 1.0` in sidecar/client.py. */
export const DEFAULT_TIMEOUT_MS = 1000
/** Matches `DEFAULT_REPLAY_TIMEOUT = 15.0` in sidecar/client.py. */
export const DEFAULT_REPLAY_TIMEOUT_MS = 15_000
/** Matches `MAX_RESPONSE_BYTES` in sidecar/client.py. */
export const DEFAULT_MAX_LINE_BYTES = 32 * 1024 * 1024

// ---------------------------------------------------------------------------
// Parsing helpers.
// ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  try {
    const prototype = Object.getPrototypeOf(value)
    return prototype === Object.prototype || prototype === null
  } catch {
    return false
  }
}

const SESSION_STATUSES: ReadonlySet<string> = new Set(['working', 'waiting', 'idle', 'dead'])
const INVALID_STATUS = '<invalid>'

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
  const rawSource = value['source_version']
  if (rawSource !== null && rawSource !== undefined && typeof rawSource !== 'string') return null
  const sourceVersion = typeof rawSource === 'string' && rawSource !== '' ? rawSource : undefined
  return {
    pid,
    version,
    ...sourceVersion === undefined ? {} : { sourceVersion },
    ...value['source_changed'] === true ? { sourceChanged: true } : {},
    http,
  }
}

/**
 * Normalize one raw status row. Rows without a usable `session_id` are
 * skipped by the caller (the daemon model guarantees the field, so this
 * only defends against wire corruption).
 */
function parseSessionRow(value: unknown): SessionRow | null {
  if (!isRecord(value)) return null
  const agent = value['agent']
  const sessionId = value['session_id']
  const project = value['project']
  const transcript = value['transcript']
  const updatedAt = value['updated_at']
  const title = value['title']
  if (
    typeof agent !== 'string' ||
    agent === '' ||
    typeof sessionId !== 'string' ||
    sessionId === '' ||
    typeof project !== 'string' ||
    typeof transcript !== 'string' ||
    typeof updatedAt !== 'number' ||
    !Number.isFinite(updatedAt) ||
    typeof title !== 'string'
  ) {
    return null
  }

  let invalid = false
  const rawStatus = value['status']
  const status = typeof rawStatus === 'string' ? rawStatus : INVALID_STATUS
  if (!SESSION_STATUSES.has(status)) invalid = true

  let extra = sanitizeSessionExtra(value['extra'])
  if (extra === null) {
    invalid = true
    extra = {}
  }

  const rawParentId = value['parent_id']
  let parentId: string | null
  if (rawParentId === null || typeof rawParentId === 'string') {
    parentId = rawParentId
  } else {
    invalid = true
    parentId = null
  }

  const row: SessionRow = {
    agent,
    session_id: sessionId,
    project,
    transcript,
    updated_at: updatedAt,
    title,
    status,
    extra,
    parent_id: parentId,
  }

  const copyMarker = (
    key: 'host' | 'source' | 'remote_alias' | 'remote_host',
    nonempty: boolean,
  ): void => {
    if (!Object.prototype.hasOwnProperty.call(value, key)) return
    const marker = value[key]
    if (typeof marker !== 'string' || (nonempty && marker === '')) {
      invalid = true
      return
    }
    row[key] = marker
  }
  copyMarker('host', true)
  copyMarker('source', false)
  copyMarker('remote_alias', true)
  copyMarker('remote_host', true)

  if (Object.prototype.hasOwnProperty.call(value, 'remote')) {
    if (typeof value['remote'] === 'boolean') row.remote = value['remote']
    else invalid = true
  }
  if (invalid) row.invalid_session = true
  return row
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

function parseSessionRows(value: unknown): SessionRow[] | null {
  if (!Array.isArray(value)) return null
  const rows: SessionRow[] = []
  for (const raw of value) {
    // Mirror sidecar/client.py: a non-object row invalidates the response.
    if (!isRecord(raw)) return null
    const row = parseSessionRow(raw)
    if (row !== null) rows.push(row)
  }
  return rows
}

/**
 * Archived rows are ordinary session rows carrying the registry decision.
 * A row without a usable decision is dropped rather than shown as active:
 * the daemon already excluded it from `sessions`.
 */
function parseArchivedRows(value: unknown): ArchivedSessionRow[] | null {
  if (value === undefined) return []
  if (!Array.isArray(value)) return null
  const rows: ArchivedSessionRow[] = []
  for (const raw of value) {
    if (!isRecord(raw)) return null
    const row = parseSessionRow(raw)
    if (row === null) continue
    const archivedAt = raw['archived_at']
    const reason = raw['archive_reason']
    if (typeof archivedAt !== 'number' || !Number.isFinite(archivedAt)) continue
    if (typeof reason !== 'string' || reason === '') continue
    rows.push({ ...row, archived_at: archivedAt, archive_reason: reason })
  }
  return rows
}

function parseArchivePolicy(value: unknown): ArchivePolicy | null {
  if (!isRecord(value)) return null
  const auto = value['auto']
  const autoAfter = value['auto_after_seconds']
  const defaultIdle = value['default_idle_seconds']
  if (typeof auto !== 'boolean') return null
  if (typeof autoAfter !== 'number' || !Number.isFinite(autoAfter)) return null
  if (typeof defaultIdle !== 'number' || !Number.isFinite(defaultIdle)) return null
  return { auto, autoAfterSeconds: autoAfter, defaultIdleSeconds: defaultIdle }
}

function parseArchiveEntries(value: unknown): ArchiveEntry[] | null {
  if (!Array.isArray(value)) return null
  const entries: ArchiveEntry[] = []
  for (const raw of value) {
    if (!isRecord(raw)) return null
    const agent = raw['agent']
    const sessionId = raw['session_id']
    const archivedAt = raw['archived_at']
    const reason = raw['reason']
    if (typeof agent !== 'string' || agent === '') return null
    if (typeof sessionId !== 'string' || sessionId === '') return null
    if (typeof archivedAt !== 'number' || !Number.isFinite(archivedAt)) return null
    if (typeof reason !== 'string' || reason === '') return null
    entries.push({ agent, session_id: sessionId, archived_at: archivedAt, reason })
  }
  return entries
}

function parseStatusSnapshot(value: unknown): StatusSnapshot | null {
  if (!isRecord(value) || value['ok'] !== true) return null
  const sessions = parseSessionRows(value['sessions'])
  if (sessions === null) return null
  const archived = parseArchivedRows(value['archived'])
  if (archived === null) return null
  const scanErrors = parseRecordList(value['scan_errors'])
  const tailErrors = parseRecordList(value['tail_errors'])
  if (scanErrors === null || tailErrors === null) return null
  const diagnostics = parseRecordList(value['diagnostics'])
  return {
    sessions,
    archived,
    archivePolicy: parseArchivePolicy(value['archive_policy']),
    scanErrors,
    tailErrors,
    diagnostics: diagnostics ?? [],
  }
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

function daemonError(value: Record<string, unknown>): SidecarDaemonError {
  const error = value['error']
  if (isRecord(error)) {
    const code = String(error['code'] ?? 'daemon_error')
    const message = String(error['message'] ?? code)
    return new SidecarDaemonError(code, message)
  }
  return new SidecarDaemonError('daemon_error', String(error ?? 'daemon_error'))
}

/**
 * Parse one `replay` response page. Mirrors sidecar/client.py strictness:
 * a non-object entry in `events` invalidates the whole response, while an
 * object entry missing normalized fields is skipped defensively (the
 * daemon model guarantees them).
 */
function parseReplayPage(value: unknown): ReplayPage | null {
  if (!isRecord(value) || value['ok'] !== true || value['op'] !== 'replay') return null
  const sessionId = value['session_id']
  const agent = value['agent']
  const rawEvents = value['events']
  if (typeof sessionId !== 'string' || typeof agent !== 'string') return null
  if (!Array.isArray(rawEvents)) return null
  const events: SidecarEvent[] = []
  for (const raw of rawEvents) {
    if (!isRecord(raw)) return null
    const event = parseEvent(raw)
    if (event !== null) events.push(event)
  }
  const afterSeq = value['after_seq']
  const count = value['count']
  const lastSeq = value['last_seq']
  return {
    sessionId,
    agent,
    afterSeq:
      typeof afterSeq === 'number' && Number.isInteger(afterSeq) && afterSeq >= 0 ? afterSeq : 0,
    events,
    count: typeof count === 'number' && Number.isInteger(count) ? count : events.length,
    lastSeq: typeof lastSeq === 'number' && Number.isInteger(lastSeq) ? lastSeq : null,
    truncated: value['truncated'] === true,
  }
}

/**
 * Build the subscribe request line, validating an optional agents filter
 * up front (before any socket exists) so misuse throws synchronously.
 */
function buildSubscribeRequest(agents: readonly string[] | undefined): string {
  if (agents === undefined) return '{"op":"subscribe"}\n'
  if (agents.length === 0 || agents.some((name) => typeof name !== 'string' || name === '')) {
    throw new RangeError('agents must be a nonempty list of nonempty agent names')
  }
  return `${JSON.stringify({ op: 'subscribe', agents })}\n`
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
  readonly replayTimeoutMs: number
  readonly maxLineBytes: number

  constructor(opts: SidecarSocketClientOptions) {
    this.socketPath = opts.socketPath
    this.timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS
    this.replayTimeoutMs = opts.replayTimeoutMs ?? DEFAULT_REPLAY_TIMEOUT_MS
    this.maxLineBytes = opts.maxLineBytes ?? DEFAULT_MAX_LINE_BYTES
    if (this.timeoutMs <= 0 || this.replayTimeoutMs <= 0 || this.maxLineBytes <= 0) {
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
   * `replay` op (T5.2): one bounded page of normalized historical events
   * after `afterSeq`. Unlike ping/status this REJECTS with a coded
   * {@link SidecarDaemonError} — daemon codes pass through verbatim and
   * transport failures get client codes — because the caller (FusionQuery
   * seam) distinguishes degradation reasons instead of polling health.
   * Local misuse throws a RangeError (mirrors sidecar/client.py's
   * ValueError). `limit` is forwarded as-is; the daemon enforces its own
   * 1..1024 bound and answers `invalid_request` beyond it.
   */
  async replay(sessionId: string, afterSeq = 0, limit?: number): Promise<ReplayPage> {
    if (typeof sessionId !== 'string' || sessionId === '') {
      throw new RangeError('sessionId must be a nonempty string')
    }
    if (!Number.isInteger(afterSeq) || afterSeq < 0) {
      throw new RangeError('afterSeq must be a nonnegative integer')
    }
    if (limit !== undefined && (!Number.isInteger(limit) || limit <= 0)) {
      throw new RangeError('limit must be a positive integer')
    }
    const payload: Record<string, unknown> = {
      op: 'replay',
      session_id: sessionId,
      after_seq: afterSeq,
    }
    if (limit !== undefined) payload['limit'] = limit
    const value = await this.requestObject(payload, this.replayTimeoutMs)
    if (isRecord(value) && value['ok'] === false) throw daemonError(value)
    const page = parseReplayPage(value)
    if (page === null) {
      throw new SidecarDaemonError(
        'invalid_response',
        'daemon replay response has no valid events list',
      )
    }
    return page
  }

  /**
   * `archive_preview` op: the sessions a batch archive would hide, plus the
   * single-use token {@link archiveApply} requires. Like {@link replay} it
   * REJECTS with a coded {@link SidecarDaemonError} so the caller can tell
   * "daemon absent" from "threshold rejected".
   */
  async archivePreview(idleSeconds: number, statuses?: readonly string[]): Promise<ArchivePreview> {
    if (!Number.isFinite(idleSeconds) || idleSeconds <= 0) {
      throw new RangeError('idleSeconds must be a positive number')
    }
    const payload: Record<string, unknown> = {
      op: 'archive_preview',
      idle_seconds: idleSeconds,
    }
    if (statuses !== undefined) payload['statuses'] = [...statuses]
    const value = await this.requestObject(payload, this.timeoutMs)
    if (isRecord(value) && value['ok'] === false) throw daemonError(value)
    if (!isRecord(value) || value['op'] !== 'archive_preview') {
      throw new SidecarDaemonError('invalid_response', 'daemon returned an invalid archive preview')
    }
    const candidates = parseSessionRows(value['candidates'])
    const token = value['token']
    const rawStatuses = value['statuses']
    if (candidates === null || typeof token !== 'string' || token === '') {
      throw new SidecarDaemonError('invalid_response', 'daemon returned an invalid archive preview')
    }
    return {
      idleSeconds:
        typeof value['idle_seconds'] === 'number' ? value['idle_seconds'] : idleSeconds,
      statuses: Array.isArray(rawStatuses)
        ? rawStatuses.filter((name): name is string => typeof name === 'string')
        : [],
      candidates,
      count: candidates.length,
      token,
    }
  }

  /** `archive_apply` op: archive a subset of one preview's candidates. */
  async archiveApply(
    targets: ReadonlyArray<{ agent: string; sessionId: string }>,
    token: string,
  ): Promise<ArchiveApplyResult> {
    if (targets.length === 0) throw new RangeError('targets must be nonempty')
    if (typeof token !== 'string' || token === '') {
      throw new RangeError('token must be a nonempty string')
    }
    const value = await this.requestObject(
      {
        op: 'archive_apply',
        token,
        targets: targets.map((target) => ({
          agent: target.agent,
          session_id: target.sessionId,
        })),
      },
      this.timeoutMs,
    )
    if (isRecord(value) && value['ok'] === false) throw daemonError(value)
    const entries = isRecord(value) ? parseArchiveEntries(value['archived']) : null
    if (entries === null) {
      throw new SidecarDaemonError('invalid_response', 'daemon returned an invalid archive result')
    }
    const requested = isRecord(value) && typeof value['requested'] === 'number'
      ? value['requested']
      : targets.length
    return { archived: entries, count: entries.length, requested }
  }

  /** `unarchive` op: release specific targets, or every archived session. */
  async unarchive(
    targets: ReadonlyArray<{ agent: string; sessionId: string }> | 'all',
  ): Promise<UnarchiveResult> {
    const payload: Record<string, unknown> =
      targets === 'all'
        ? { op: 'unarchive', all: true }
        : {
            op: 'unarchive',
            targets: targets.map((target) => ({
              agent: target.agent,
              session_id: target.sessionId,
            })),
          }
    if (targets !== 'all' && targets.length === 0) {
      throw new RangeError('targets must be nonempty')
    }
    const value = await this.requestObject(payload, this.timeoutMs)
    if (isRecord(value) && value['ok'] === false) throw daemonError(value)
    const rawReleased = isRecord(value) ? value['released'] : null
    if (!Array.isArray(rawReleased)) {
      throw new SidecarDaemonError('invalid_response', 'daemon returned an invalid unarchive result')
    }
    const released: Array<{ agent: string; session_id: string }> = []
    for (const raw of rawReleased) {
      if (!isRecord(raw)) continue
      const agent = raw['agent']
      const sessionId = raw['session_id']
      if (typeof agent !== 'string' || typeof sessionId !== 'string') continue
      released.push({ agent, session_id: sessionId })
    }
    return { released, count: released.length }
  }

  /** `archive_list` op: the current registry plus the rows it hides. */
  async archiveList(): Promise<ArchiveListResult> {
    const value = await this.requestObject({ op: 'archive_list' }, this.timeoutMs)
    if (isRecord(value) && value['ok'] === false) throw daemonError(value)
    const entries = isRecord(value) ? parseArchiveEntries(value['entries']) : null
    const sessions = isRecord(value) ? parseArchivedRows(value['sessions']) : null
    if (entries === null || sessions === null) {
      throw new SidecarDaemonError('invalid_response', 'daemon returned an invalid archive list')
    }
    return { entries, sessions, count: entries.length }
  }

  /**
   * Open a subscribe stream: write the op, validate the ack, then deliver
   * each JSONL event through `handlers.onEvent`. After the ack the
   * connection may idle indefinitely (no timeout), matching
   * sidecar/client.py which disables its socket timeout post-handshake.
   * An `opts.agents` allowlist becomes the daemon-side stream filter.
   */
  subscribe(handlers: SubscribeHandlers, opts: SubscribeOptions = {}): Subscription {
    // Validate (and possibly throw) before any socket exists.
    const request = buildSubscribeRequest(opts.agents)
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
      socket.write(request)
    })
    socket.on('data', (chunk: Buffer) => lines.push(chunk))

    return { close: () => finish() }
  }

  /**
   * Send one single-line JSON request and read one JSONL response line,
   * REJECTING with a coded {@link SidecarDaemonError} on every transport
   * failure (the replay path needs error provenance, not just null).
   */
  private requestObject(
    payload: Record<string, unknown>,
    timeoutMs: number,
  ): Promise<unknown> {
    return new Promise<unknown>((resolve, reject) => {
      let settled = false
      const socket: Socket = createConnection({ path: this.socketPath })
      const finish = (settle: () => void): void => {
        if (settled) return
        settled = true
        socket.destroy()
        settle()
      }
      const fail = (code: string, detail: string): void => {
        finish(() => reject(new SidecarDaemonError(code, detail)))
      }
      const lines = new LineBuffer(
        this.maxLineBytes,
        (line) => {
          let value: unknown
          try {
            value = JSON.parse(line.toString('utf8'))
          } catch {
            fail('invalid_response', 'daemon returned an unparsable response line')
            return
          }
          finish(() => resolve(value))
        },
        () => fail('invalid_response', 'daemon response line exceeded the size bound'),
      )
      socket.setTimeout(timeoutMs)
      socket.once('timeout', () => fail('timeout', 'daemon did not answer within the bound'))
      socket.once('error', (err: Error) => fail('connection_failed', err.message))
      socket.once('close', () =>
        fail('connection_closed', 'connection closed before a response line'),
      )
      socket.once('connect', () => {
        socket.write(`${JSON.stringify(payload)}\n`)
      })
      socket.on('data', (chunk: Buffer) => lines.push(chunk))
    })
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
  /** Optional so pre-archive store fakes stay assignable. */
  setArchived?(rows: ArchivedSessionRow[], policy: ArchivePolicy | null): void
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
  /** First retry delay after a failed reconcile (doubles per consecutive failure, capped at the cadence). */
  failureBackoffMs?: number
}

export const DEFAULT_ACTIVE_MS = 2000
export const DEFAULT_IDLE_MS = 10000
export const DEFAULT_DEBOUNCE_MS = 200
export const DEFAULT_RECONNECT_MIN_MS = 1000
export const DEFAULT_RECONNECT_MAX_MS = 30000
export const DEFAULT_FAILURE_BACKOFF_MS = 250

/**
 * Dual-cadence status reconciliation plus subscribe-stream supervision:
 * - `status` snapshots run on an active (any working session) or idle
 *   cadence and are applied as the authoritative full state.
 * - each subscribe event is folded into the store as a hint and schedules
 *   one debounced early reconcile.
 * - a FAILED snapshot (daemon absent or not yet ready) retries on a short
 *   backoff (250ms doubling, capped at the current cadence) instead of
 *   sleeping a whole cadence period — a cold start where the very first
 *   `status` races the daemon socket must not cost a full `idleMs`
 *   (M1 acceptance ②). A success resets the streak to the steady cadence.
 * - `reconcileNow()` is public so the supervisor can hand off "daemon just
 *   became reachable" (ADOPTED/HOSTED are ping-gated) as one immediate
 *   reconcile.
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
  private readonly failureBackoffMs: number

  private running = false
  private backoffMs: number
  /** Consecutive failed reconciles; drives the short retry backoff. */
  private failStreak = 0
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
    this.failureBackoffMs = opts.failureBackoffMs ?? DEFAULT_FAILURE_BACKOFF_MS
    this.backoffMs = this.reconnectMinMs
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.backoffMs = this.reconnectMinMs
    this.failStreak = 0
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

  /**
   * Run one immediate `status` reconcile and reschedule the next poll from
   * its outcome. Public as the supervisor hand-off seam: the plugin entry
   * calls this on the ADOPTED/HOSTED transition (both are gated on a
   * successful ping, so the socket is known-reachable at that moment).
   * Coalesces with an in-flight reconcile; a no-op when stopped.
   */
  async reconcileNow(): Promise<void> {
    if (!this.running) return
    if (this.reconcileInFlight) {
      this.reconcileQueued = true
      return
    }
    this.reconcileInFlight = true
    try {
      const snapshot = await this.client.status()
      if (snapshot === null) {
        this.failStreak += 1
      } else {
        this.failStreak = 0
        if (this.running) {
          this.store.applySnapshot(snapshot.sessions)
          // A daemon predating the archive ops answers status without the
          // archive fields; treat that as "nothing archived, no policy".
          this.store.setArchived?.(snapshot.archived ?? [], snapshot.archivePolicy ?? null)
        }
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
    const cadence = this.store.hasWorkingSessions() ? this.activeMs : this.idleMs
    // After a failure, retry on the short doubling backoff; the cadence cap
    // means a persistently absent daemon converges to the steady-state poll
    // rate instead of adding load.
    const delay =
      this.failStreak > 0
        ? Math.min(this.failureBackoffMs * 2 ** (this.failStreak - 1), cadence)
        : cadence
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
