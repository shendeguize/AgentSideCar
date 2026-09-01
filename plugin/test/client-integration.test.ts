/**
 * Unit tests for the T2.4 integration glue: the controller's pure snapshot
 * mapping (epoch-seconds → epoch-ms at the boundary), composite stream
 * health, filter persistence, and the settings-glue conversions between the
 * host Config wire shape and the settings card's flat values. Node
 * environment, injected fakes throughout (fake stream, Map-backed storage).
 */

import { readFileSync } from 'node:fs'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import {
  injectBlockReason,
  INVALID_INJECT_ELIGIBILITY,
  normalizeInjectEligibility,
  normalizeTimelineHealth,
  sessionInjectEligibility,
} from '../src/client/api.ts'
import type {
  InjectEligibility,
  SessionDetail as SessionDetailWire,
  SessionView,
  StateSnapshot,
  TimelineHealth,
} from '../src/client/api.ts'
import type { StreamStatus } from '../src/client/sse.ts'
import {
  FILTERS_STORAGE_KEY,
  PLUGIN_ID,
  SidecarController,
  combineStreamHealth,
  daemonDetailOf,
  initialViewState,
  mapSessions,
  mapSnapshot,
  readStoredFilters,
  type StateStreamLike,
  type StorageLike,
} from '../src/client/controller.ts'
import {
  DEFAULT_CONFIG_VIEW,
  cardValuesEqual,
  configToValues,
  diffGroups,
  joinCommand,
  splitCommand,
  valuesToConfigView,
} from '../src/client/settings-glue.ts'
import {
  SettingsCard,
  normalizeAnalysisRouteField,
  resolveAnalysisRoute,
} from '../src/client/settings-card.tsx'
import {
  createDefaultIntegration,
  type AnalysisStorePort,
  type BoardUiPort,
  type DetailStorePort,
  type ProjectsStorePort,
  type SearchStorePort,
} from '../src/client/ui-integration.ts'
import { Board, matchesSessionFocusTarget } from '../src/client/board/Board.tsx'
import {
  agentFilterOptions,
  filterSessions,
  withAgentFilter,
  type BoardFilterState,
  type SessionCardVM,
} from '../src/client/board/logic.ts'
import { ProjectView } from '../src/client/board/project-view.tsx'
import { AnalysisPanel } from '../src/client/analysis/AnalysisPanel.tsx'
import type { AnalysisGlueState } from '../src/client/analysis-glue.ts'
import {
  createInjectEligibilityRefresher,
  DetailInjectTrigger,
  requestInjectOpen,
  TimelineAvailabilityBoundary,
} from '../src/client/detail-view.tsx'
import { DetailStore } from '../src/client/detail-glue.ts'
import type { SessionDetailWire as M3SessionDetailWire } from '../src/client/detail/transport.ts'
import type { TimelinePageWire } from '../src/client/detail/logic.ts'
import {
  ERROR_COPY as CLIENT_INJECT_ERROR_COPY,
  InjectPanel,
  createPanelPrepareRequest,
  displayInjectOutcome,
  effectiveInjectMode,
  injectErrorCopy,
  reduceEligibilityAwarePanel,
  resultCopyKey,
} from '../src/client/inject/InjectPanel.tsx'
import {
  classifyExecuteResponse,
  classifyPanelKey,
  isDeliveredResult,
  resultActions,
} from '../src/client/inject/logic.ts'
import { en } from '../src/client/locales/en.ts'
import { setLocale } from '../src/client/locales/index.ts'
import { zh } from '../src/client/locales/zh.ts'
import {
  clampScrollTop,
  scheduleAfterLayout,
} from '../src/client/mount.tsx'

vi.mock('@deepseek-ai/dsh-client-ui-primitives', () => ({
  Button: 'button',
  IconChevronDownOutline14: 'svg',
  Input: 'input',
  Modal: 'div',
  Pill: 'span',
  StateDot: 'span',
  writeClipboard: vi.fn(() => Promise.resolve(true)),
}))

const MOUNT_SOURCE = readFileSync(
  new URL('../src/client/mount.tsx', import.meta.url),
  'utf8',
)
const BOARD_SOURCE = readFileSync(
  new URL('../src/client/board/Board.tsx', import.meta.url),
  'utf8',
)
const DETAIL_SOURCE = readFileSync(
  new URL('../src/client/detail/SessionDetail.tsx', import.meta.url),
  'utf8',
)
const DETAIL_VIEW_SOURCE = readFileSync(
  new URL('../src/client/detail-view.tsx', import.meta.url),
  'utf8',
)
const PROJECT_VIEW_SOURCE = readFileSync(
  new URL('../src/client/board/project-view.tsx', import.meta.url),
  'utf8',
)
const ANALYSIS_CSS_SOURCE = readFileSync(
  new URL('../src/client/analysis/analysis.module.css', import.meta.url),
  'utf8',
)

// ---------------------------------------------------------------------------
// Fakes and fixtures.
// ---------------------------------------------------------------------------

class FakeStream implements StateStreamLike {
  readonly mode = 'sse' as const
  status: StreamStatus = 'connecting'
  started = 0
  stopped = 0
  polled = 0
  private snapshotCb: ((snapshot: StateSnapshot) => void) | null = null
  private statusCb: ((status: StreamStatus) => void) | null = null

  start(): void {
    this.started += 1
  }

  stop(): void {
    this.stopped += 1
  }

  pollNow(): void {
    this.polled += 1
  }

  onSnapshot(cb: (snapshot: StateSnapshot) => void): () => void {
    this.snapshotCb = cb
    return () => {}
  }

  onStatus(cb: (status: StreamStatus) => void): () => void {
    this.statusCb = cb
    cb(this.status)
    return () => {}
  }

  emitSnapshot(snapshot: StateSnapshot): void {
    this.snapshotCb?.(snapshot)
  }

  emitStatus(status: StreamStatus): void {
    this.status = status
    this.statusCb?.(status)
  }
}

class FakeStorage implements StorageLike {
  readonly map = new Map<string, string>()

  getItem(key: string): string | null {
    return this.map.get(key) ?? null
  }

  setItem(key: string, value: string): void {
    this.map.set(key, value)
  }
}

class FakeAnimationFrames {
  private nextHandle = 1
  private readonly callbacks = new Map<number, (time: number) => void>()
  readonly cancelled: number[] = []

  requestAnimationFrame(callback: (time: number) => void): number {
    const handle = this.nextHandle
    this.nextHandle += 1
    this.callbacks.set(handle, callback)
    return handle
  }

  cancelAnimationFrame(handle: number): void {
    this.cancelled.push(handle)
    this.callbacks.delete(handle)
  }

  flushOne(): void {
    const entry = this.callbacks.entries().next().value as
      | [number, (time: number) => void]
      | undefined
    if (entry === undefined) return
    const [handle, callback] = entry
    this.callbacks.delete(handle)
    callback(0)
  }

  get pending(): number {
    return this.callbacks.size
  }
}

function snapshotFixture(): StateSnapshot {
  return {
    daemon: {
      state: 'adopted',
      lastPing: { pid: 4242, version: '0.6.0', http: { enabled: false } },
    },
    board: {
      sessions: [
        {
          agent: 'dsh',
          session_id: 'sess-alpha',
          status: 'working',
          title: 'Fix the flux capacitor',
          project: '/home/u/proj',
          updated_at: 1_700_000_000,
          last_event: { ts: '2026-08-24T00:00:00Z', kind: 'message', text: 'hello' },
          gap: false,
        },
        {
          agent: 'claude',
          session_id: 'sess-beta',
          status: 'idle',
          title: '',
          project: '',
          updated_at: 1_700_000_100,
          last_event: null,
          gap: true,
        },
      ],
      streamHealth: 'ok',
      lastReconcileAt: 1_700_000_200_000,
    },
    capabilities: { inject: false },
  }
}

// ---------------------------------------------------------------------------
// Identity pins.
// ---------------------------------------------------------------------------

describe('PLUGIN_ID', () => {
  it('mirrors the package.json name (localStorage prefix contract)', () => {
    const pkg = JSON.parse(
      readFileSync(new URL('../package.json', import.meta.url), 'utf8'),
    ) as { name: string }
    expect(PLUGIN_ID).toBe(pkg.name)
    expect(FILTERS_STORAGE_KEY.startsWith(`${pkg.name}:`)).toBe(true)
  })
})

describe('locale root composition', () => {
  it('keeps one locale subscription in each board root wrapper', () => {
    const contentStart = MOUNT_SOURCE.indexOf('function createBoardContent(')
    const boardRootStart = MOUNT_SOURCE.indexOf('export function createBoardTab(')
    const overlayRootStart = MOUNT_SOURCE.indexOf('export function createCenterOverlay(')
    const footerRootStart = MOUNT_SOURCE.indexOf('export function createFooterWidget(')

    expect([contentStart, boardRootStart, overlayRootStart, footerRootStart])
      .not.toContain(-1)

    const content = MOUNT_SOURCE.slice(contentStart, boardRootStart)
    const boardRoot = MOUNT_SOURCE.slice(boardRootStart, overlayRootStart)
    const overlayRoot = MOUNT_SOURCE.slice(overlayRootStart, footerRootStart)

    expect(content).not.toContain('useActiveLocale()')
    expect(boardRoot.match(/useActiveLocale\(\)/g)).toHaveLength(1)
    expect(overlayRoot.match(/useActiveLocale\(\)/g)).toHaveLength(1)
    expect(boardRoot).toContain('createBoardContent(controller, integration)')
    expect(overlayRoot).toContain('createBoardContent(controller, integration)')
    expect(overlayRoot).not.toContain('createBoardTab(')
  })
})

function eligibilitySession(
  agent: string,
  status: string,
  eligibility?: InjectEligibility,
  spelling: 'camel' | 'snake' = 'camel',
): SessionView {
  const session: SessionView = {
    agent,
    session_id: `session-${agent}`,
    status,
    title: `${agent} session`,
    project: '/tmp/project',
    updated_at: 1_700_000_000,
    last_event: null,
    gap: false,
  }
  if (eligibility === undefined) return session
  return spelling === 'camel'
    ? { ...session, injectEligibility: eligibility }
    : { ...session, inject_eligibility: eligibility }
}

function detailWire(session: SessionView): SessionDetailWire {
  return {
    session,
    timeline: null,
    timelineNote: 'eligibility-only fixture',
  }
}

