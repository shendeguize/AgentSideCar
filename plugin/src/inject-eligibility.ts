/**
 * Pure host-side injection eligibility policy.
 *
 * This is the only place that interprets a full sidecar SessionRow for
 * injection. Callers retain/project only the static verdict; raw `extra` and
 * `parent_id` never need to cross the host wire.
 */

import type { SessionRow } from './bridge.ts'

export type InjectIneligibilityReason =
  | 'unsupported_agent'
  | 'working_session'
  | 'dead_session'
  | 'child_session'
  | 'remote_session'
  | 'invalid_session'

export type InjectEligibility =
  | Readonly<{ allowed: true; reason: 'eligible' }>
  | Readonly<{ allowed: false; reason: InjectIneligibilityReason }>

const ELIGIBLE: InjectEligibility = Object.freeze({ allowed: true, reason: 'eligible' })

const REJECTED: Readonly<Record<InjectIneligibilityReason, InjectEligibility>> = Object.freeze({
  unsupported_agent: Object.freeze({ allowed: false, reason: 'unsupported_agent' }),
  working_session: Object.freeze({ allowed: false, reason: 'working_session' }),
  dead_session: Object.freeze({ allowed: false, reason: 'dead_session' }),
  child_session: Object.freeze({ allowed: false, reason: 'child_session' }),
  remote_session: Object.freeze({ allowed: false, reason: 'remote_session' }),
  invalid_session: Object.freeze({ allowed: false, reason: 'invalid_session' }),
})

const EXTERNAL_AGENTS: ReadonlySet<string> = new Set([
  'claude',
  'codex',
  'cursor-cli',
  'kimi',
])
const LIVE_STATUSES: ReadonlySet<string> = new Set(['working', 'waiting', 'idle'])
const KNOWN_STATUSES: ReadonlySet<string> = new Set([...LIVE_STATUSES, 'dead'])

/** Match the sidecar remote-row JSON limits. */
export const MAX_SESSION_EXTRA_DEPTH = 32
export const MAX_SESSION_EXTRA_ITEMS = 8192
export const MAX_SESSION_EXTRA_STRING_BYTES = 256 * 1024
export const MAX_SESSION_EXTRA_BYTES = 256 * 1024

const INVALID_JSON_VALUE = Symbol('invalid-json-value')

interface OwnData {
  present: boolean
  value?: unknown
}

interface JsonBudget {
  items: number
  bytes: number
}

function isRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  try {
    const prototype = Object.getPrototypeOf(value)
    return prototype === Object.prototype || prototype === null
  } catch {
    return false
  }
}

function hasOwn(record: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key)
}

/** Read an own data property without invoking a getter. */
function ownData(record: Record<string, unknown>, key: string): OwnData | null {
  try {
    const descriptor = Object.getOwnPropertyDescriptor(record, key)
    if (descriptor === undefined) return { present: false }
    if (!('value' in descriptor)) return null
    return { present: true, value: descriptor.value }
  } catch {
    return null
  }
}

