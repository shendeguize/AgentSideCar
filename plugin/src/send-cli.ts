/**
 * Sidecar `send` CLI executor — injection path two of design §4.d
 * (.local/tasks/make_dsh_mode/design/dsh_plugin_design.md): external agents
 * (claude / codex / cursor-cli) are reached by spawning
 * `agent-sidecar send <session_prefix> --message-stdin --allow-write --json`.
 *
 * Contract facts verified against sidecar/cli.py and sidecar/inject.py:
 *
 * - The send subcommand takes the target as the `session_prefix` positional;
 *   there is no `--agent`, `--session`, or `--mode` flag. The queue/steer
 *   distinction only exists on the dsh in-process path — a sidecar send is
 *   always a one-shot headless resume, so `mode` is logged but not forwarded.
 * - The message NEVER enters argv: it is written to the child's stdin and the
 *   write end is closed (`--message-stdin`, S7).
 * - `--json` prints one `SendResult.to_dict()` object on stdout:
 *   `{agent, session_id, outcome, delivery, returncode, response, stderr,
 *   error_code, request_id, replayed}` with `delivery ∈ {delivered, unknown}`.
 *   `delivery` is the authoritative signal: `delivered` → outcome 'delivered',
 *   `unknown` → outcome 'unknown' (terminal; S6 — never retried).
 * - Exit codes: 0 completed / 1 non-completed result / 2 usage (SendError) /
 *   130 interrupted. Used only as a fallback when stdout is not a receipt.
 * - `--timeout` accepts 1..900 seconds (MAX_SEND_TIMEOUT_SECONDS).
 *
 * Pure DI: no cordis/dsh imports. index.ts adapts `ctx.subprocess.spawn`
 * into {@link SpawnLike}; tests adapt `node:child_process`. The log callback
 * never receives the message body (S8).
 *
 * @module
 */

import type { InjectExecutionRequest, InjectExecutor, InjectResult } from './inject-gateway.ts'

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
 * - `stdin` write/end must not throw asynchronously for a dead pipe
 *   (swallow EPIPE-style errors; the failure surfaces via `exited`).
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
  errorCode?: string
  replayed: boolean
}

/**
 * Parse stdout as one `send --json` receipt. Anything that is not a JSON
 * object carrying a valid `delivery` field yields null (exit-code fallback).
 */
function parseReceipt(stdoutText: string): ParsedReceipt | null {
  const trimmed = stdoutText.trim()
  if (!trimmed) return null
  let value: unknown
  try {
    value = JSON.parse(trimmed)
  } catch {
    return null
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null
  const record = value as Record<string, unknown>
  const delivery = record['delivery']
  if (delivery !== 'delivered' && delivery !== 'unknown') return null
  const rawErrorCode = record['error_code']
  const errorCode =
    typeof rawErrorCode === 'string' && rawErrorCode !== '' ? rawErrorCode : undefined
  return {
    delivery,
    ...(errorCode !== undefined ? { errorCode } : {}),
    replayed: record['replayed'] === true,
  }
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
      // Target = session_prefix positional; message deliberately absent.
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
      proc.onStdout((chunk) => stdout.append(chunk))
      proc.onStderr((chunk) => stderr.append(chunk))

      try {
        proc.stdin.write(Buffer.from(req.message, 'utf8'))
        proc.stdin.end()
      } catch {
        // A dead stdin pipe means the process is already gone; the exit /
        // spawn-error path below reports the authoritative failure.
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
        return {
          outcome: 'failed',
          errorCode: 'cli_not_found',
          detail: describeError(settled.error),
        }
      }

      const receipt = parseReceipt(stdout.text())
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
        return {
          outcome: receipt.delivery === 'delivered' ? 'delivered' : 'unknown',
          ...(receipt.errorCode !== undefined ? { errorCode: receipt.errorCode } : {}),
          ...(receipt.replayed ? { replayed: true } : {}),
          ...(stderrText ? { detail: stderrText } : {}),
        }
      }

      // stdout was empty or not a receipt: fall back to exit-code vocabulary.
      const code = settled.code
      if (code === 0) {
        return {
          outcome: 'delivered',
          detail: stderrText
            ? `parse_warning: exit 0 but stdout was not a send --json receipt; stderr: ${stderrText}`
            : 'parse_warning: exit 0 but stdout was not a send --json receipt',
        }
      }
      const base: InjectResult = { outcome: 'failed' }
      if (code === 2) base.errorCode = 'usage_error'
      else if (code === 130) base.errorCode = 'interrupted'
      else if (code !== 1) base.errorCode = `exit_${code ?? 'signal'}`
      if (stderrText) base.detail = stderrText
      return base
    },
  }
}