describe('client injection eligibility', () => {
  const allowed = { allowed: true, reason: 'eligible' } as const

  it.each(['cursor-ide'])(
    'consumes the host rejection for unsupported %s targets',
    (agent) => {
      const verdict = sessionInjectEligibility(eligibilitySession(agent, 'waiting', {
        allowed: false,
        reason: 'unsupported_agent',
      }))
      expect(verdict).toEqual({ allowed: false, reason: 'unsupported_agent' })
    },
  )

  it.each(['waiting', 'idle'])(
    'renders protected resume without steering for eligible Kimi %s sessions',
    (status) => {
      const eligibility = sessionInjectEligibility(eligibilitySession('kimi', status, allowed))
      expect(eligibility).toEqual(allowed)
      try {
        setLocale('en')
        const html = renderToStaticMarkup(createElement(InjectPanel, {
          capability: { inject: true },
          eligibility,
          target: { agent: 'kimi', sessionId: 'session-kimi' },
          defaultMode: 'steer',
          onPrepare: vi.fn(),
          onExecute: vi.fn(),
        }))
        expect(html).toContain('<textarea')
        expect(html).toContain('Protected resume')
        expect(html).toContain('separate Kimi ACP process')
        expect(html).toContain('never attaches to or steers the existing terminal')
        expect(html).toContain('Prepare protected resume')
        expect(html).not.toContain('agent-sidecar-inject-blocked-reason')
        expect(html).not.toContain('steer (mid-turn)')
        expect(html).not.toContain('type="radio"')
        expect(effectiveInjectMode('kimi', 'steer')).toBe('queue')
        expect(createPanelPrepareRequest(
          { agent: 'kimi', sessionId: 'session-kimi' },
          'resume safely',
          'steer',
        )).toEqual({
          target: { agent: 'kimi', sessionId: 'session-kimi' },
          mode: 'queue',
          message: 'resume safely',
        })
      } finally {
        setLocale('zh')
      }
    },
  )

  it.each([
    ['working', 'working_session'],
    ['child', 'child_session'],
    ['remote', 'remote_session'],
  ] as const)('keeps Kimi %s disabled from the host verdict', (_label, reason) => {
    const eligibility = sessionInjectEligibility(eligibilitySession('kimi', 'working', {
      allowed: false,
      reason,
    }))
    expect(injectBlockReason(true, eligibility)).toBe(reason)
    const html = renderToStaticMarkup(createElement(DetailInjectTrigger, {
      injectEnabled: true,
      eligibility,
      onOpen: vi.fn(),
    }))
    expect(html).toContain('disabled=""')
  })

  it.each([
    ['external working status', 'claude', 'working', 'working_session'],
    ['external child topology', 'cursor-cli', 'waiting', 'child_session'],
    ['external remote target', 'codex', 'idle', 'remote_session'],
    ['invalid external status', 'claude', 'paused', 'invalid_session'],
    ['ended target', 'claude', 'dead', 'dead_session'],
  ] as const)('fails closed for %s', (_label, agent, status, reason) => {
    expect(sessionInjectEligibility(eligibilitySession(agent, status, {
      allowed: false,
      reason,
    }))).toEqual({ allowed: false, reason })
  })

  it.each([
    ['working', 'working dsh target'],
    ['waiting', 'child dsh target'],
  ] as const)('keeps dsh %s operable when the host verdict allows it', (status, title) => {
    expect(sessionInjectEligibility({
      ...eligibilitySession('dsh', status, allowed),
      title,
    })).toEqual(allowed)
  })

  it('accepts both current verdict spellings without ever defaulting to allowed', () => {
    expect(sessionInjectEligibility(
      eligibilitySession('claude', 'waiting', allowed, 'camel'),
    )).toEqual(allowed)
    expect(sessionInjectEligibility(
      eligibilitySession('claude', 'waiting', allowed, 'snake'),
    )).toEqual(allowed)
    expect(sessionInjectEligibility(
      eligibilitySession('claude', 'waiting'),
    )).toBe(INVALID_INJECT_ELIGIBILITY)
    expect(normalizeInjectEligibility({
      allowed: true,
      reason: 'unknown_future_reason',
    })).toBe(INVALID_INJECT_ELIGIBILITY)
    expect(normalizeInjectEligibility({
      allowed: false,
      reason: 'private_raw_code',
    })).toBe(INVALID_INJECT_ELIGIBILITY)
  })

  it('checks target identity and gives the global gate reason priority', () => {
    const session = eligibilitySession('claude', 'working', {
      allowed: false,
      reason: 'working_session',
    })
    expect(sessionInjectEligibility(session, {
      agent: 'codex',
      sessionId: session.session_id,
    })).toBe(INVALID_INJECT_ELIGIBILITY)
    expect(injectBlockReason(false, sessionInjectEligibility(session)))
      .toBe('inject_disabled')
    expect(injectBlockReason(true, sessionInjectEligibility(session)))
      .toBe('working_session')
  })

  it('maps every eligibility error to localized, code-free copy', () => {
    expect(CLIENT_INJECT_ERROR_COPY).toMatchObject({
      unsupported_agent: 'inject.errUnsupportedAgent',
      working_session: 'inject.errWorkingSession',
      child_session: 'inject.errChildSession',
      remote_session: 'inject.errRemoteSession',
      invalid_session: 'inject.errInvalidSession',
      target_dead: 'inject.errTargetDead',
    })
    for (const code of [
      'unsupported_agent',
      'working_session',
      'child_session',
      'remote_session',
      'invalid_session',
      'target_dead',
    ]) {
      expect(injectErrorCopy(code).key).not.toBe('inject.errGeneric')
    }
    expect(injectErrorCopy('private_raw_code')).toEqual({ key: 'inject.errUnknown' })
  })

  it('keeps English and Chinese eligibility keys in parity', () => {
    expect(Object.keys(en).sort()).toEqual(Object.keys(zh).sort())
    for (const key of Object.values(CLIENT_INJECT_ERROR_COPY)) {
      expect(en[key]).toBeTypeOf('string')
      expect(zh[key]).toBeTypeOf('string')
    }
  })

  it('native-disables detail triggers with unique, consistent ARIA reasons', () => {
    try {
      setLocale('en')
      const html = renderToStaticMarkup(createElement('div', null,
        createElement(DetailInjectTrigger, {
          injectEnabled: false,
          eligibility: { allowed: false, reason: 'working_session' },
          onOpen: () => {},
        }),
        createElement(DetailInjectTrigger, {
          injectEnabled: true,
          eligibility: { allowed: false, reason: 'working_session' },
          onOpen: () => {},
        }),
      ))
      const describedBy = [...html.matchAll(/aria-describedby="([^"]+)"/g)]
        .map((match) => match[1])

      expect(html.match(/disabled=""/g)).toHaveLength(2)
      expect(html).toContain('title="Injection is disabled on the server')
      expect(html).toContain('title="This external-agent session is working')
      expect(describedBy).toHaveLength(2)
      expect(new Set(describedBy).size).toBe(2)
      for (const id of describedBy) expect(html).toContain(`id="${id}"`)
    } finally {
      setLocale('zh')
    }
  })

  it.each(['mouse click', 'Enter', 'Space'])(
    'retains an activation guard for blocked %s paths',
    () => {
      const opened = vi.fn()
      expect(requestInjectOpen('working_session', opened)).toBe(false)
      expect(opened).not.toHaveBeenCalled()
    },
  )

  it('blocks panel prepare shortcuts and callbacks while ineligible', () => {
    const onPrepare = vi.fn(() => Promise.resolve({
      kind: 'http' as const,
      reason: 'working_session',
      status: 409,
    }))
    const onExecute = vi.fn(() => Promise.resolve({
      outcome: 'failed' as const,
      errorCode: 'working_session',
    }))
    try {
      setLocale('en')
      const html = renderToStaticMarkup(createElement(InjectPanel, {
        capability: { inject: true },
        eligibility: { allowed: false, reason: 'working_session' },
        target: { agent: 'claude', sessionId: 'session-claude' },
        defaultMode: 'queue',
        onPrepare,
        onExecute,
      }))

      expect(html).toContain('This external-agent session is working')
      expect(html).not.toContain('working_session')
      expect(html).not.toContain('Prepare injection')
      expect(onPrepare).not.toHaveBeenCalled()
      expect(onExecute).not.toHaveBeenCalled()
      expect(classifyPanelKey({
        key: 'Enter',
        metaKey: true,
        ctrlKey: false,
        phase: 'idle',
        canPrepare: false,
      })).toBeNull()
      expect(classifyPanelKey({
        key: 'Enter',
        metaKey: false,
        ctrlKey: true,
        phase: 'idle',
        canPrepare: false,
      })).toBeNull()
    } finally {
      setLocale('zh')
    }
  })

  it('preserves a Kimi unknown receipt while accepting explicit delivery', () => {
    const response = { outcome: 'unknown' as const, errorCode: 'executor_error' }
    expect(classifyExecuteResponse(response)).toEqual({
      type: 'EXECUTE_RESULT',
      result: response,
    })
    expect(isDeliveredResult(response)).toBe(false)
    expect(resultActions(response.outcome)).toEqual({
      canReprepare: false,
      showCheckSessionHint: true,
    })
    expect(displayInjectOutcome('kimi', 'delivered')).toBe('delivered')
    expect(resultCopyKey('kimi', 'delivered')).toBe('inject.resultDelivered')
    expect(resultCopyKey('kimi', 'unknown')).toBe('inject.kimiResultUnknown')
    expect(resultCopyKey('kimi', 'failed')).toBe('inject.kimiResultFailed')
    expect(en['inject.kimiResultUnknown']).toContain('Kimi 0.38 completed')
    expect(en['inject.kimiResultUnknown']).toContain('delivery')
    expect(en['inject.kimiResultUnknown']).toContain('cannot be proven')
    expect(en['inject.kimiResultUnknown']).toContain('Do not automatically or manually retry')
    expect(en['inject.kimiResultUnknown']).not.toMatch(/\bdelivered\b|\bsuccess(?:ful|fully)?\b/i)
    expect(zh['inject.kimiResultUnknown']).toContain('已完成')
    expect(zh['inject.kimiResultUnknown']).toContain('无法证明')
    expect(zh['inject.kimiResultUnknown']).toContain('请勿自动或手工盲目重试')
    expect(zh['inject.kimiResultUnknown']).not.toMatch(/已送达|成功送达/)
    expect(en['inject.kimiResultReplayed']).toContain('same request')
    expect(en['inject.kimiResultReplayed']).toContain('No new Kimi ACP process')
    expect(zh['inject.kimiResultReplayed']).toContain('同一请求')
    expect(zh['inject.kimiResultReplayed']).toContain('未启动新的 Kimi ACP 进程')
    expect(en['inject.kimiResultFailed']).toContain('before the prompt was sent')
    expect(en['inject.kimiResultFailed']).toContain('no message was sent')
    expect(zh['inject.kimiResultFailed']).toContain('提示词发送前')
    expect(zh['inject.kimiResultFailed']).toContain('未向 Kimi 发送消息')
  })

  it('fails a prepared Kimi panel closed when live eligibility becomes working', () => {
    const state = {
      phase: 'confirm' as const,
      message: 'resume safely',
      mode: 'queue' as const,
      requestId: 'request-kimi-live-transition',
      confirmToken: 'confirm-kimi-live-transition',
      plan: {
        target: { agent: 'kimi', sessionId: 'session-kimi' },
        mode: 'queue' as const,
        targetStatus: {
          agent: 'kimi',
          sessionId: 'session-kimi',
          status: 'waiting',
        },
        messagePreview: { bytes: 13, head: 'resume safely' },
      },
      expiresAt: Date.now() + 60_000,
    }
    expect(reduceEligibilityAwarePanel(state, { type: 'ELIGIBILITY_BLOCKED' }))
      .toEqual({ phase: 'idle', notice: null })

    try {
      setLocale('en')
      const html = renderToStaticMarkup(createElement(InjectPanel, {
        capability: { inject: true },
        eligibility: { allowed: false, reason: 'working_session' },
        target: { agent: 'kimi', sessionId: 'session-kimi' },
        defaultMode: 'steer',
        onPrepare: vi.fn(),
        onExecute: vi.fn(),
      }))
      expect(html).toContain('Protected resume')
      expect(html).toContain('Kimi session is working')
      expect(html).not.toContain('<textarea')
      expect(html).not.toContain('Prepare protected resume')
    } finally {
      setLocale('zh')
    }
  })

  it.each(['claude', 'codex', 'dsh'])(
    'keeps generic queue and steer controls for %s',
    (agent) => {
      try {
        setLocale('en')
        const html = renderToStaticMarkup(createElement(InjectPanel, {
          capability: { inject: true },
          eligibility: allowed,
          target: { agent, sessionId: `session-${agent}` },
          defaultMode: 'steer',
          onPrepare: vi.fn(),
          onExecute: vi.fn(),
        }))
        expect(html).toContain('Inject message')
        expect(html).toContain('queue (next turn)')
        expect(html).toContain('steer (mid-turn)')
        expect(html).toMatch(/<input[^>]*checked=""[^>]*value="steer"/)
        expect(html).toContain('Prepare injection')
        expect(effectiveInjectMode(agent, 'steer')).toBe('steer')
      } finally {
        setLocale('zh')
      }
    },
  )

  it('renders missing verdicts closed and becomes usable again when eligible', () => {
    const props = {
      capability: { inject: true },
      target: { agent: 'dsh', sessionId: 'session-dsh' },
      defaultMode: 'steer' as const,
      onPrepare: () => Promise.resolve({
        kind: 'http' as const,
        reason: 'invalid_session',
        status: 422,
      }),
      onExecute: () => Promise.resolve({
        outcome: 'failed' as const,
        errorCode: 'invalid_session',
      }),
    }
    try {
      setLocale('en')
      const missing = renderToStaticMarkup(createElement(InjectPanel, props))
      const restored = renderToStaticMarkup(createElement(InjectPanel, {
        ...props,
        eligibility: allowed,
      }))

      expect(missing).toContain('host did not provide a valid target eligibility verdict')
      expect(missing).not.toContain('invalid_session')
      expect(restored).toContain('<textarea')
      expect(restored).toContain('Prepare injection')
      expect(restored).not.toContain('eligibility verdict')
    } finally {
      setLocale('zh')
    }
  })

  it('updates waiting → working → waiting from explicit detail/SSE refreshes only', async () => {
    const responses = [
      detailWire(eligibilitySession('claude', 'waiting', allowed)),
      detailWire(eligibilitySession('claude', 'working', {
        allowed: false,
        reason: 'working_session',
      })),
      detailWire(eligibilitySession('claude', 'waiting', allowed)),
    ]
    const fetchSessionFn = vi.fn(async () => {
      const response = responses.shift()
      if (response === undefined) throw new Error('unexpected implicit refresh')
      return response
    })
    const seen: InjectEligibility[] = []
    const refresher = createInjectEligibilityRefresher(
      'session-claude',
      () => 'claude',
      (eligibility) => { seen.push(eligibility) },
      fetchSessionFn,
    )

    const first = refresher.refresh()
    expect(seen).toEqual([INVALID_INJECT_ELIGIBILITY])
    await first
    const transition = refresher.refresh()
    expect(seen.at(-1)).toBe(INVALID_INJECT_ELIGIBILITY)
    await transition
    const recovery = refresher.refresh()
    expect(seen.at(-1)).toBe(INVALID_INJECT_ELIGIBILITY)
    await recovery
    refresher.dispose()

    expect(seen).toEqual([
      INVALID_INJECT_ELIGIBILITY,
      allowed,
      INVALID_INJECT_ELIGIBILITY,
      { allowed: false, reason: 'working_session' },
      INVALID_INJECT_ELIGIBILITY,
      allowed,
    ])
    expect(fetchSessionFn).toHaveBeenCalledTimes(3)
    expect(DETAIL_VIEW_SOURCE).toContain('void eligibilityRefresher.refresh()')
    expect(DETAIL_VIEW_SOURCE).not.toContain('setInterval')
  })

  it.each(['resolve', 'reject'] as const)(
    'suppresses an aborted old allow %s after the latest deny publishes',
    async (oldSettlement) => {
      const deferred = <T,>(): {
        promise: Promise<T>
        resolve(value: T): void
        reject(reason: unknown): void
      } => {
        let resolve!: (value: T) => void
        let reject!: (reason: unknown) => void
        const promise = new Promise<T>((resolvePromise, rejectPromise) => {
          resolve = resolvePromise
          reject = rejectPromise
        })
        return { promise, resolve, reject }
      }
      const oldAllow = deferred<SessionDetailWire>()
      const latestDeny = deferred<SessionDetailWire>()
      const signals: Array<{ aborted: boolean }> = []
      let call = 0
      const fetchSessionFn = vi.fn((
        _sessionId: string,
        opts?: { signal?: { aborted: boolean } },
      ): Promise<SessionDetailWire> => {
        if (opts?.signal !== undefined) signals.push(opts.signal)
        call += 1
        return call === 1 ? oldAllow.promise : latestDeny.promise
      })
      const seen: InjectEligibility[] = []
      const refresher = createInjectEligibilityRefresher(
        'session-claude',
        () => 'claude',
        (eligibility) => { seen.push(eligibility) },
        fetchSessionFn,
      )

      const oldRequest = refresher.refresh()
      const latestRequest = refresher.refresh()
      expect(signals[0]?.aborted).toBe(true)
      expect(signals[1]?.aborted).toBe(false)

      latestDeny.resolve(detailWire(eligibilitySession('claude', 'working', {
        allowed: false,
        reason: 'working_session',
      })))
      await latestRequest
      expect(seen.at(-1)).toEqual({ allowed: false, reason: 'working_session' })

      if (oldSettlement === 'resolve') {
        oldAllow.resolve(detailWire(eligibilitySession('claude', 'waiting', allowed)))
      } else {
        oldAllow.reject(new Error('stale transport failure'))
      }
      await oldRequest

      expect(seen).toEqual([
        INVALID_INJECT_ELIGIBILITY,
        INVALID_INJECT_ELIGIBILITY,
        { allowed: false, reason: 'working_session' },
      ])
      refresher.dispose()
    },
  )

  it('invalidates in-flight results on agent change and session disposal', async () => {
    const pending: Array<{
      resolve(value: SessionDetailWire): void
      promise: Promise<SessionDetailWire>
    }> = []
    const signals: Array<{ aborted: boolean }> = []
    const fetchSessionFn = vi.fn((
      _sessionId: string,
      opts?: { signal?: { aborted: boolean } },
    ): Promise<SessionDetailWire> => {
      let resolve!: (value: SessionDetailWire) => void
      const promise = new Promise<SessionDetailWire>((done) => { resolve = done })
      pending.push({ promise, resolve })
      if (opts?.signal !== undefined) signals.push(opts.signal)
      return promise
    })
    let agent = 'claude'
    const seen: InjectEligibility[] = []
    const refresher = createInjectEligibilityRefresher(
      'session-shared',
      () => agent,
      (eligibility) => { seen.push(eligibility) },
      fetchSessionFn,
    )

    const changedAgentRequest = refresher.refresh()
    agent = 'codex'
    pending[0]?.resolve(detailWire({
      ...eligibilitySession('claude', 'waiting', allowed),
      session_id: 'session-shared',
    }))
    await changedAgentRequest
    expect(seen).toEqual([INVALID_INJECT_ELIGIBILITY])

    const disposedSessionRequest = refresher.refresh()
    refresher.dispose()
    expect(signals[1]?.aborted).toBe(true)
    pending[1]?.resolve(detailWire({
      ...eligibilitySession('codex', 'waiting', allowed),
      session_id: 'session-shared',
    }))
    await disposedSessionRequest
    expect(seen).toEqual([
      INVALID_INJECT_ELIGIBILITY,
      INVALID_INJECT_ELIGIBILITY,
    ])
  })
})

