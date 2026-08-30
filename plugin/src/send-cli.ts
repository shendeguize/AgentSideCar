/**
 * Sidecar `send` CLI executor — injection path two of design §4.d
 * (.local/tasks/make_dsh_mode/design/dsh_plugin_design.md): external agents
 * (claude / codex / cursor-cli / copilot) are reached by spawning
 * `agent-sidecar send <session_id> --agent <agent> --exact-session
 * --message-stdin --allow-write --json`.
 *
 * Contract facts verified against sidecar/cli.py and sidecar/inject.py:
 *
 * - The send subcommand takes the exact session ID as its positional and the
 *   token-bound agent through `--agent`. The queue/steer distinction only
 *   exists on the dsh in-process path — a sidecar send is always a one-shot
 *   headless resume, so `mode` is logged but not forwarded.
 * - The message NEVER enters argv: it is written to the child's stdin and the
 *   write end is closed (`--message-stdin`, S7).
 * - `--json` prints one `SendResult.to_dict()` object on stdout:
 *   `{agent, session_id, outcome, delivery, returncode, response, stderr,
 *   error_code, request_id, replayed}` with `delivery ∈ {delivered, unknown}`.
 *   `delivery` is the authoritative signal: `delivered` → outcome 'delivered',
 *   `unknown` → outcome 'unknown' (terminal; S6 — never retried).
 * - Once stdin submission starts, exit codes and process errors are never
 *   delivery proof. Without a bound receipt or structured pre-delivery error,
 *   every such result is terminal unknown.
 * - `--timeout` accepts 1..900 seconds (MAX_SEND_TIMEOUT_SECONDS).
 *
 * Pure DI: no cordis/dsh imports. index.ts adapts `ctx.subprocess.spawn`
 * into {@link SpawnLike}; tests adapt `node:child_process`. The log callback
 * never receives the message body (S8).
 *
 * @module
 */

import type {
  InjectErrorCode,
  InjectExecutionRequest,
  InjectExecutor,
  InjectResult,
} from './inject-gateway.ts'

// ---------------------------------------------------------------------------
// Spawn seam.
// ---------------------------------------------------------------------------

/**
 * Minimal handle over one spawned CLI process. Both `ctx.subprocess.spawn`
 * and `node:child_process.spawn` adapt onto this surface.
 *
 * Adapter obligations:
 * - `exited` settles with the exit code (null when signal-killed) and
 *   rejects when the process could not be started at all (ENOENT et al.).
 * - `stdin` write/end may throw synchronously. The executor records submission
 *   as started before the first write, so partial/dead-pipe writes fail closed
 *   as terminal unknown unless stdout contains a validated JSON envelope.
 * - asynchronous EPIPE-style errors must be swallowed by the adapter; the
 *   process failure surfaces via `exited`.
 */
export interface SpawnedProcess {
  stdin: {
    write(chunk: Uint8Array | string): void
    end(): void
  }
  onStdout(listener: (chunk: Uint8Array | string) => void): void
  onStderr(listener: (chunk: Uint8Array | string) => void): void
  /** Exit code (null for signal death); rejects on spawn failure. */
  readonly exited: Promise<number | null>
  /** Best-effort hard kill, used by the process-level timeout. */
  kill(): void
}

export type SpawnLike = (argv: readonly string[]) => SpawnedProcess

export type SendCliLogLevel = 'debug' | 'info' | 'warn' | 'error'

export type SendCliLog = (
  level: SendCliLogLevel,
  msg: string,
  meta?: Record<string, unknown>,
) => void

export interface SendCliOptions {
  /** Sidecar CLI invocation prefix; argv, never a shell string. */
  command?: readonly string[]
  /**
   * Budget forwarded to `send --timeout` as floor(timeoutMs / 1000),
   * clamped into the CLI's accepted 1..900s range. Default 30000.
   */
  timeoutMs?: number
  /**
   * Extra slack on top of {@link timeoutMs} before the process-level hard
   * kill fires. Spec default 5000; injectable so tests stay fast.
   */
  hardTimeoutBufferMs?: number
}

// ---------------------------------------------------------------------------
// Tunables.
// ---------------------------------------------------------------------------