function cloneBoundedJson(
  value: unknown,
  depth: number,
  budget: JsonBudget,
  seen: WeakSet<object>,
): unknown | typeof INVALID_JSON_VALUE {
  budget.items += 1
  if (budget.items > MAX_SESSION_EXTRA_ITEMS || depth > MAX_SESSION_EXTRA_DEPTH) {
    return INVALID_JSON_VALUE
  }
  const consumeBytes = (bytes: number): boolean => {
    budget.bytes += bytes
    return budget.bytes <= MAX_SESSION_EXTRA_BYTES
  }
  if (value === null) return consumeBytes(4) ? value : INVALID_JSON_VALUE
  if (typeof value === 'boolean') {
    return consumeBytes(value ? 4 : 5) ? value : INVALID_JSON_VALUE
  }
  if (typeof value === 'number') {
    return Number.isFinite(value) && consumeBytes(Buffer.byteLength(String(value), 'utf8'))
      ? value
      : INVALID_JSON_VALUE
  }
  if (typeof value === 'string') {
    const bytes = Buffer.byteLength(value, 'utf8')
    return bytes <= MAX_SESSION_EXTRA_STRING_BYTES && consumeBytes(bytes + 2)
      ? value
      : INVALID_JSON_VALUE
  }
  if (typeof value !== 'object') return INVALID_JSON_VALUE
  if (seen.has(value)) return INVALID_JSON_VALUE
  seen.add(value)

  try {
    if (Array.isArray(value)) {
      if (!consumeBytes(2)) return INVALID_JSON_VALUE
      if (Object.getPrototypeOf(value) !== Array.prototype) return INVALID_JSON_VALUE
      if (Object.getOwnPropertySymbols(value).length > 0) return INVALID_JSON_VALUE
      const names = Object.getOwnPropertyNames(value)
      if (
        names.length !== value.length + 1 ||
        names[names.length - 1] !== 'length'
      ) {
        return INVALID_JSON_VALUE
      }
      const out: unknown[] = []
      for (let index = 0; index < value.length; index += 1) {
        if (index > 0 && !consumeBytes(1)) return INVALID_JSON_VALUE
        const descriptor = Object.getOwnPropertyDescriptor(value, String(index))
        if (descriptor === undefined || !('value' in descriptor) || !descriptor.enumerable) {
          return INVALID_JSON_VALUE
        }
        const item = cloneBoundedJson(descriptor.value, depth + 1, budget, seen)
        if (item === INVALID_JSON_VALUE) return INVALID_JSON_VALUE
        out.push(item)
      }
      return out
    }

    if (!isRecord(value) || Object.getOwnPropertySymbols(value).length > 0) {
      return INVALID_JSON_VALUE
    }
    if (!consumeBytes(2)) return INVALID_JSON_VALUE
    const out: Record<string, unknown> = {}
    const keys = Object.getOwnPropertyNames(value)
    for (let index = 0; index < keys.length; index += 1) {
      const key = keys[index]
      if (key === undefined) return INVALID_JSON_VALUE
      if (
        !consumeBytes(
          Buffer.byteLength(key, 'utf8') + 3 + (index > 0 ? 1 : 0),
        )
      ) {
        return INVALID_JSON_VALUE
      }
      const descriptor = Object.getOwnPropertyDescriptor(value, key)
      if (descriptor === undefined || !('value' in descriptor) || !descriptor.enumerable) {
        return INVALID_JSON_VALUE
      }
      const item = cloneBoundedJson(descriptor.value, depth + 1, budget, seen)
      if (item === INVALID_JSON_VALUE) return INVALID_JSON_VALUE
      // defineProperty avoids the legacy __proto__ setter.
      Object.defineProperty(out, key, {
        value: item,
        enumerable: true,
        configurable: true,
        writable: true,
      })
    }
    return out
  } catch {
    return INVALID_JSON_VALUE
  } finally {
    seen.delete(value)
  }
}

/**
 * Return a detached, accessor-free JSON object within the sidecar's bounds.
 * Invalid prototypes, cycles, non-JSON values, and accessors fail closed.
 */
export function sanitizeSessionExtra(value: unknown): Record<string, unknown> | null {
  if (!isRecord(value)) return null
  const cloned = cloneBoundedJson(value, 1, { items: 0, bytes: 0 }, new WeakSet())
  return cloned === INVALID_JSON_VALUE || !isRecord(cloned) ? null : cloned
}

type RemoteMarker = 'local' | 'remote' | 'invalid'

/**
 * Match sidecar.inject's established local/remote contract exactly:
 * `extra.host` presence, `remote === true`, or `source === "remote"`, plus
 * fleet rows' top-level host and explicit remote alias/host markers.
 * Absence of a top-level host is the normal daemon-local shape.
 */