describe('timeline source outcome integration', () => {
  const healthyOutcomes = {
    liveSession: 'succeeded',
    sessionQuery: 'not_found',
    sidecarReplay: 'unavailable',
    buffer: 'not_found',
  } as const
  const partialOutcomes = {
    liveSession: 'succeeded',
    sessionQuery: 'source_failed',
    sidecarReplay: 'unavailable',
    buffer: 'not_found',
  } as const
  const failedOutcomes = {
    liveSession: 'source_failed',
    sessionQuery: 'unavailable',
    sidecarReplay: 'replay_unsupported',
    buffer: 'not_found',
  } as const

  const renderBoundary = (
    health: TimelineHealth,
    entryCount: number,
    child = 'healthy-empty-content',
    extra: Record<string, unknown> = {},
  ): string => renderToStaticMarkup(createElement(TimelineAvailabilityBoundary, {
    health,
    entryCount,
    refreshing: false,
    onRefresh: () => {},
    onClose: () => {},
    children: createElement('div', { 'data-testid': 'timeline-content' }, child),
    ...extra,
  }))

  it('keeps healthy empty distinct from an all-source failure with retry', () => {
    try {
      setLocale('en')
      const healthy = normalizeTimelineHealth({
        entries: [],
        sourceOutcomes: healthyOutcomes,
        degraded: false,
        reason: null,
      })
      const failed = normalizeTimelineHealth({
        entries: [],
        sourceOutcomes: failedOutcomes,
        degraded: true,
        reason: 'all_sources_failed',
      })

      expect(healthy).toMatchObject({ kind: 'healthy', legacy: false })
      // `replay_unsupported` counts as unavailable, not failed: the source
      // answered that this session has no replayable transcript.
      expect(failed).toMatchObject({
        kind: 'failed',
        summary: { available: 0, unavailable: 3, failed: 1 },
      })

      const healthyHtml = renderBoundary(healthy, 0)
      const failedHtml = renderBoundary(failed, 0)
      expect(healthyHtml).toContain('healthy-empty-content')
      expect(healthyHtml).not.toContain('agent-sidecar-timeline-degraded')
      expect(failedHtml).toContain('role="alert"')
      expect(failedHtml).toContain('All usable timeline sources failed')
      expect(failedHtml).toContain('0 available · 3 unavailable · 1 failed')
      expect(failedHtml).toContain('agent-sidecar-timeline-retry')
      expect(failedHtml).toContain('Back to board')
      expect(failedHtml).not.toContain('healthy-empty-content')
    } finally {
      setLocale('zh')
    }
  })

  it('shows partial degradation in both locales without hiding entries or leaking details', () => {
    const health = normalizeTimelineHealth({
      entries: [{ seq: 9 }],
      sourceOutcomes: {
        ...partialOutcomes,
        debugPath: '/Users/private/project',
        rawError: 'session-secret-id',
      },
      degraded: true,
      reason: 'partial_source_failure',
    })
    expect(health).toMatchObject({
      kind: 'partial',
      summary: { available: 1, unavailable: 2, failed: 1 },
    })

    try {
      setLocale('en')
      const english = renderBoundary(health, 1, 'rendered-entry-9')
      expect(english).toContain('role="status"')
      expect(english).toContain('Some timeline sources are unavailable')
      expect(english).toContain('rendered-entry-9')
      expect(english).not.toContain('/Users/private/project')
      expect(english).not.toContain('session-secret-id')

      setLocale('zh')
      const chinese = renderBoundary(health, 1, 'rendered-entry-9')
      expect(chinese).toContain('部分时间线来源暂不可用')
      expect(chinese).toContain('1 个可用 · 2 个不可用 · 1 个失败')
      expect(chinese).toContain('rendered-entry-9')
    } finally {
      setLocale('zh')
    }
  })

  it('names the daemon as the cause when the supervisor is down', () => {
    const failed = normalizeTimelineHealth({
      entries: [],
      sourceOutcomes: failedOutcomes,
      degraded: true,
      reason: 'all_sources_failed',
    })

    // The generic "press refresh" line is useless while nothing is
    // listening; the actionable fact is that the daemon is not running.
    const down = renderBoundary(failed, 0, 'ignored', { daemonState: 'failed' })
    expect(down).toContain('agent-sidecar daemon start')
    expect(down).not.toContain('可点击「刷新」重试时间线来源')

    const deferred = renderBoundary(failed, 0, 'ignored', { daemonState: 'defer' })
    expect(deferred).toContain('服务拉起后')

    const running = renderBoundary(failed, 0, 'ignored', { daemonState: 'hosted' })
    expect(running).toContain('可点击「刷新」重试时间线来源')
    expect(running).not.toContain('agent-sidecar daemon start')
  })

  it('fails closed on an unknown outcome and keeps field-less legacy pages healthy', () => {
    const unknown = normalizeTimelineHealth({
      entries: [],
      sourceOutcomes: { ...healthyOutcomes, liveSession: 'future_private_state' },
      degraded: false,
      reason: null,
      sourceError: '/private/path/session-id',
    })
    const legacy = normalizeTimelineHealth({
      entries: [],
      nextCursor: null,
      sources: {},
    })

    expect(unknown).toEqual({ kind: 'unverified', legacy: false, summary: null })
    expect(legacy).toEqual({ kind: 'healthy', legacy: true, summary: null })
    const unknownHtml = renderBoundary(unknown, 0)
    expect(unknownHtml).toContain('role="alert"')
    expect(unknownHtml).not.toContain('healthy-empty-content')
    expect(unknownHtml).not.toContain('future_private_state')
    expect(unknownHtml).not.toContain('/private/path/session-id')
    expect(renderBoundary(legacy, 0)).toContain('healthy-empty-content')
  })

  function paginationPage(
    seqs: readonly number[],
    nextCursor: string | null,
    sourceOutcomes: typeof healthyOutcomes | typeof partialOutcomes,
    degraded: boolean,
    reason: null | 'partial_source_failure',
  ): TimelinePageWire {
    return {
      sessionId: 'session-claude',
      entries: seqs.map((seq) => ({
        origin: 'sidecar' as const,
        seq,
        ts: seq * 1_000,
        kind: 'assistant',
        text: `entry-${seq}`,
        extra: null,
      })),
      cursor: null,
      nextCursor,
      sources: {
        dshLive: false,
        dshCold: false,
        sidecarReplay: true,
        sidecarBuffer: false,
      },
      sourceOutcomes,
      degraded,
      reason,
    } as TimelinePageWire
  }

  it('preserves cursor pagination and dedupe while accepting page health', async () => {
    const session = eligibilitySession('claude', 'idle')
    const newest = paginationPage([2], 'OLDER', healthyOutcomes, false, null)
    const older = paginationPage(
      [1, 2],
      null,
      partialOutcomes,
      true,
      'partial_source_failure',
    )
    const cursors: Array<string | null | undefined> = []
    const wire: M3SessionDetailWire = { session, unified: null, timeline: newest }
    const store = new DetailStore(session.session_id, {
      fetchDetailFn: () => Promise.resolve(wire),
      fetchPageFn: (_sessionId, options) => {
        cursors.push(options?.cursor)
        return Promise.resolve(older)
      },
    })

    await store.open()
    expect(store.getState().timelineHealth.kind).toBe('healthy')
    await store.loadMore()

    const state = store.getState()
    expect(cursors).toEqual(['OLDER'])
    expect(state.timeline.entries.map((entry) => entry.seq)).toEqual([1, 2])
    expect(state.timeline.nextCursor).toBeNull()
    expect(state.timeline.reachedStart).toBe(true)
    expect(state.hasMore).toBe(false)
    expect(state.timelineHealth.kind).toBe('partial')
    expect(renderBoundary(
      state.timelineHealth,
      state.timeline.entries.length,
      'deduped-paginated-entries',
    )).toContain('deduped-paginated-entries')
    store.dispose()
  })
})