export const DEFAULT_SEND_CLI_COMMAND: readonly string[] = Object.freeze(['agent-sidecar'])
export const DEFAULT_SEND_TIMEOUT_MS = 30_000
export const HARD_TIMEOUT_BUFFER_MS = 5_000
/** Detail cap for collected stderr (2 KiB). */
export const STDERR_DETAIL_BYTES = 2 * 1024

/** send --json responses are ≤4 MiB; anything past this is garbage. */
const MAX_STDOUT_BYTES = 8 * 1024 * 1024
/** sidecar/inject.py MAX_SEND_TIMEOUT_SECONDS. */
const MAX_CLI_TIMEOUT_SECONDS = 900

// ---------------------------------------------------------------------------
// Internals.
// ---------------------------------------------------------------------------

/** Byte-bounded chunk accumulator; excess input is dropped, not buffered. */
class BoundedCollector {
  private readonly chunks: Buffer[] = []
  private size = 0

  constructor(private readonly limit: number) {}

  append(chunk: Uint8Array | string): void {
    if (this.size >= this.limit) return
    const buf = typeof chunk === 'string' ? Buffer.from(chunk, 'utf8') : Buffer.from(chunk)
    const room = this.limit - this.size
    const kept = buf.byteLength > room ? buf.subarray(0, room) : buf
    this.chunks.push(Buffer.from(kept))
    this.size += kept.byteLength
  }

  get bytes(): number {
    return this.size
  }

  text(): string {
    return Buffer.concat(this.chunks).toString('utf8')
  }
}

interface ParsedReceipt {
  delivery: 'delivered' | 'unknown'
  agent?: string
  sessionId?: string
  requestId?: string
  errorCode?: string
  replayed: boolean
}

