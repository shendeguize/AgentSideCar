/**
 * Unit tests for the sidecar `send` CLI executor (design §4.d path two).
 *
 * A temp-dir mock Node script simulates the `agent-sidecar send` contract
 * (reads stdin fully, records argv + a stdin sha256 observation to a file,
 * then emits a scenario-selected JSON receipt / exit code), and a real
 * `node:child_process.spawn` adapter drives it through {@link SpawnLike}.
 */

import { spawn as nodeSpawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'

import type { InjectExecutionRequest } from '../src/inject-gateway.ts'
import {
  createSendCliExecutor,
  DEFAULT_SEND_CLI_COMMAND,
  DEFAULT_SEND_TIMEOUT_MS,
  STDERR_DETAIL_BYTES,
  type SendCliLogLevel,
  type SpawnLike,
} from '../src/send-cli.ts'

// ---------------------------------------------------------------------------
// Mock `agent-sidecar` CLI: a Node script driven by MOCK_SCENARIO.
// ---------------------------------------------------------------------------

const MOCK_SCRIPT = `'use strict'
const { createHash } = require('node:crypto')
const { writeFileSync } = require('node:fs')

function readStdin() {
  return new Promise((resolve) => {
    const chunks = []
    process.stdin.on('data', (chunk) => chunks.push(chunk))
    process.stdin.on('end', () => resolve(Buffer.concat(chunks)))
  })
}

function exitWithStdout(code, data) {
  process.stdout.write(data, () => process.exit(code))
}

function exitWithStderr(code, data) {
  process.stderr.write(data, () => process.exit(code))
}

async function main() {
  const scenario = process.env.MOCK_SCENARIO || 'ok'
  const argv = process.argv.slice(2)
  const stdin = await readStdin()
  writeFileSync(
    process.env.MOCK_OBS_PATH,
    JSON.stringify({
      argv,
      stdinBytes: stdin.length,
      stdinSha256: createHash('sha256').update(stdin).digest('hex'),
    }),
  )
  const requestIdAt = argv.indexOf('--request-id')
  const receipt = (outcome, delivery, extra) =>
    JSON.stringify(
      Object.assign(
        {
          agent: 'claude',
          session_id: argv[1] || '',
          outcome,
          delivery,
          returncode: null,
          response: '',
          stderr: '',
          error_code: null,
          request_id: requestIdAt >= 0 ? argv[requestIdAt + 1] : '',
          replayed: false,
        },
        extra || {},
      ),
    ) + '\\n'

  switch (scenario) {
    case 'ok':
      return exitWithStdout(0, receipt('completed', 'delivered', { returncode: 0 }))
    case 'replayed':
      return exitWithStdout(
        0,
        receipt('completed', 'delivered', { returncode: 0, replayed: true }),
      )
    case 'unknown-timeout':
      return exitWithStdout(1, receipt('timed_out', 'unknown', { error_code: 'timeout' }))
    case 'unknown-native-fail':
      return exitWithStdout(
        1,
        receipt('failed', 'unknown', { returncode: 3, error_code: 'native_exit' }),
      )
    case 'exit1-silent':
      return exitWithStderr(1, 'send: something went wrong\\n')
    case 'exit2':
      return exitWithStderr(2, 'send: explicit --allow-write is required\\n')
    case 'exit130':
      return process.exit(130)
    case 'exit7':
      return process.exit(7)
    case 'nonjson-exit0':
      return exitWithStdout(0, 'this is not a JSON receipt\\n')
    case 'stderr-flood':
      return exitWithStderr(1, 'E'.repeat(5000) + '\\n')
    case 'hang':
      setInterval(() => {}, 1000)
      return
    default:
      return process.exit(99)
  }
}

main()
`

const workDir = mkdtempSync(join(tmpdir(), 'send-cli-test-'))
const scriptPath = join(workDir, 'mock-agent-sidecar.cjs')
writeFileSync(scriptPath, MOCK_SCRIPT)

afterAll(() => {
  rmSync(workDir, { recursive: true, force: true })
})

// ---------------------------------------------------------------------------
// Real child_process adapter for the SpawnLike seam.
// ---------------------------------------------------------------------------

function spawnWithEnv(env: Record<string, string>): SpawnLike {
  return (argv) => {
    const child = nodeSpawn(argv[0]!, argv.slice(1), {
      stdio: ['pipe', 'pipe', 'pipe'],
      env: { ...process.env, ...env },
    })
    // A dead pipe (ENOENT child) must not crash the adapter (SpawnLike
    // obligation: stdin errors surface via `exited`, never thrown).
    child.stdin!.on('error', () => {})
    const exited = new Promise<number | null>((resolve, reject) => {
      child.once('error', reject)
      child.once('close', (code) => resolve(code))
    })
    return {
      stdin: {
        write: (chunk) => {
          child.stdin!.write(chunk)
        },
        end: () => {
          child.stdin!.end()
        },
      },
      onStdout: (listener) => {
        child.stdout!.on('data', listener)
      },
      onStderr: (listener) => {
        child.stderr!.on('data', listener)
      },
      exited,
      kill: () => {
        child.kill('SIGKILL')
      },
    }
  }
}

// ---------------------------------------------------------------------------
// Harness.
// ---------------------------------------------------------------------------

interface LogRecord {
  level: SendCliLogLevel
  msg: string
  meta?: Record<string, unknown>
}

interface Observation {
  argv: string[]
  stdinBytes: number
  stdinSha256: string
}

let obsCounter = 0

function harness(
  scenario: string,
  opts?: { timeoutMs?: number; hardTimeoutBufferMs?: number; command?: readonly string[] },
) {
  obsCounter += 1
  const obsPath = join(workDir, `obs-${obsCounter}.json`)
  const logs: LogRecord[] = []
  const executor = createSendCliExecutor({
    spawn: spawnWithEnv({ MOCK_SCENARIO: scenario, MOCK_OBS_PATH: obsPath }),
    log: (level, msg, meta) => logs.push({ level, msg, ...(meta ? { meta } : {}) }),
    opts: {
      command: opts?.command ?? [process.execPath, scriptPath],
      ...(opts?.timeoutMs !== undefined ? { timeoutMs: opts.timeoutMs } : {}),
      ...(opts?.hardTimeoutBufferMs !== undefined
        ? { hardTimeoutBufferMs: opts.hardTimeoutBufferMs }
        : {}),
    },
  })
  return {
    executor,
    logs,
    observation: (): Observation => JSON.parse(readFileSync(obsPath, 'utf8')) as Observation,
  }
}

function request(overrides?: Partial<InjectExecutionRequest>): InjectExecutionRequest {
  return {
    target: { agent: 'claude', sessionId: 'sess-claude-42' },
    mode: 'queue',
    message: 'TOP-SECRET message body 你好',
    requestId: 'req-0001',
    ...overrides,
  }
}

function sha256(message: string): string {
  return createHash('sha256').update(Buffer.from(message, 'utf8')).digest('hex')
}

// ---------------------------------------------------------------------------
// Tests.
// ---------------------------------------------------------------------------

describe('createSendCliExecutor', () => {
  it('exposes the send-cli executor kind and spec defaults', () => {
    const { executor } = harness('ok')
    expect(executor.kind).toBe('send-cli')
    expect(DEFAULT_SEND_CLI_COMMAND).toEqual(['agent-sidecar'])
    expect(DEFAULT_SEND_TIMEOUT_MS).toBe(30_000)
  })

  it('parses a delivered receipt and assembles the exact message-free argv', async () => {
    const { executor, logs, observation } = harness('ok')
    const req = request()
    const result = await executor.execute(req)

    expect(result).toEqual({ outcome: 'delivered' })

    const obs = observation()
    // Verified argv shape: session_prefix positional, stdin transport,
    // no --mode / --agent (absent from sidecar/cli.py's send subcommand).
    expect(obs.argv).toEqual([
      'send',
      'sess-claude-42',
      '--message-stdin',
      '--allow-write',
      '--json',
      '--request-id',
      'req-0001',
      '--timeout',
      '30',
    ])
    expect(obs.argv.join(' ')).not.toContain('TOP-SECRET')

    // The log callback never sees the message body (S8).
    expect(JSON.stringify(logs)).not.toContain('TOP-SECRET')
    expect(logs.length).toBeGreaterThan(0)
  })

  it('delivers a 16 KiB message intact through stdin', async () => {
    const { executor, observation } = harness('ok')
    const message = 'x'.repeat(16 * 1024)
    const result = await executor.execute(request({ message }))

    expect(result.outcome).toBe('delivered')
    const obs = observation()
    expect(obs.stdinBytes).toBe(16 * 1024)
    expect(obs.stdinSha256).toBe(sha256(message))
  })

  it('delivers multibyte content intact through stdin', async () => {
    const { executor, observation } = harness('ok')
    const message = '多字节消息:你好,世界 — émoji 🚀🔍 と改行\nsecond line'
    const result = await executor.execute(request({ message }))

    expect(result.outcome).toBe('delivered')
    const obs = observation()
    expect(obs.stdinBytes).toBe(Buffer.byteLength(message, 'utf8'))
    expect(obs.stdinSha256).toBe(sha256(message))
    expect(obs.argv.join(' ')).not.toContain('你好')
  })

  it('passes the replayed flag through from the receipt', async () => {
    const { executor } = harness('replayed')
    const result = await executor.execute(request())
    expect(result.outcome).toBe('delivered')
    expect(result.replayed).toBe(true)
  })

  it('maps delivery:unknown receipts to terminal outcome unknown', async () => {
    const { executor } = harness('unknown-timeout')
    const result = await executor.execute(request())
    expect(result.outcome).toBe('unknown')
    expect(result.errorCode).toBe('timeout')
  })

  it('lets a delivery:unknown receipt win over the exit-1 fallback', async () => {
    const { executor } = harness('unknown-native-fail')
    const result = await executor.execute(request())
    // exit code is 1, but the parsed receipt is authoritative (S6: the
    // message may have been consumed, so this must not read as plain failed).
    expect(result.outcome).toBe('unknown')
    expect(result.errorCode).toBe('native_exit')
  })

  it('falls back to failed on exit 1 without a receipt, keeping stderr as detail', async () => {
    const { executor } = harness('exit1-silent')
    const result = await executor.execute(request())
    expect(result.outcome).toBe('failed')
    expect(result.errorCode).toBeUndefined()
    expect(result.detail).toContain('something went wrong')
  })

  it('maps exit 2 to usage_error', async () => {
    const { executor } = harness('exit2')
    const result = await executor.execute(request())
    expect(result).toMatchObject({ outcome: 'failed', errorCode: 'usage_error' })
    expect(result.detail).toContain('--allow-write')
  })

  it('maps exit 130 to interrupted', async () => {
    const { executor } = harness('exit130')
    const result = await executor.execute(request())
    expect(result).toMatchObject({ outcome: 'failed', errorCode: 'interrupted' })
  })

  it('maps other exit codes to exit_<code>', async () => {
    const { executor } = harness('exit7')
    const result = await executor.execute(request())
    expect(result).toMatchObject({ outcome: 'failed', errorCode: 'exit_7' })
  })

  it('treats exit 0 with non-JSON stdout as delivered with a parse warning', async () => {
    const { executor } = harness('nonjson-exit0')
    const result = await executor.execute(request())
    expect(result.outcome).toBe('delivered')
    expect(result.errorCode).toBeUndefined()
    expect(result.detail).toContain('parse_warning')
  })

  it('kills a hung CLI at the hard timeout and reports terminal unknown', async () => {
    const { executor } = harness('hang', { timeoutMs: 300, hardTimeoutBufferMs: 100 })
    const startedAt = Date.now()
    const result = await executor.execute(request())
    expect(result.outcome).toBe('unknown')
    expect(result.errorCode).toBe('timeout')
    // 300ms budget + 100ms buffer: well inside 5s proves the kill fired.
    expect(Date.now() - startedAt).toBeLessThan(5000)
  })

  it('clamps sub-second timeout budgets to the CLI floor of 1 second', async () => {
    const { executor, observation } = harness('ok', { timeoutMs: 300 })
    await executor.execute(request())
    const obs = observation()
    const timeoutAt = obs.argv.indexOf('--timeout')
    expect(timeoutAt).toBeGreaterThan(-1)
    expect(obs.argv[timeoutAt + 1]).toBe('1')
  })

  it('maps a missing CLI executable to cli_not_found', async () => {
    const { executor } = harness('ok', {
      command: [join(workDir, 'definitely-missing-agent-sidecar')],
    })
    const result = await executor.execute(request())
    expect(result.outcome).toBe('failed')
    expect(result.errorCode).toBe('cli_not_found')
    expect(result.detail).toContain('ENOENT')
  })

  it('truncates collected stderr detail at 2 KiB', async () => {
    const { executor } = harness('stderr-flood')
    const result = await executor.execute(request())
    expect(result.outcome).toBe('failed')
    expect(result.detail).toBeDefined()
    expect(result.detail!.length).toBe(STDERR_DETAIL_BYTES)
    expect(result.detail!.startsWith('EEE')).toBe(true)
  })
})