describe('board agent filtering and initial snapshot state', () => {
  const nowMs = 1_700_000_000_000
  const hourMs = 3_600_000
  const session = (
    sessionId: string,
    agent: string,
    status = 'waiting',
    updatedAtMs = nowMs,
  ): SessionCardVM => ({
    agent,
    sessionId,
    status,
    title: sessionId,
    project: '/tmp/project',
    updatedAtMs,
    lastEvent: null,
    gap: false,
  })
  const renderBoard = (
    overrides: Partial<Parameters<typeof Board>[0]> = {},
  ): string => renderToStaticMarkup(createElement(Board, {
    daemonState: 'hosted',
    streamHealth: 'ok',
    lastReconcileAtMs: nowMs,
    hasSnapshot: true,
    initialLoadFailed: false,
    sessions: [],
    filters: { timeWindowHours: 24, showDead: false },
    onFiltersChange: () => {},
    onRefresh: () => {},
    onSelectSession: () => {},
    returnFocusTarget: null,
    onReturnFocusConsumed: () => {},
    rootRef: () => {},
    onScrollTopChange: () => {},
    nowMs,
    ...overrides,
  }))

  it('AND-combines agent with the existing status/time/dead filters', () => {
    const sessions = [
      session('claude-match', 'claude'),
      session('wrong-agent', 'dsh'),
      session('old-claude', 'claude', 'idle', nowMs - 48 * hourMs),
      session('dead-claude', 'claude', 'dead'),
    ]
    const filters: BoardFilterState = {
      timeWindowHours: 24,
      showDead: false,
      statusFilter: 'waiting',
      agentFilter: 'claude',
    }

    expect(filterSessions(sessions, filters, nowMs).map((item) => item.sessionId))
      .toEqual(['claude-match'])
  })

  it('All agents clears only the agent condition', () => {
    const filters: BoardFilterState = {
      timeWindowHours: 12,
      showDead: true,
      statusFilter: 'working',
      agentFilter: 'codex',
    }

    expect(withAgentFilter(filters, '')).toEqual({
      timeWindowHours: 12,
      showDead: true,
      statusFilter: 'working',
    })
  })

  it('keeps stable recognizable options and never reflects unknown agent values', () => {
    const secretLikeAgent = 'private-agent-token-123'
    const sessions = [
      session('cursor', 'cursor-cli'),
      session('unknown', secretLikeAgent),
      session('dsh', 'DSH'),
      session('claude', 'claude'),
    ]

    expect(agentFilterOptions(sessions, 'codex')).toEqual([
      'dsh',
      'claude',
      'codex',
      'cursor-cli',
    ])
    expect(agentFilterOptions(sessions, secretLikeAgent)).toEqual([
      'dsh',
      'claude',
      'cursor-cli',
    ])
    expect(withAgentFilter(
      { timeWindowHours: 24, showDead: false, agentFilter: secretLikeAgent },
      secretLikeAgent,
    )).toEqual({ timeWindowHours: 24, showDead: false })

    try {
      setLocale('en')
      const html = renderBoard({
        sessions,
        filters: { timeWindowHours: 24, showDead: false, agentFilter: secretLikeAgent },
      })
      const select =
        html.match(/<select[^>]*data-testid="agent-sidecar-agent-filter"[\s\S]*?<\/select>/)
          ?.[0] ?? ''
      expect(select).toContain('All agents')
      expect(select).toContain('aria-label="Filter sessions by agent type"')
      expect(select).not.toContain(secretLikeAgent)
    } finally {
      setLocale('zh')
    }
  })

  it('localizes delayed initial loading and suppresses premature empty/error states', () => {
    try {
      setLocale('en')
      const english = renderBoard({
        daemonState: 'failed',
        hasSnapshot: false,
      })
      expect(english).toContain('aria-busy="true"')
      expect(english).toContain('role="status"')
      expect(english).toContain('Loading the first sidecar snapshot…')
      expect(english).not.toContain('sidecar is offline')
      expect(english).not.toContain('No observed sessions yet')

      setLocale('zh')
      const chinese = renderBoard({
        daemonState: 'failed',
        hasSnapshot: false,
      })
      expect(chinese).toContain('正在加载首个 sidecar 快照…')
      expect(chinese).not.toContain('sidecar 已离线')
      expect(chinese).not.toContain('暂无被观测的会话')
    } finally {
      setLocale('zh')
    }
  })

  it('ends initial loading on failure and exposes localized error plus retry', () => {
    try {
      setLocale('en')
      const english = renderBoard({
        hasSnapshot: true,
        initialLoadFailed: true,
        streamHealth: 'degraded',
      })
      expect(english).toContain('aria-busy="false"')
      expect(english).toContain('Refresh failed')
      expect(english).toContain('>Refresh</button>')
      expect(english).not.toContain('agent-sidecar-board-loading')

      setLocale('zh')
      const chinese = renderBoard({
        hasSnapshot: true,
        initialLoadFailed: true,
        streamHealth: 'degraded',
      })
      expect(chinese).toContain('手动刷新失败')
      expect(chinese).toContain('>刷新</button>')
      expect(chinese).not.toContain('正在加载首个 sidecar 快照')
    } finally {
      setLocale('zh')
    }
  })

  it('settles into data, empty, and error views without masking stale data', () => {
    try {
      setLocale('en')
      const data = renderBoard({
        hasSnapshot: true,
        sessions: [session('stale-card', 'claude', 'working')],
        streamHealth: 'degraded',
      })
      expect(data).toContain('aria-busy="false"')
      expect(data).toContain('stale-card')
      expect(data).toContain('data-tone="warn"')
      expect(data).not.toContain('agent-sidecar-board-loading')

      const empty = renderBoard({ hasSnapshot: true })
      expect(empty).toContain('No observed sessions yet')
      expect(empty).not.toContain('agent-sidecar-board-loading')

      const error = renderBoard({ daemonState: 'failed', hasSnapshot: true })
      expect(error).toContain('sidecar is offline')
      expect(error).not.toContain('agent-sidecar-board-loading')
    } finally {
      setLocale('zh')
    }
  })

  it('passes controller snapshot readiness through shared tab and overlay content', () => {
    expect(MOUNT_SOURCE).toContain('hasSnapshot={state.hasSnapshot}')
    expect(MOUNT_SOURCE).toContain('initialLoadFailed={state.initialLoadFailed}')
    expect(MOUNT_SOURCE.match(/<Board\s/g)).toHaveLength(1)
  })

  it('renders the idle-fold toggle and one expandable idle summary per project', () => {
    try {
      setLocale('en')
      const html = renderBoard({
        sessions: [
          session('idle-1', 'claude', 'idle'),
          session('idle-2', 'dsh', 'idle'),
          session('working', 'dsh', 'working'),
        ],
        filters: { timeWindowHours: 24, showDead: false, collapseIdle: true },
      })
      expect(html).toContain('data-testid="agent-sidecar-collapse-idle"')
      expect(html).toContain('Fold idle')
      expect(html).toContain('data-testid="agent-sidecar-idle-summary"')
      expect(html).toContain('2 idle sessions')
      expect(html).not.toContain('idle-1')
      expect(html).not.toContain('idle-2')
    } finally {
      setLocale('zh')
    }
  })

  it('adds the cross-agent and project analysis entry points with target kinds', () => {
    try {
      setLocale('en')
      const crossAgent = vi.fn()
      const project = vi.fn()
      const boardHtml = renderBoard({ onAnalyze: crossAgent })
      const projectHtml = renderToStaticMarkup(createElement(ProjectView, {
      groups: [{
        project: '/tmp/project',
        agents: ['claude'],
        sessions: [{
          agent: 'claude',
          sessionId: 'session-claude',
          status: 'idle',
          title: 'session',
          lastActivityAt: nowMs,
        }],
        lastActivityAt: nowMs,
      }],
      loading: false,
      error: null,
      onSelectSession: () => {},
      onAnalyzeProject: project,
      returnFocusTarget: null,
      onReturnFocusConsumed: () => {},
      rootRef: () => {},
      onScrollTopChange: () => {},
      nowMs,
      }))
      expect(boardHtml).toContain('Cross-agent analysis')
      expect(projectHtml).toContain('Analyze this project')
      // Static rendering cannot activate buttons; the callbacks remain typed
      // target seams, pinned by the source contract below.
      expect(PROJECT_VIEW_SOURCE).toContain("targetKind: 'project'")
      expect(BOARD_SOURCE).toContain("targetKind: 'cross-agent'")
      expect(crossAgent).not.toHaveBeenCalled()
      expect(project).not.toHaveBeenCalled()
    } finally {
      setLocale('zh')
    }
  })
})