function parseJsonObject(stdoutText: string): Record<string, unknown> | null {
  const trimmed = stdoutText.trim()
  if (!trimmed) return null
  let value: unknown
  try {
    value = JSON.parse(trimmed)
  } catch {
    return null
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

/**
 * Parse one JSON object as a `send --json` receipt. Identity fields remain
 * optional here so the caller can classify omissions as a binding mismatch.
 */
function parseReceipt(record: Record<string, unknown>): ParsedReceipt | null {
  const delivery = record['delivery']
  if (delivery !== 'delivered' && delivery !== 'unknown') return null
  const rawErrorCode = record['error_code']
  const errorCode =
    typeof rawErrorCode === 'string' && rawErrorCode !== '' ? rawErrorCode : undefined
  return {
    delivery,
    ...(typeof record['agent'] === 'string' ? { agent: record['agent'] } : {}),
    ...(typeof record['session_id'] === 'string' ? { sessionId: record['session_id'] } : {}),
    ...(typeof record['request_id'] === 'string' ? { requestId: record['request_id'] } : {}),
    ...(errorCode !== undefined ? { errorCode } : {}),
    replayed: record['replayed'] === true,
  }
}

function parseCliError(record: Record<string, unknown>): string | null {
  const keys = Object.keys(record)
  const code = record['code']
  return keys.length === 1 && keys[0] === 'code' && typeof code === 'string' && code !== ''
    ? code
    : null
}

/** Map Python preflight vocabulary onto the gateway's stable error surface. */
function mapCliError(code: string): InjectErrorCode {
  if (
    code === 'working_session' ||
    code === 'dead_session' ||
    code === 'child_session' ||
    code === 'remote_session' ||
    code === 'invalid_session' ||
    code === 'target_not_found' ||
    code === 'unsupported_agent'
  ) {
    return code
  }
  if (code === 'session_busy') return 'working_session'
  if (code === 'session_unavailable') return 'target_not_found'
  if (code === 'session_changed') return 'target_changed'
  if (
    code === 'ambiguous_session' ||
    code === 'invalid_session_id' ||
    code === 'invalid_project' ||
    code === 'invalid_plan' ||
    code === 'request_conflict'
  ) {
    return 'invalid_session'
  }
  if (
    code === 'unsupported_cursor_ide' ||
    code === 'unsupported_copilot' ||
    code === 'unsupported_kimi' ||
    code === 'unsupported_dsh'
  ) {
    return 'unsupported_agent'
  }
  if (
    code === 'invalid_message_type' ||
    code === 'invalid_message_utf8' ||
    code === 'blank_message' ||
    code === 'message_nul' ||
    code === 'message_too_large'
  ) {
    return 'invalid_message'
  }
  return 'executor_error'
}

/**
 * A bound receipt with unknown delivery remains terminal/HTTP 200, but its
 * diagnostic code still uses the stable gateway vocabulary. Timeout is kept
 * distinct for the existing unknown-delivery UX; ACP/process/snapshot codes
 * intentionally collapse to executor_error.
 */
function mapReceiptError(code: string): string {
  return code === 'timeout' ? code : mapCliError(code)
}

function describeError(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

// ---------------------------------------------------------------------------
// Executor.
// ---------------------------------------------------------------------------

type Settled =
  | { kind: 'exit'; code: number | null }
  | { kind: 'spawn-error'; error: unknown }
  | { kind: 'timeout' }

type StdinSubmissionBoundary = 'not-started' | 'started' | 'completed'

export function createSendCliExecutor(deps: {
  spawn: SpawnLike
  log?: SendCliLog
  opts?: SendCliOptions
}): InjectExecutor {
  const command = deps.opts?.command ?? DEFAULT_SEND_CLI_COMMAND
  const timeoutMs = deps.opts?.timeoutMs ?? DEFAULT_SEND_TIMEOUT_MS
  const bufferMs = deps.opts?.hardTimeoutBufferMs ?? HARD_TIMEOUT_BUFFER_MS
  const log: SendCliLog = deps.log ?? (() => {})

  const cliTimeoutSecs = Math.min(
    MAX_CLI_TIMEOUT_SECONDS,
    Math.max(1, Math.floor(timeoutMs / 1000)),
  )
  const hardTimeoutMs = timeoutMs + bufferMs

  return {
    kind: 'send-cli',
    async execute(req: InjectExecutionRequest): Promise<InjectResult> {
      // Target = exact session ID + exact agent binding; message absent.
      // M2 review F-4 (recorded, unchanged): the positional is not fenced
      // behind `--`, so a hypothetical '-'-leading sessionId would be read
      // as a flag — every such misparse fail-closes as a usage error
      // (exit 2), and the id comes from local session-file scans inside the
      // machine trust boundary. Reordering to `send [flags] -- <prefix>`
      // is a CLI-contract change (argparse `--` handling must be verified
      // across the supported Python range first) deferred to its own PR.
      const argv: string[] = [
        ...command,
        'send',
        req.target.sessionId,
        '--agent',
        req.target.agent,
        '--exact-session',
        '--message-stdin',
        '--allow-write',
        '--json',
        '--request-id',
        req.requestId,
        '--timeout',
        String(cliTimeoutSecs),
      ]

      // `mode` has no CLI flag: a sidecar send is a one-shot headless resume.
      log('debug', 'spawning sidecar send CLI', {
        requestId: req.requestId,
        agent: req.target.agent,
        sessionId: req.target.sessionId,
        mode: req.mode,
        timeoutSecs: cliTimeoutSecs,
      })

      let proc: SpawnedProcess
      try {
        proc = deps.spawn(argv)
      } catch (error) {
        log('warn', 'send CLI spawn failed', {
          requestId: req.requestId,
          error: describeError(error),
        })
        return { outcome: 'failed', errorCode: 'cli_not_found', detail: describeError(error) }
      }

      const stdout = new BoundedCollector(MAX_STDOUT_BYTES)
      const stderr = new BoundedCollector(STDERR_DETAIL_BYTES)
      try {
        proc.onStdout((chunk) => stdout.append(chunk))
        proc.onStderr((chunk) => stderr.append(chunk))
      } catch {
        try {
          proc.kill()
        } catch {
          // Best effort: no message submission has started.
        }
        return { outcome: 'failed', errorCode: 'executor_error' }
      }

      let submissionBoundary: StdinSubmissionBoundary = 'not-started'
      try {
        // Set before invoking write: a synchronous throw may mean a partial
        // write, so it is already beyond the safe "definitely not submitted"
        // boundary.
        submissionBoundary = 'started'
        proc.stdin.write(Buffer.from(req.message, 'utf8'))
        proc.stdin.end()
        submissionBoundary = 'completed'
      } catch {
        // Preserve `started`; only a validated receipt/error below may sharpen
        // this conservative delivery-unknown state.
      }

      let timer: ReturnType<typeof setTimeout> | undefined
      const settled = await Promise.race<Settled>([
        proc.exited.then(
          (code): Settled => ({ kind: 'exit', code }),
          (error: unknown): Settled => ({ kind: 'spawn-error', error }),
        ),
        new Promise<Settled>((resolve) => {
          timer = setTimeout(() => resolve({ kind: 'timeout' }), hardTimeoutMs)
        }),
      ])
      if (timer !== undefined) clearTimeout(timer)

      const stderrText = stderr.text()

      if (settled.kind === 'timeout') {
        try {
          proc.kill()
        } catch {
          // Best effort; the child may have exited in the race window.
        }
        log('warn', 'send CLI hard timeout; process killed', {
          requestId: req.requestId,
          hardTimeoutMs,
        })
        // Not 'failed': the CLI may have delivered before we lost patience —
        // same semantics as a delivery:'unknown' receipt (S6, terminal).
        return {
          outcome: 'unknown',
          errorCode: 'timeout',
          detail: stderrText
            ? `no exit within ${hardTimeoutMs}ms; killed; stderr: ${stderrText}`
            : `no exit within ${hardTimeoutMs}ms; killed`,
        }
      }

      if (settled.kind === 'spawn-error') {
        log('warn', 'send CLI could not be started', {
          requestId: req.requestId,
          error: describeError(settled.error),
        })
        return submissionBoundary === 'not-started'
          ? {
              outcome: 'failed',
              errorCode: 'cli_not_found',
              detail: describeError(settled.error),
            }
          : {
              outcome: 'unknown',
              errorCode: 'executor_error',
              detail: 'process startup failed after stdin submission began',
            }
      }

      const parsedJson = parseJsonObject(stdout.text())
      const receipt = parsedJson === null ? null : parseReceipt(parsedJson)
      log('info', 'send CLI exited', {
        requestId: req.requestId,
        agent: req.target.agent,
        sessionId: req.target.sessionId,
        exitCode: settled.code,
        parsedReceipt: receipt !== null,
        ...(receipt !== null
          ? {
              delivery: receipt.delivery,
              ...(receipt.errorCode !== undefined ? { errorCode: receipt.errorCode } : {}),
              replayed: receipt.replayed,
            }
          : {}),
        stdoutBytes: stdout.bytes,
        stderrBytes: stderr.bytes,
      })

      if (receipt !== null) {
        if (
          receipt.agent !== req.target.agent ||
          receipt.sessionId !== req.target.sessionId ||
          receipt.requestId !== req.requestId
        ) {
          return {
            outcome: 'unknown',
            errorCode: 'executor_error',
            detail: stderrText
              ? `send CLI receipt identity mismatch; stderr: ${stderrText}`
              : 'send CLI receipt identity mismatch',
          }
        }
        return {
          outcome: receipt.delivery === 'delivered' ? 'delivered' : 'unknown',
          ...(receipt.errorCode !== undefined
            ? { errorCode: mapReceiptError(receipt.errorCode) }
            : {}),
          ...(receipt.replayed ? { replayed: true } : {}),
          ...(stderrText ? { detail: stderrText } : {}),
        }
      }

      const cliError = parsedJson === null ? null : parseCliError(parsedJson)
      if (settled.code !== 0 && cliError !== null) {
        return {
          outcome: 'failed',
          errorCode: mapCliError(cliError),
          ...(stderrText ? { detail: stderrText } : {}),
        }
      }

      // Submission started before write(), so an unstructured exit, signal,
      // parse failure, or missing stdout can never prove non-delivery.
      return {
        outcome: 'unknown',
        errorCode: settled.code === 130 ? 'interrupted' : 'executor_error',
        detail: stderrText
          ? `no valid bound send receipt or error; stderr: ${stderrText}`
          : 'no valid bound send receipt or error',
      }
    },
  }
}
