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
import type { StateSnapshot } from '../src/client/api.ts'
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
import { SettingsCard } from '../src/client/settings-card.tsx'
import {
  createDefaultIntegration,
  type AnalysisStorePort,
  type BoardUiPort,
  type DetailStorePort,
  type ProjectsStorePort,
  type SearchStorePort,
} from '../src/client/ui-integration.ts'

vi.mock('@deepseek-ai/dsh-client-ui-primitives', () => ({
  Button: 'button',
  IconChevronDownOutline14: 'svg',
  Input: 'input',
  Pill: 'span',
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

describe('copy feedback timer lifecycle', () => {
  it.each([
    ['board session card', BOARD_SOURCE],
    ['session detail', DETAIL_SOURCE],
  ])('%s ignores late clipboard work and clears every timer', (_label, source) => {
    expect(source).toMatch(
      /useEffect\(\(\) => \{\s*copyAliveRef\.current = true\s*return \(\) => \{\s*copyAliveRef\.current = false\s*if \(copyTimerRef\.current !== null\) \{\s*clearTimeout\(copyTimerRef\.current\)\s*copyTimerRef\.current = null/,
    )
    expect(source).toContain('if (!copyAliveRef.current) return')
    expect(source).toMatch(
      /if \(copyTimerRef\.current !== null\) clearTimeout\(copyTimerRef\.current\)\s*setCopied\(true\)\s*copyTimerRef\.current = setTimeout\(\(\) => \{\s*copyTimerRef\.current = null\s*setCopied\(false\)/,
    )
  })
})

describe('SettingsCard', () => {
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
        createAnalysisStore: () => analysisStore,
      },
      createProjectsStore: () => projectsStore,
    }

    expect(integration.createProjectsStore()).toBe(projectsStore)
    expect(integration.detail.createDetailStore('sess-structural', null)).toBe(detailStore)
    expect(integration.detail.createSearchStore()).toBe(searchStore)
    expect(integration.detail.createAnalysisStore()).toBe(analysisStore)
  })

  it('assembles narrow board and detail ports over the production stores', () => {
    let analysisEnabled = false
    const integration = createDefaultIntegration({
      getAnalysisEnabled: () => analysisEnabled,
    })
    const projects = integration.createProjectsStore()
    const detail = integration.detail.createDetailStore('sess-alpha', null)
    const search = integration.detail.createSearchStore()
    const analysis = integration.detail.createAnalysisStore()

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
  })

  it('initialViewState starts probing with unknown health and no sessions', () => {
    const state = initialViewState()
    expect(state.daemonState).toBe('probe')
    expect(state.streamHealth).toBe('unknown')
    expect(state.sessions).toEqual([])
    expect(state.hasSnapshot).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Filter persistence.
// ---------------------------------------------------------------------------

describe('readStoredFilters', () => {
  it('reads a valid stored value', () => {
    const storage = new FakeStorage()
    storage.setItem(FILTERS_STORAGE_KEY, JSON.stringify({ timeWindowHours: 6, showDead: true }))
    expect(readStoredFilters(storage)).toEqual({ timeWindowHours: 6, showDead: true })
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