describe('AnalysisPanel conversation rendering', () => {
  it('renders explicit user/assistant messages and a segmented pending assistant update', () => {
    const state: AnalysisGlueState = {
      phase: 'answering',
      analysisSessionId: 'agent-sidecar-analysis-test',
      exchanges: [{
        question: null,
        summary: 'first insight',
        truncated: false,
        tokensHint: null,
      }],
      messages: [
        { role: 'assistant', content: 'first insight' },
        { role: 'user', content: 'What should happen next?' },
        { role: 'assistant', content: '', pending: true },
      ],
      disclaimer: 'AI analysis is for reference only',
      errorCode: null,
      noticeCode: null,
      progressStep: 2,
    }
    try {
      setLocale('en')
      const html = renderToStaticMarkup(createElement(AnalysisPanel, {
        enabled: true,
        state,
        onStart: () => {},
        onFollowup: () => {},
        onStop: () => {},
        onClose: () => {},
      }))
      expect(html).toContain('data-role="user"')
      expect(html).toContain('data-role="assistant"')
      expect(html).toContain('segment 3')
      expect(html).toContain('data-pending="true"')
    } finally {
      setLocale('zh')
    }
  })
})

describe('full-page analysis navigation', () => {
  it('replaces every source route and returns to its exact source route', () => {
    expect(MOUNT_SOURCE).toContain("type MainView = SourceView | 'analysis'")
    expect(MOUNT_SOURCE).toContain(
      'if (analysisRoute !== null && boardAnalysisStore !== null)',
    )
    expect(MOUNT_SOURCE).toContain('setMainView(analysisRoute.source.view)')
    expect(MOUNT_SOURCE).toContain('setDetail(analysisRoute.source.detail)')
    expect(MOUNT_SOURCE).toContain('data-testid="agent-sidecar-analysis-back"')
    expect(MOUNT_SOURCE).toContain('onAnalyze={openAnalysis}')
    expect(DETAIL_VIEW_SOURCE).toContain(
      "props.onAnalyze({ targetKind: 'session', targetId: sessionId })",
    )
    expect(DETAIL_VIEW_SOURCE).not.toContain('<AnalysisPanel')
  })

  it('keeps one tab-scoped analysis store alive across route switches', () => {
    expect(MOUNT_SOURCE).toContain(
      '() => integration?.createAnalysisStore() ?? null',
    )
    expect(MOUNT_SOURCE).toContain('boardAnalysisStore?.dispose()')
    expect(MOUNT_SOURCE).not.toContain('integration.detail.createAnalysisStore()')
    expect(DETAIL_VIEW_SOURCE).not.toContain('createAnalysisStore')
  })

  it('gives the full-page panel an independent message scroller', () => {
    expect(ANALYSIS_CSS_SOURCE).toMatch(
      /\.panel\s*\{[\s\S]*?flex:\s*1;[\s\S]*?min-height:\s*0;[\s\S]*?overflow:\s*hidden;/,
    )
    expect(ANALYSIS_CSS_SOURCE).toMatch(
      /\.messages\s*\{[\s\S]*?flex:\s*1;[\s\S]*?min-height:\s*0;[\s\S]*?overflow:\s*auto;/,
    )
    expect(ANALYSIS_CSS_SOURCE).not.toMatch(/\.summary\s*\{/)
  })
})

describe('copy feedback timer lifecycle', () => {
  it.each([
    ['board session card', BOARD_SOURCE],
    ['session detail', DETAIL_SOURCE],
  ])('%s ignores late clipboard work and clears every timer', (_label, source) => {
    expect(source).toMatch(
      /useEffect\(\(\) => \{\s*copyAliveRef\.current = true\s*return \(\) => \{\s*copyAliveRef\.current = false\s*if \(copyTimerRef\.current !== null\) \{\s*clearTimeout\(copyTimerRef\.current\)\s*copyTimerRef\.current = null/,
    )
    expect(source).toContain('if (!copyAliveRef.current) return')
    // The detail header copies several values (id / project / transcript) so
    // its copied state names the field instead of a bare boolean; the timer
    // lifecycle being pinned here is identical either way.
    expect(source).toMatch(
      /if \(copyTimerRef\.current !== null\) clearTimeout\(copyTimerRef\.current\)\s*setCopied\((?:true|field)\)\s*copyTimerRef\.current = setTimeout\(\(\) => \{\s*copyTimerRef\.current = null\s*setCopied\((?:false|null)\)/,
    )
  })
})

describe('board/detail navigation focus and scroll', () => {
  const rawSessionId = 'session /项目?#alpha'
  const sharedSessionId = 'shared-session'

  it('matches return focus by strict agent plus session identity', () => {
    const target = { agent: 'claude', sessionId: sharedSessionId }
    expect(matchesSessionFocusTarget(
      { agent: 'claude', sessionId: sharedSessionId },
      target,
    )).toBe(true)
    expect(matchesSessionFocusTarget(
      { agent: 'codex', sessionId: sharedSessionId },
      target,
    )).toBe(false)
    expect(matchesSessionFocusTarget(
      { agent: 'claude', sessionId: 'another-session' },
      target,
    )).toBe(false)
  })

  it.each([
    ['negative', -50, 900, 300, 0],
    ['within range', 250, 900, 300, 250],
    ['past current range', 800, 900, 300, 600],
    ['non-scrollable', 120, 200, 300, 0],
  ])('clamps %s scroll offsets without viewport assumptions', (
    _label,
    requested,
    scrollHeight,
    clientHeight,
    expected,
  ) => {
    expect(clampScrollTop(requested, scrollHeight, clientHeight)).toBe(expected)
  })

  it('uses card refs for exact focus and heading refs when data disappeared', () => {
    for (const source of [BOARD_SOURCE, PROJECT_VIEW_SOURCE]) {
      expect(source).toContain('openerRef.current')
      expect(source).toContain('opener.focus({ preventScroll: true })')
      expect(source).toContain('fallbackFocusRef.current')
      expect(source).toContain('fallback.focus({ preventScroll: true })')
      expect(source).toContain('props.onReturnFocusConsumed()')
      expect(source).not.toContain('data-sidecar-session-opener')
    }
    expect(MOUNT_SOURCE).not.toContain('querySelector')
    expect(MOUNT_SOURCE).not.toContain('encodeSessionOpenerKey')
  })

  it('waits for two layout frames before restoring', () => {
    const frames = new FakeAnimationFrames()
    const restored = vi.fn()
    scheduleAfterLayout(frames, restored)

    expect(frames.pending).toBe(1)
    frames.flushOne()
    expect(restored).not.toHaveBeenCalled()
    expect(frames.pending).toBe(1)
    frames.flushOne()
    expect(restored).toHaveBeenCalledOnce()
  })

  it('cancels stale focus for rapid open/back/unmount sequences', () => {
    const frames = new FakeAnimationFrames()
    const staleRestore = vi.fn()
    const latestRestore = vi.fn()
    const cancelStale = scheduleAfterLayout(frames, staleRestore)

    frames.flushOne()
    cancelStale()
    cancelStale()
    const cancelLatest = scheduleAfterLayout(frames, latestRestore)
    frames.flushOne()
    frames.flushOne()
    cancelLatest()

    expect(staleRestore).not.toHaveBeenCalled()
    expect(latestRestore).toHaveBeenCalledOnce()
    expect(frames.pending).toBe(0)
    expect(frames.cancelled).toHaveLength(1)
  })

  it.each(['board', 'projects'] as const)(
    'A card → detail → internal B → Back clears A and uses the %s fallback',
    (view) => {
      const switchStart = MOUNT_SOURCE.indexOf('const switchDetailSession =')
      const closeStart = MOUNT_SOURCE.indexOf('const closeDetail =', switchStart)
      const switchBlock = MOUNT_SOURCE.slice(switchStart, closeStart)

      expect(switchStart).toBeGreaterThan(-1)
      expect(closeStart).toBeGreaterThan(switchStart)
      expect(switchBlock).toContain('view: current.returnRequest.view')
      expect(switchBlock).toContain('focusTarget: null')
      expect(switchBlock).not.toContain('current.returnRequest.focusTarget')
      expect(switchBlock).not.toContain('saveVisibleScroll')
      expect(switchBlock).not.toContain('setMainView')
      expect(switchBlock).not.toContain('setFilters')
      expect(MOUNT_SOURCE).toContain(
        `returnFocusRequest?.view === '${view}'`,
      )
      expect(MOUNT_SOURCE).toContain(
        'returnFocusRequest.focusTarget ?? FALLBACK_FOCUS_TARGET',
      )
      expect(MOUNT_SOURCE).toContain('root.scrollTop = clampScrollTop(')
    },
  )

  it.each(['mouse click', 'Enter', 'Space'])(
    'keeps board and project %s activation on native opener buttons',
    () => {
      const boardHtml = renderToStaticMarkup(createElement(Board, {
        daemonState: 'adopted',
        streamHealth: 'ok',
        lastReconcileAtMs: 1_700_000_000_000,
        hasSnapshot: true,
        initialLoadFailed: false,
        sessions: [{
          agent: 'dsh',
          sessionId: rawSessionId,
          status: 'working',
          title: 'Focused session',
          project: '/tmp/private-project',
          updatedAtMs: 1_700_000_000_000,
          lastEvent: null,
          gap: false,
        }],
        filters: { timeWindowHours: 24, showDead: false },
        onFiltersChange: () => {},
        onRefresh: () => {},
        onSelectSession: () => {},
        returnFocusTarget: null,
        onReturnFocusConsumed: () => {},
        rootRef: () => {},
        onScrollTopChange: () => {},
        nowMs: 1_700_000_000_000,
      }))
      const projectHtml = renderToStaticMarkup(createElement(ProjectView, {
        groups: [{
          project: '/tmp/private-project',
          agents: ['dsh'],
          sessions: [{
            agent: 'dsh',
            sessionId: rawSessionId,
            status: 'working',
            title: 'Focused session',
            lastActivityAt: 1_700_000_000_000,
            live: true,
          }],
          lastActivityAt: 1_700_000_000_000,
        }],
        loading: false,
        error: null,
        onSelectSession: () => {},
        returnFocusTarget: null,
        onReturnFocusConsumed: () => {},
        rootRef: () => {},
        onScrollTopChange: () => {},
        nowMs: 1_700_000_000_000,
      }))

      for (const html of [boardHtml, projectHtml]) {
        expect(html).toContain('<button type="button"')
        expect(html).not.toContain('data-sidecar-session-opener')
        expect(html).not.toContain(`id="${rawSessionId}"`)
        expect(html).not.toMatch(/data-[^=\s]+="session-[a-z0-9.]+"/)
      }
    },
  )

  it('keeps detail focus semantic and all pending callbacks cancellable', () => {
    expect(DETAIL_VIEW_SOURCE).toContain(
      '[data-testid="agent-sidecar-detail"] header button:not([disabled])',
    )
    expect(DETAIL_VIEW_SOURCE).toContain('window.cancelAnimationFrame(focusFrameRef.current)')
    expect(MOUNT_SOURCE).toContain(
      'const returnRequest: ReturnRequest = { view: source, focusTarget: target }',
    )
    expect(MOUNT_SOURCE).toContain('returnRequestRef.current = detail.returnRequest')
    expect(MOUNT_SOURCE).toContain('setReturnFocusRequest(request)')
    expect(MOUNT_SOURCE).toContain('return cancelPendingFocus')
    expect(MOUNT_SOURCE).toContain('returnRequestRef.current = null')
  })
})

describe('SettingsCard', () => {
  const renderOpenSettings = (
    values = configToValues(DEFAULT_CONFIG_VIEW),
    dirty = false,
  ): string => renderToStaticMarkup(createElement(SettingsCard, {
    values,
    onChange: () => {},
    onSave: () => {},
    onDiscard: () => {},
    writable: true,
    dirty,
    saving: false,
    defaultOpen: true,
  }))

  const namedButton = (html: string, label: string): string =>
    html.match(new RegExp(`<button[^>]*>${label}</button>`))?.[0] ?? ''

  it('exposes the themed settings surface contract on its root', () => {
    const html = renderToStaticMarkup(createElement(SettingsCard, {
      values: configToValues(DEFAULT_CONFIG_VIEW),
      onChange: () => {},
      onSave: () => {},
      onDiscard: () => {},
      writable: true,
      dirty: false,
      saving: false,
    }))

    expect(html).toContain('data-dsh-plugin="agent-sidecar"')
    expect(html).toContain('data-dsh-part="settings-card"')
    expect(html).toContain('aria-expanded="false"')
  })

  it('shows profile-owned runtime groups as honest read-only guidance', () => {
    try {
      setLocale('en')
      const html = renderOpenSettings()
      expect(html).toContain(
        'daemon lifecycle values come from the plugin row config in the profile cordis.patch',
      )
      expect(html).toContain(
        'sidecar command and runtime directory come from the plugin row config',
      )
      expect(html).toContain(
        'stream cadence values come from the plugin row config',
      )
      expect(html).toContain('edit the profile patch and restart DSH')
      expect(html).toContain('reload the plugin or restart DSH')
      expect(html).not.toContain('Management policy')
      expect(html).not.toContain('Executable command')
      expect(html).not.toContain('Active cadence (ms)')
      expect(html).not.toContain('Provide the skill in-process')
      expect(html.match(/take effect immediately after saving/g)).toHaveLength(3)
    } finally {
      setLocale('zh')
    }
  })

  it('renders bilingual analysis route controls through the live locale table', () => {
    try {
      setLocale('en')
      const english = renderOpenSettings()
      expect(english).toContain('Analysis provider')
      expect(english).toContain('Analysis model')
      expect(english).toContain('Current route: host default model')

      setLocale('zh')
      const chinese = renderOpenSettings()
      expect(chinese).toContain('分析 provider')
      expect(chinese).toContain('分析 model')
      expect(chinese).toContain('当前路由:宿主默认模型')
      expect(chinese).toContain('本节设置保存后即时生效')
    } finally {
      setLocale('zh')
    }
  })

  it('classifies blank, explicit, and partial routes using host-compatible trimming', () => {
    expect(normalizeAnalysisRouteField('  vertex-ai  ')).toBe('vertex-ai')
    expect(resolveAnalysisRoute('  ', '\t')).toEqual({
      kind: 'host-default',
      provider: '',
      model: '',
    })
    expect(resolveAnalysisRoute(' openai ', ' gpt-5 ')).toEqual({
      kind: 'explicit',
      provider: 'openai',
      model: 'gpt-5',
    })
    expect(resolveAnalysisRoute('openai', ' ')).toEqual({
      kind: 'partial',
      provider: 'openai',
      model: '',
    })
  })

  it('allows a complete explicit route and reports the selected pair', () => {
    try {
      setLocale('en')
      const values = {
        ...configToValues(DEFAULT_CONFIG_VIEW),
        analysisProvider: 'openai',
        analysisModel: 'gpt-5',
      }
      const html = renderOpenSettings(values, true)
      expect(html).toContain('value="openai"')
      expect(html).toContain('value="gpt-5"')
      expect(html).toContain('Current route: explicit openai / gpt-5.')
      expect(html).not.toContain('role="alert"')
      expect(namedButton(html, 'Save')).not.toContain('disabled')
    } finally {
      setLocale('zh')
    }
  })

  it('reports the daemon-reported archive policy, not the staged config', () => {
    const renderWithPolicy = (
      archivePolicy: { auto: boolean; autoAfterSeconds: number } | null,
      values = configToValues(DEFAULT_CONFIG_VIEW),
    ): string => renderToStaticMarkup(createElement(SettingsCard, {
      values,
      onChange: () => {},
      onSave: () => {},
      onDiscard: () => {},
      writable: true,
      dirty: false,
      saving: false,
      defaultOpen: true,
      archivePolicy,
    }))

    try {
      setLocale('en')
      // Staged "on" while the reached daemon still has it off: the readout
      // must follow the daemon, or the card claims sessions are being
      // archived when nothing is.
      const staged = { ...configToValues(DEFAULT_CONFIG_VIEW), archiveAuto: true }
      expect(renderWithPolicy({ auto: false, autoAfterSeconds: 86400 }, staged))
        .toContain('off')

      expect(renderWithPolicy({ auto: true, autoAfterSeconds: 86400 }))
        .toContain('threshold 24h')
      // A non-hour threshold is reported as it is, never rounded into a lie.
      expect(renderWithPolicy({ auto: true, autoAfterSeconds: 5400 }))
        .toContain('threshold 1.5h')
      expect(renderWithPolicy(null)).toContain('no daemon reached yet')

      // And the note must say whose daemon the toggle can actually reach.
      expect(renderWithPolicy(null)).toContain('daemons this plugin spawns')
    } finally {
      setLocale('zh')
    }
  })

  it('disables the threshold field until automatic archiving is on', () => {
    // A distinct value so the assertion cannot match the board time window,
    // whose default is also 24.
    const base = { ...configToValues(DEFAULT_CONFIG_VIEW), archiveAutoAfterHours: 48 }
    const thresholdField = (html: string): string =>
      html.match(/<input[^>]*value="48"[^>]*>/)?.[0] ?? ''

    expect(thresholdField(renderOpenSettings(base, true))).toContain('disabled')
    expect(thresholdField(renderOpenSettings({ ...base, archiveAuto: true }, true)))
      .not.toContain('disabled')
  })

  it('marks a partial route invalid and disables misleading saves', () => {
    try {
      setLocale('en')
      const values = {
        ...configToValues(DEFAULT_CONFIG_VIEW),
        analysisProvider: 'openai',
        analysisModel: '',
      }
      const html = renderOpenSettings(values, true)
      expect(html).toContain('Provider and model must either both be blank or both be set.')
      expect(html).toContain('role="alert"')
      expect(html).toContain('aria-invalid="true"')
      expect(namedButton(html, 'Save')).toContain('disabled=""')
    } finally {
      setLocale('zh')
    }
  })
})

describe('createDefaultIntegration', () => {
  it('accepts plain structural stores through the factory ports', () => {
    const unusedState = (): never => { throw new Error('state not read by this test') }
    const detailStore = {
      subscribe: () => () => {},
      getState: unusedState,
      open: () => Promise.resolve(),
      loadMore: () => Promise.resolve(),
      toggleListen: () => {},
      refreshNewest: () => Promise.resolve(),
      notifySnapshot: () => {},
      dispose: () => {},
    } satisfies DetailStorePort
    const searchStore = {
      subscribe: () => () => {},
      getState: unusedState,
      setQuery: () => {},
      submit: () => Promise.resolve(),
      dispose: () => {},
    } satisfies SearchStorePort
    const analysisStore = {
      subscribe: () => () => {},
      getState: unusedState,
      start: () => Promise.resolve(),
      followup: () => Promise.resolve(),
      stop: () => Promise.resolve(),
      dispose: () => {},
    } satisfies AnalysisStorePort
    const projectsStore = {
      subscribe: () => () => {},
      getState: unusedState,
      refresh: () => Promise.resolve(),
      notifySnapshot: () => {},
      dispose: () => {},
    } satisfies ProjectsStorePort
    const integration: BoardUiPort = {
      detail: {
        getAnalysisEnabled: () => true,
        createDetailStore: () => detailStore,
        createSearchStore: () => searchStore,
      },
      createProjectsStore: () => projectsStore,
      createAnalysisStore: () => analysisStore,
    }

    expect(integration.createProjectsStore()).toBe(projectsStore)
    expect(integration.detail.createDetailStore('sess-structural', null)).toBe(detailStore)
    expect(integration.detail.createSearchStore()).toBe(searchStore)
    expect(integration.createAnalysisStore()).toBe(analysisStore)
  })

  it('assembles narrow board and detail ports over the production stores', () => {
    let analysisEnabled = false
    const integration = createDefaultIntegration({
      getAnalysisEnabled: () => analysisEnabled,
    })
    const projects = integration.createProjectsStore()
    const detail = integration.detail.createDetailStore('sess-alpha', null)
    const search = integration.detail.createSearchStore()
    const analysis = integration.createAnalysisStore()

    expect(projects.getState()).toMatchObject({ groups: [], loading: false })
    expect(detail.getState()).toMatchObject({ sessionId: 'sess-alpha', ready: false })
    expect(search.getState()).toMatchObject({ query: '', items: [] })
    expect(analysis.getState()).toMatchObject({ phase: 'idle', exchanges: [] })
    expect(integration.detail.inject).toBeUndefined()
    expect(integration.detail.getAnalysisEnabled()).toBe(false)
    analysisEnabled = true
    expect(integration.detail.getAnalysisEnabled()).toBe(true)

    projects.dispose()
    detail.dispose()
    search.dispose()
    analysis.dispose()
  })
})

// ---------------------------------------------------------------------------
// Pure mapping.
// ---------------------------------------------------------------------------

describe('mapSessions', () => {
  it('converts updated_at epoch seconds to updatedAtMs milliseconds', () => {
    const cards = mapSessions(snapshotFixture().board.sessions)
    expect(cards[0]?.updatedAtMs).toBe(1_700_000_000_000)
    expect(cards[1]?.updatedAtMs).toBe(1_700_000_100_000)
  })

  it('maps wire field names and passes gap/lastEvent through', () => {
    const cards = mapSessions(snapshotFixture().board.sessions)
    expect(cards[0]).toMatchObject({
      agent: 'dsh',
      sessionId: 'sess-alpha',
      status: 'working',
      title: 'Fix the flux capacitor',
      project: '/home/u/proj',
      lastEvent: { kind: 'message', text: 'hello' },
      gap: false,
    })
    expect(cards[1]?.lastEvent).toBeNull()
    expect(cards[1]?.gap).toBe(true)
  })
})

describe('daemonDetailOf', () => {
  it('formats pid and version', () => {
    expect(daemonDetailOf({ pid: 4242, version: '0.6.0', http: { enabled: false } }))
      .toBe('pid 4242 · v0.6.0')
  })

  it('is undefined without a ping', () => {
    expect(daemonDetailOf(null)).toBeUndefined()
  })
})

describe('combineStreamHealth', () => {
  it('is unknown before the first snapshot regardless of browser status', () => {
    expect(combineStreamHealth(null, 'open', false)).toBe('unknown')
    expect(combineStreamHealth(null, 'degraded', false)).toBe('unknown')
  })

  it('lets a degraded browser stream override host-reported ok', () => {
    expect(combineStreamHealth('ok', 'degraded', true)).toBe('degraded')
  })

  it('keeps the host verdict while the browser is open or reconnecting', () => {
    expect(combineStreamHealth('ok', 'open', true)).toBe('ok')
    expect(combineStreamHealth('ok', 'connecting', true)).toBe('ok')
    expect(combineStreamHealth('degraded', 'open', true)).toBe('degraded')
    expect(combineStreamHealth('unknown', 'open', true)).toBe('unknown')
  })
})

describe('mapSnapshot', () => {
  it('folds the full snapshot into view state', () => {
    const state = mapSnapshot(snapshotFixture(), 'open')
    expect(state.daemonState).toBe('adopted')
    expect(state.daemonDetail).toBe('pid 4242 · v0.6.0')
    expect(state.streamHealth).toBe('ok')
    expect(state.lastReconcileAtMs).toBe(1_700_000_200_000)
    expect(state.sessions).toHaveLength(2)
    expect(state.injectCapability).toBe(false)
    expect(state.hasSnapshot).toBe(true)
    expect(state.initialLoadFailed).toBe(false)
  })

  it('initialViewState starts probing with unknown health and no sessions', () => {
    const state = initialViewState()
    expect(state.daemonState).toBe('probe')
    expect(state.streamHealth).toBe('unknown')
    expect(state.sessions).toEqual([])
    expect(state.hasSnapshot).toBe(false)
    expect(state.initialLoadFailed).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Filter persistence.
// ---------------------------------------------------------------------------

describe('readStoredFilters', () => {
  it('reads and canonicalizes a valid stored agent filter', () => {
    const storage = new FakeStorage()
    storage.setItem(FILTERS_STORAGE_KEY, JSON.stringify({
      timeWindowHours: 6,
      showDead: true,
      agentFilter: ' Claude ',
    }))
    expect(readStoredFilters(storage)).toEqual({
      timeWindowHours: 6,
      showDead: true,
      agentFilter: 'claude',
    })
  })

  it.each([
    ['absent', null],
    ['non-json', 'not json'],
    ['wrong shape', JSON.stringify({ timeWindowHours: 'six', showDead: true })],
    ['non-positive window', JSON.stringify({ timeWindowHours: 0, showDead: false })],
  ])('answers null for %s', (_label, raw) => {
    const storage = new FakeStorage()
    if (raw !== null) storage.setItem(FILTERS_STORAGE_KEY, raw)
    expect(readStoredFilters(storage)).toBeNull()
  })

  it('answers null without a storage', () => {
    expect(readStoredFilters(null)).toBeNull()
  })

  it('carries a legal statusFilter and drops an unrecognized one (UX-01)', () => {
    const storage = new FakeStorage()
    storage.setItem(
      FILTERS_STORAGE_KEY,
      JSON.stringify({ timeWindowHours: 6, showDead: false, statusFilter: 'waiting' }),
    )
    expect(readStoredFilters(storage)).toEqual({
      timeWindowHours: 6,
      showDead: false,
      statusFilter: 'waiting',
    })
    storage.setItem(
      FILTERS_STORAGE_KEY,
      JSON.stringify({ timeWindowHours: 6, showDead: false, statusFilter: 'dead' }),
    )
    // The record survives; only the illegal statusFilter token is dropped.
    expect(readStoredFilters(storage)).toEqual({ timeWindowHours: 6, showDead: false })
  })

  it('restores the persisted idle-fold toggle and drops malformed values', () => {
    const storage = new FakeStorage()
    storage.setItem(
      FILTERS_STORAGE_KEY,
      JSON.stringify({ timeWindowHours: 24, showDead: false, collapseIdle: true }),
    )
    expect(readStoredFilters(storage)).toEqual({
      timeWindowHours: 24,
      showDead: false,
      collapseIdle: true,
    })
    storage.setItem(
      FILTERS_STORAGE_KEY,
      JSON.stringify({ timeWindowHours: 24, showDead: false, collapseIdle: 'yes' }),
    )
    expect(readStoredFilters(storage)).toEqual({ timeWindowHours: 24, showDead: false })
  })

  it.each([
    ['unknown string', 'private-agent-token'],
    ['empty string', '   '],
    ['non-string', 42],
  ])('normalizes %s agentFilter safely to All agents', (_label, agentFilter) => {
    const storage = new FakeStorage()
    storage.setItem(FILTERS_STORAGE_KEY, JSON.stringify({
      timeWindowHours: 24,
      showDead: false,
      statusFilter: 'working',
      agentFilter,
    }))
    expect(readStoredFilters(storage)).toEqual({
      timeWindowHours: 24,
      showDead: false,
      statusFilter: 'working',
    })
  })

  it('keeps legacy and version-annotated filter records compatible', () => {
    const storage = new FakeStorage()
    storage.setItem(FILTERS_STORAGE_KEY, JSON.stringify({
      schemaVersion: 99,
      timeWindowHours: 12,
      showDead: true,
      agentFilter: 'codex',
    }))
    expect(readStoredFilters(storage)).toEqual({
      timeWindowHours: 12,
      showDead: true,
      agentFilter: 'codex',
    })
  })
})

// ---------------------------------------------------------------------------
// Controller behavior (fake stream + fake storage).
// ---------------------------------------------------------------------------

describe('SidecarController', () => {
  function build(storage: StorageLike | null = new FakeStorage()): {
    controller: SidecarController
    stream: FakeStream
  } {
    const stream = new FakeStream()
    const controller = new SidecarController({ stream, storage })
    return { controller, stream }
  }

  it('folds snapshots into stable-reference view state and notifies', () => {
    const { controller, stream } = build()
    let notified = 0
    controller.subscribe(() => {
      notified += 1
    })
    controller.start()
    const before = controller.getState()
    stream.emitStatus('open')
    stream.emitSnapshot(snapshotFixture())
    const after = controller.getState()
    expect(after).not.toBe(before)
    expect(after.sessions[0]?.updatedAtMs).toBe(1_700_000_000_000)
    expect(after.streamHealth).toBe('ok')
    expect(notified).toBeGreaterThanOrEqual(2)
    expect(controller.getState()).toBe(after)
  })

  it('recombines stream health when the browser status degrades', () => {
    const { controller, stream } = build()
    controller.start()
    stream.emitStatus('open')
    stream.emitSnapshot(snapshotFixture())
    stream.emitStatus('degraded')
    expect(controller.getState().streamHealth).toBe('degraded')
    stream.emitStatus('open')
    expect(controller.getState().streamHealth).toBe('ok')
  })

  it('persists user filters under the package-prefixed key', () => {
    const storage = new FakeStorage()
    const { controller } = build(storage)
    controller.setFilters({ timeWindowHours: 12, showDead: true })
    expect(JSON.parse(storage.map.get(FILTERS_STORAGE_KEY) ?? '')).toEqual({
      timeWindowHours: 12,
      showDead: true,
    })
    expect(controller.getFilters()).toEqual({ timeWindowHours: 12, showDead: true })
  })

  it('seeds filters from storage at construction', () => {
    const storage = new FakeStorage()
    storage.setItem(FILTERS_STORAGE_KEY, JSON.stringify({ timeWindowHours: 6, showDead: true }))
    const { controller } = build(storage)
    expect(controller.getFilters()).toEqual({ timeWindowHours: 6, showDead: true })
  })

  it('restores agentFilter after refresh with every other filter intact', () => {
    const storage = new FakeStorage()
    const first = build(storage).controller
    first.setFilters({
      timeWindowHours: 12,
      showDead: true,
      statusFilter: 'waiting',
      agentFilter: 'claude',
    })

    const restored = build(storage).controller
    expect(restored.getFilters()).toEqual({
      timeWindowHours: 12,
      showDead: true,
      statusFilter: 'waiting',
      agentFilter: 'claude',
    })
  })

  it('adopts settings defaults only while filters are untouched', () => {
    const { controller } = build()
    controller.adoptConfigDefaults({ timeWindowHours: 24, showDead: true })
    expect(controller.getFilters()).toEqual({ timeWindowHours: 24, showDead: true })
    controller.setFilters({ timeWindowHours: 12, showDead: false })
    controller.adoptConfigDefaults({ timeWindowHours: 168, showDead: true })
    expect(controller.getFilters()).toEqual({ timeWindowHours: 12, showDead: false })
  })

  it('ignores settings defaults when filters were restored from storage', () => {
    const storage = new FakeStorage()
    storage.setItem(FILTERS_STORAGE_KEY, JSON.stringify({ timeWindowHours: 6, showDead: false }))
    const { controller } = build(storage)
    controller.adoptConfigDefaults({ timeWindowHours: 24, showDead: true })
    expect(controller.getFilters()).toEqual({ timeWindowHours: 6, showDead: false })
  })

  it('refresh() pulls one snapshot through the injected fetch and resolves true', async () => {
    const stream = new FakeStream()
    const controller = new SidecarController({
      stream,
      storage: null,
      fetchStateFn: () => Promise.resolve(snapshotFixture()),
    })
    controller.start()
    await expect(controller.refresh()).resolves.toBe(true)
    expect(controller.getState().daemonState).toBe('adopted')
    expect(controller.getState().hasSnapshot).toBe(true)
  })

  it('refresh() resolves false (never rejects) when the pull fails (UX-07)', async () => {
    const stream = new FakeStream()
    const controller = new SidecarController({
      stream,
      storage: null,
      fetchStateFn: () => Promise.reject(new Error('boom')),
    })
    controller.start()
    await expect(controller.refresh()).resolves.toBe(false)
    expect(controller.getState()).toMatchObject({
      hasSnapshot: true,
      initialLoadFailed: true,
      streamHealth: 'degraded',
    })
  })

  it('ignores an old eligible refresh after a newer disabled SSE snapshot', async () => {
    const stream = new FakeStream()
    let resolveOld!: (snapshot: StateSnapshot) => void
    const oldResponse = new Promise<StateSnapshot>((resolve) => {
      resolveOld = resolve
    })
    const controller = new SidecarController({
      stream,
      storage: null,
      fetchStateFn: () => oldResponse,
    })
    controller.start()

    const oldRefresh = controller.refresh()
    const disabled = snapshotFixture()
    disabled.capabilities.inject = false
    disabled.board.sessions = [
      eligibilitySession('claude', 'working', {
        allowed: false,
        reason: 'working_session',
      }),
    ]
    stream.emitSnapshot(disabled)
    const disabledState = controller.getState()

    const eligible = snapshotFixture()
    eligible.capabilities.inject = true
    eligible.board.sessions = [
      eligibilitySession('claude', 'waiting', {
        allowed: true,
        reason: 'eligible',
      }),
    ]
    resolveOld(eligible)

    await expect(oldRefresh).resolves.toBe(false)
    expect(controller.getState()).toBe(disabledState)
    expect(controller.getState()).toMatchObject({
      injectCapability: false,
      hasSnapshot: true,
      initialLoadFailed: false,
      sessions: [{ status: 'working' }],
    })
  })

  it('ignores an old refresh rejection after a newer manual retry succeeds', async () => {
    const stream = new FakeStream()
    let rejectOld!: (reason: unknown) => void
    let resolveNew!: (snapshot: StateSnapshot) => void
    const oldResponse = new Promise<StateSnapshot>((_resolve, reject) => {
      rejectOld = reject
    })
    const newResponse = new Promise<StateSnapshot>((resolve) => {
      resolveNew = resolve
    })
    const controller = new SidecarController({
      stream,
      storage: null,
      fetchStateFn: vi.fn()
        .mockReturnValueOnce(oldResponse)
        .mockReturnValueOnce(newResponse),
    })
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    controller.start()

    try {
      const oldRefresh = controller.refresh()
      const newRefresh = controller.refresh()
      const success = snapshotFixture()
      success.capabilities.inject = true
      resolveNew(success)
      await expect(newRefresh).resolves.toBe(true)
      const successState = controller.getState()
      rejectOld(new Error('stale failure'))

      await expect(oldRefresh).resolves.toBe(false)
      expect(controller.getState()).toBe(successState)
      expect(controller.getState()).toMatchObject({
        injectCapability: true,
        hasSnapshot: true,
        initialLoadFailed: false,
      })
      expect(errorSpy).not.toHaveBeenCalled()
    } finally {
      errorSpy.mockRestore()
    }
  })

  it('does not commit pending refreshes or stream callbacks after stop', async () => {
    const stream = new FakeStream()
    let resolvePending!: (snapshot: StateSnapshot) => void
    const pendingResponse = new Promise<StateSnapshot>((resolve) => {
      resolvePending = resolve
    })
    const controller = new SidecarController({
      stream,
      storage: null,
      fetchStateFn: () => pendingResponse,
    })
    const notified = vi.fn()
    controller.subscribe(notified)
    controller.start()

    const pendingRefresh = controller.refresh()
    const stateAtDispose = controller.getState()
    controller.stop()
    stream.emitStatus('degraded')
    stream.emitSnapshot(snapshotFixture())
    resolvePending(snapshotFixture())

    await expect(pendingRefresh).resolves.toBe(false)
    expect(controller.getState()).toBe(stateAtDispose)
    expect(notified).not.toHaveBeenCalled()
  })

  it('keeps connecting pending, settles first degraded SSE, then accepts retry success', async () => {
    const stream = new FakeStream()
    const fetchStateFn = vi.fn()
      .mockResolvedValue(snapshotFixture())
    const controller = new SidecarController({
      stream,
      storage: null,
      fetchStateFn,
    })
    controller.start()

    expect(controller.getState()).toMatchObject({
      streamStatus: 'connecting',
      hasSnapshot: false,
      initialLoadFailed: false,
    })
    stream.emitStatus('degraded')
    expect(controller.getState()).toMatchObject({
      streamStatus: 'degraded',
      streamHealth: 'degraded',
      hasSnapshot: true,
      initialLoadFailed: true,
    })

    await expect(controller.refresh()).resolves.toBe(true)
    expect(controller.getState()).toMatchObject({
      hasSnapshot: true,
      initialLoadFailed: false,
      sessions: expect.any(Array),
    })
  })

  it('settles a first state failure and clears it after a successful retry', async () => {
    const stream = new FakeStream()
    const fetchStateFn = vi.fn()
      .mockRejectedValueOnce(new Error('first failure'))
      .mockResolvedValueOnce(snapshotFixture())
    const controller = new SidecarController({
      stream,
      storage: null,
      fetchStateFn,
    })
    controller.start()

    await expect(controller.refresh()).resolves.toBe(false)
    expect(controller.getState()).toMatchObject({
      hasSnapshot: true,
      initialLoadFailed: true,
      streamHealth: 'degraded',
    })

    await expect(controller.refresh()).resolves.toBe(true)
    expect(controller.getState()).toMatchObject({
      hasSnapshot: true,
      initialLoadFailed: false,
      daemonState: 'adopted',
    })
  })

  it('does not mask stale data when a later refresh fails', async () => {
    const stream = new FakeStream()
    const controller = new SidecarController({
      stream,
      storage: null,
      fetchStateFn: () => Promise.reject(new Error('later failure')),
    })
    controller.start()
    stream.emitSnapshot(snapshotFixture())
    const staleSessions = controller.getState().sessions

    await expect(controller.refresh()).resolves.toBe(false)
    expect(controller.getState().sessions).toBe(staleSessions)
    expect(controller.getState()).toMatchObject({
      hasSnapshot: true,
      initialLoadFailed: false,
    })
  })

  it('persists the statusFilter through setFilters (UX-01)', () => {
    const storage = new FakeStorage()
    const { controller } = build(storage)
    controller.setFilters({ timeWindowHours: 12, showDead: false, statusFilter: 'working' })
    expect(JSON.parse(storage.map.get(FILTERS_STORAGE_KEY) ?? '')).toEqual({
      timeWindowHours: 12,
      showDead: false,
      statusFilter: 'working',
    })
    expect(controller.getFilters().statusFilter).toBe('working')
  })

  it('persists the idle-fold toggle through the existing safe storage seam', () => {
    const storage = new FakeStorage()
    const { controller } = build(storage)
    controller.setFilters({ timeWindowHours: 24, showDead: false, collapseIdle: true })
    expect(JSON.parse(storage.map.get(FILTERS_STORAGE_KEY) ?? '')).toEqual({
      timeWindowHours: 24,
      showDead: false,
      collapseIdle: true,
    })
    controller.setFilters({ timeWindowHours: 24, showDead: false, collapseIdle: false })
    expect(JSON.parse(storage.map.get(FILTERS_STORAGE_KEY) ?? '')).toEqual({
      timeWindowHours: 24,
      showDead: false,
    })
  })

  it('start() is idempotent and pollNow()/stop() forward to the stream', () => {
    const { controller, stream } = build()
    controller.start()
    controller.start()
    expect(stream.started).toBe(1)
    controller.pollNow()
    expect(stream.polled).toBe(1)
    controller.stop()
    expect(stream.stopped).toBe(1)
  })
})

// ---------------------------------------------------------------------------
// Settings glue.
// ---------------------------------------------------------------------------

describe('settings-glue', () => {
  it('configToValues ↔ valuesToConfigView round-trips the defaults', () => {
    const values = configToValues(DEFAULT_CONFIG_VIEW)
    expect(valuesToConfigView(values)).toEqual(DEFAULT_CONFIG_VIEW)
  })

  it('loads and round-trips an existing explicit analysis route without wiping it', () => {
    const configured = {
      ...DEFAULT_CONFIG_VIEW,
      analysis: {
        enabled: false,
        provider: 'openai',
        model: 'gpt-5',
      },
    }
    const values = configToValues(configured)
    expect(values.analysisProvider).toBe('openai')
    expect(values.analysisModel).toBe('gpt-5')
    expect(valuesToConfigView(values)).toEqual(configured)

    const patches = diffGroups(values, { ...values, analysisEnabled: true })
    expect(patches).toEqual([{
      group: 'analysis',
      patch: { enabled: true, provider: 'openai', model: 'gpt-5' },
    }])
  })

  it('joins and splits the sidecar command line', () => {
    expect(joinCommand(['uv', 'run', 'agent-sidecar'])).toBe('uv run agent-sidecar')
    expect(splitCommand('  uv   run  agent-sidecar ')).toEqual(['uv', 'run', 'agent-sidecar'])
    expect(splitCommand('')).toEqual([])
  })

  it('cardValuesEqual detects any field difference', () => {
    const a = configToValues(DEFAULT_CONFIG_VIEW)
    expect(cardValuesEqual(a, { ...a })).toBe(true)
    expect(cardValuesEqual(a, { ...a, uiShowDead: !a.uiShowDead })).toBe(false)
  })

  it('diffGroups emits one complete group object per changed group only', () => {
    const current = configToValues(DEFAULT_CONFIG_VIEW)
    const target = {
      ...current,
      daemonPolicy: 'adopt-only' as const,
      uiTimeWindowHours: 72,
    }
    const patches = diffGroups(current, target)
    expect(patches.map((p) => p.group)).toEqual(['daemon', 'ui'])
    expect(patches[0]?.patch).toEqual({
      policy: 'adopt-only',
      backoffLimit: DEFAULT_CONFIG_VIEW.daemon.backoffLimit,
    })
    expect(patches[1]?.patch).toEqual({ timeWindowHours: 72, showDead: false })
  })

  it('diffGroups answers empty for identical values', () => {
    const current = configToValues(DEFAULT_CONFIG_VIEW)
    expect(diffGroups(current, { ...current })).toEqual([])
  })

  it('DEFAULT_CONFIG_VIEW mirrors the host schema defaults', async () => {
    // The host Config schema is importable from the node-side test program;
    // resolving {} through it yields the schema defaults.
    const { Config } = await import('../src/config.ts')
    const resolved = Config({}) as unknown as typeof DEFAULT_CONFIG_VIEW
    expect(JSON.parse(JSON.stringify(resolved))).toEqual(DEFAULT_CONFIG_VIEW)
  })
})