function remoteMarker(
  row: Record<string, unknown>,
  extra: Record<string, unknown>,
): RemoteMarker {
  const host = ownData(row, 'host')
  const remote = ownData(row, 'remote')
  const source = ownData(row, 'source')
  const remoteAlias = ownData(row, 'remote_alias')
  const remoteHost = ownData(row, 'remote_host')
  if (
    host === null ||
    remote === null ||
    source === null ||
    remoteAlias === null ||
    remoteHost === null
  ) {
    return 'invalid'
  }
  if (host.present && (typeof host.value !== 'string' || host.value === '')) return 'invalid'
  if (remote.present && typeof remote.value !== 'boolean') return 'invalid'
  if (source.present && typeof source.value !== 'string') return 'invalid'
  for (const marker of [remoteAlias, remoteHost]) {
    if (marker.present && (typeof marker.value !== 'string' || marker.value === '')) {
      return 'invalid'
    }
  }

  for (const key of ['remote_alias', 'remote_host']) {
    if (hasOwn(extra, key)) {
      const value = extra[key]
      if (typeof value !== 'string' || value === '') return 'invalid'
      return 'remote'
    }
  }

  if (
    (host.present && host.value !== 'local') ||
    remote.value === true ||
    source.value === 'remote' ||
    remoteAlias.present ||
    remoteHost.present ||
    hasOwn(extra, 'host') ||
    extra['remote'] === true ||
    extra['source'] === 'remote'
  ) {
    return 'remote'
  }
  return 'local'
}

/** Python's `extra.get("sidechain", False) is not False` contract. */
function isSidechain(extra: Record<string, unknown>): boolean {
  return hasOwn(extra, 'sidechain') && extra['sidechain'] !== false
}

/**
 * Derive one stable, body-free verdict from the complete sidecar row.
 *
 * After structural validation, explicit remote provenance wins so no remote
 * row can be represented by a weaker local-state verdict.
 * Dsh deliberately skips external child/sidechain rejection; its in-process
 * preflight owns whether that topology can be resumed or steered.
 */
export function deriveInjectEligibility(row: SessionRow): InjectEligibility {
  if (!isRecord(row)) return REJECTED.invalid_session

  const agent = ownData(row, 'agent')
  const sessionId = ownData(row, 'session_id')
  const project = ownData(row, 'project')
  const transcript = ownData(row, 'transcript')
  const updatedAt = ownData(row, 'updated_at')
  const title = ownData(row, 'title')
  const status = ownData(row, 'status')
  const rawExtra = ownData(row, 'extra')
  const parentId = ownData(row, 'parent_id')
  const invalidMarker = ownData(row, 'invalid_session')
  if (
    agent === null ||
    sessionId === null ||
    project === null ||
    transcript === null ||
    updatedAt === null ||
    title === null ||
    status === null ||
    rawExtra === null ||
    parentId === null ||
    invalidMarker === null ||
    !agent.present ||
    typeof agent.value !== 'string' ||
    agent.value === '' ||
    !sessionId.present ||
    typeof sessionId.value !== 'string' ||
    sessionId.value === '' ||
    !project.present ||
    typeof project.value !== 'string' ||
    !transcript.present ||
    typeof transcript.value !== 'string' ||
    !updatedAt.present ||
    typeof updatedAt.value !== 'number' ||
    !Number.isFinite(updatedAt.value) ||
    !title.present ||
    typeof title.value !== 'string' ||
    !status.present ||
    typeof status.value !== 'string' ||
    !KNOWN_STATUSES.has(status.value) ||
    !rawExtra.present ||
    !parentId.present ||
    (parentId.value !== null && typeof parentId.value !== 'string') ||
    (invalidMarker.present && invalidMarker.value !== true)
  ) {
    return REJECTED.invalid_session
  }

  if (invalidMarker.value === true) return REJECTED.invalid_session
  const extra = sanitizeSessionExtra(rawExtra.value)
  if (extra === null) return REJECTED.invalid_session
  const marker = remoteMarker(row, extra)
  if (marker === 'invalid') return REJECTED.invalid_session
  if (marker === 'remote') return REJECTED.remote_session

  const isDsh = agent.value === 'dsh'
  if (!isDsh && !EXTERNAL_AGENTS.has(agent.value)) {
    return REJECTED.unsupported_agent
  }

  if (status.value === 'dead') return REJECTED.dead_session
  if (!isDsh && status.value === 'working') return REJECTED.working_session

  if (!isDsh && (parentId.value !== null || isSidechain(extra))) {
    return REJECTED.child_session
  }

  return ELIGIBLE
}
