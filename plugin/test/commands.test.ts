/**
 * `/sidecar` slash command (T4.6): the pure snapshot→overview derivation
 * (`buildOverview`), its popupSelect rendering (`overviewToOptions`), the
 * command-segment locale table, and the contribution/registration seam
 * against a fake `commandUi` registry, plus the shared Center navigation
 * store and overlay surface.
 */

import { createElement } from 'react'
import type { ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/client/api.ts'
import type { SessionView, StateSnapshot } from '../src/client/api.ts'
import {
  DEFAULT_OVERVIEW_TOP_N,
  SIDECAR_COMMAND_NAME,
  buildOverview,
  createSidecarCommandContribution,
  overviewToOptions,
  registerSidecarCommand,
} from '../src/client/commands.ts'
import type {
  CommandMountContext,
  SidecarCommandContribution,
  SidecarSelectOption,
} from '../src/client/commands.ts'
import { commandEn, commandZh } from '../src/client/locales/command.ts'
import { BASE_LOCALE, setLocale } from '../src/client/locales/index.ts'
import { CenterOverlay } from '../src/client/navigation/CenterOverlay.tsx'
import { createCenterNavigation } from '../src/client/navigation/center.ts'

vi.mock('@deepseek-ai/dsh-client-ui-primitives', async () => {
  const React = await import('react')
  return {
    Modal: (props: {
      open: boolean
      title: string
      closeLabel?: string
      className?: string
      contentClassName?: string
      children?: ReactNode
    }) => props.open
      ? React.createElement(
          'section',
          {
            className: props.className,
            role: 'dialog',
            'aria-modal': 'true',
            'aria-label': props.title,
            'data-close-label': props.closeLabel,
          },
          React.createElement('div', { className: props.contentClassName }, props.children),
        )
      : null,
  }
})

afterEach(() => {
  setLocale(BASE_LOCALE)
  vi.useRealTimers()
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

/** Fixed clock (epoch ms) so every relative time is deterministic. */
const NOW = 1_756_000_000_000

/** Snapshot timestamps are epoch SECONDS (the wire contract). */
function secondsAgo(ms: number): number {
  return (NOW - ms) / 1000
}

let sessionSeq = 0

function makeSession(overrides: Partial<SessionView> = {}): SessionView {
  sessionSeq += 1
  return {
    agent: 'claude',
    session_id: `session-${String(sessionSeq).padStart(4, '0')}`,
    status: 'working',
    title: `任务 ${sessionSeq}`,
    project: '/Users/me/proj-a',
    updated_at: secondsAgo(30_000),
    last_event: { ts: '2026-08-24T10:00:00Z', kind: 'tool_call', text: 'run tests' },
    gap: false,
    ...overrides,
  }
}

function makeSnapshot(
  sessions: SessionView[],
  overrides: Partial<StateSnapshot> = {},
): StateSnapshot {
  return {
    daemon: {
      state: 'hosted',
      lastPing: { pid: 4242, version: '1.2.3', http: { enabled: false } },
    },
    board: { sessions, streamHealth: 'ok', lastReconcileAt: NOW - 5_000 },
    capabilities: { inject: false },
    ...overrides,
  }
}

function build(snapshot: StateSnapshot | null, topN?: number) {
  return buildOverview(snapshot, { nowMs: NOW, ...(topN !== undefined ? { topN } : {}) })
}

function optionIds(options: readonly SidecarSelectOption[]): string[] {
  return options.map((o) => o.id)
}

// ---------------------------------------------------------------------------
// buildOverview — ready snapshot.
// ---------------------------------------------------------------------------

describe('buildOverview on a healthy snapshot', () => {
  it('derives daemon label, connection health and the board hint', () => {
    const model = build(makeSnapshot([makeSession()]))
    expect(model.reachable).toBe(true)
    expect(model.daemonState).toBe('hosted')
    expect(model.daemonLabel).toBe(commandZh['command.daemon.hosted'])
    expect(model.connection).toBe('ok')
    expect(model.connectionLabel).toBe(commandZh['command.connection.ok'])
    expect(model.guidance).toBeNull()
    expect(model.boardHint).toBe(commandZh['command.boardHint'])
  })

  it('adopted daemon with a degraded stream is a degraded connection', () => {
    const snapshot = makeSnapshot([], {
      daemon: { state: 'adopted', lastPing: null },
    })
    snapshot.board.streamHealth = 'degraded'
    const model = build(snapshot)
    expect(model.connection).toBe('degraded')
    expect(model.connectionLabel).toBe(commandZh['command.connection.degraded'])
    expect(model.guidance).toBeNull()
  })

  it('counts working (status normalization applied) and waiting sessions', () => {
    const model = build(
      makeSnapshot([
        makeSession({ status: 'working' }),
        makeSession({ status: ' WORKING ' }),
        makeSession({ status: 'waiting' }),
        makeSession({ status: 'idle' }),
        makeSession({ status: 'dead' }),
      ]),
    )
    expect(model.workingCount).toBe(2)
    expect(model.waitingCount).toBe(1)
    expect(model.totalCount).toBe(5)
  })

  it('derives render-ready session rows', () => {
    const longId = 'claude-session-0001-abcdefghijkl'
    const model = build(
      makeSnapshot([
        makeSession({
          session_id: longId,
          title: '  ',
          status: 'waiting',
          updated_at: secondsAgo(5 * 60_000),
        }),
      ]),
    )
    const rows = model.groups.flatMap((g) => g.rows)
    expect(rows).toHaveLength(1)
    const row = rows[0]!
    expect(row.glyph).toBe('✳')
    expect(row.sessionId).toBe(longId)
    expect(row.shortId).toBe(`${longId.slice(0, 12)}…${longId.slice(-6)}`)
    expect(row.status).toBe('waiting')
    expect(row.statusLabel).toBe(commandZh['command.status.waiting'])
    expect(row.title).toBe(commandZh['command.untitled'])
    expect(row.relativeTime).toBe('5 分钟前')
  })

  it('renders just-now for fresh activity', () => {
    const model = build(makeSnapshot([makeSession({ updated_at: secondsAgo(10_000) })]))
    expect(model.groups[0]!.rows[0]!.relativeTime).toBe(commandZh['command.time.justNow'])
  })
})

// ---------------------------------------------------------------------------
// buildOverview — empty board.
// ---------------------------------------------------------------------------

describe('buildOverview on an empty board', () => {
  it('yields zero counts, no groups and no guidance', () => {
    const model = build(makeSnapshot([]))
    expect(model.reachable).toBe(true)
    expect(model.workingCount).toBe(0)
    expect(model.waitingCount).toBe(0)
    expect(model.totalCount).toBe(0)
    expect(model.groups).toEqual([])
    expect(model.truncatedCount).toBe(0)
    expect(model.guidance).toBeNull()
  })

  it('renders the empty notice instead of a counts row', () => {
    const options = overviewToOptions(build(makeSnapshot([])))
    expect(optionIds(options)).toEqual(['daemon', 'empty', 'board'])
    expect(options[1]!.label).toBe(commandZh['command.noSessions'])
  })
})

// ---------------------------------------------------------------------------
// buildOverview — offline / unreachable.
// ---------------------------------------------------------------------------

describe('buildOverview offline states', () => {
  it('daemon failed: off connection, failure guidance, last snapshot kept', () => {
    const model = build(
      makeSnapshot([makeSession()], { daemon: { state: 'failed', lastPing: null } }),
    )
    expect(model.connection).toBe('off')
    expect(model.daemonLabel).toBe(commandZh['command.daemon.failed'])
    expect(model.guidance).toMatchObject({
      kind: 'daemon-failed',
      title: commandZh['command.offlineFailed'],
      hint: commandZh['command.offlineFailedHint'],
    })
    // Honesty posture: the last snapshot's sessions stay visible.
    expect(model.totalCount).toBe(1)
    expect(model.groups.flatMap((g) => g.rows)).toHaveLength(1)
  })

  it('daemon defer: degraded connection and the defer guidance', () => {
    const model = build(makeSnapshot([], { daemon: { state: 'defer', lastPing: null } }))
    expect(model.connection).toBe('degraded')
    expect(model.guidance).toMatchObject({ kind: 'daemon-defer' })
  })

  it('null snapshot: unreachable guidance and a fully degraded model', () => {
    const model = build(null)
    expect(model.reachable).toBe(false)
    expect(model.daemonState).toBeNull()
    expect(model.daemonLabel).toBe(commandZh['command.daemon.unknown'])
    expect(model.connection).toBe('off')
    expect(model.workingCount).toBe(0)
    expect(model.totalCount).toBe(0)
    expect(model.groups).toEqual([])
    expect(model.guidance).toMatchObject({
      kind: 'unreachable',
      title: commandZh['command.unreachable'],
    })
  })

  it('null snapshot renders guidance without counts or session rows', () => {
    const options = overviewToOptions(build(null))
    expect(optionIds(options)).toEqual(['daemon', 'guidance:unreachable', 'board'])
    expect(options[1]!.detail).toBe(commandZh['command.unreachableHint'])
  })
})

// ---------------------------------------------------------------------------
// buildOverview — project grouping.
// ---------------------------------------------------------------------------

describe('project grouping', () => {
  it('groups by project, newest group first, unknown bucket last', () => {
    const model = build(
      makeSnapshot([
        makeSession({ project: '/repo/proj-a', updated_at: secondsAgo(60_000) }),
        makeSession({ project: '/repo/proj-a', status: 'waiting', updated_at: secondsAgo(30_000) }),
        makeSession({ project: '/repo/proj-b', status: 'idle', updated_at: secondsAgo(10_000) }),
        makeSession({ project: '  ', updated_at: secondsAgo(5_000) }),
      ]),
    )
    expect(model.groups.map((g) => g.label)).toEqual([
      'proj-b',
      'proj-a',
      commandZh['command.unknownProject'],
    ])
    expect(model.groups[2]!.key).toBe('')
    expect(model.groups[1]!.fullPath).toBe('/repo/proj-a')
  })

  it('orders cards inside a group by status rank before recency', () => {
    const model = build(
      makeSnapshot([
        makeSession({ project: '/p', status: 'waiting', updated_at: secondsAgo(1_000), title: '新等待' }),
        makeSession({ project: '/p', status: 'working', updated_at: secondsAgo(90_000), title: '旧工作' }),
        makeSession({ project: '/p', status: 'idle', updated_at: secondsAgo(2_000), title: '新空闲' }),
      ]),
    )
    expect(model.groups[0]!.rows.map((r) => r.title)).toEqual(['旧工作', '新等待', '新空闲'])
  })
})

// ---------------------------------------------------------------------------
// buildOverview — top-N truncation.
// ---------------------------------------------------------------------------

describe('top-N truncation', () => {
  function crowdedSnapshot(): StateSnapshot {
    // proj-1 is the newer group (4 active), proj-2 older (3 active); 2 dead.
    const sessions = [
      ...Array.from({ length: 4 }, (_, i) =>
        makeSession({ project: '/repo/proj-1', updated_at: secondsAgo(10_000 + i * 1000) })),
      ...Array.from({ length: 3 }, (_, i) =>
        makeSession({ project: '/repo/proj-2', updated_at: secondsAgo(500_000 + i * 1000) })),
      makeSession({ status: 'dead', project: '/repo/proj-1' }),
      makeSession({ status: 'dead', project: '/repo/proj-2' }),
    ]
    return makeSnapshot(sessions)
  }

  it('caps listed rows at top N across groups and counts the remainder', () => {
    const model = build(crowdedSnapshot(), 5)
    const listed = model.groups.flatMap((g) => g.rows)
    expect(listed).toHaveLength(5)
    expect(model.groups.map((g) => [g.label, g.rows.length])).toEqual([
      ['proj-1', 4],
      ['proj-2', 1],
    ])
    expect(model.truncatedCount).toBe(2)
    expect(model.totalCount).toBe(9)
  })

  it('drops groups entirely beyond the cap', () => {
    const model = build(crowdedSnapshot(), 3)
    expect(model.groups.map((g) => [g.label, g.rows.length])).toEqual([['proj-1', 3]])
    expect(model.truncatedCount).toBe(4)
  })

  it('never lists dead sessions', () => {
    const model = build(crowdedSnapshot(), 100)
    expect(model.groups.flatMap((g) => g.rows)).toHaveLength(7)
    expect(model.truncatedCount).toBe(0)
    for (const row of model.groups.flatMap((g) => g.rows)) {
      expect(row.status).not.toBe('dead')
    }
  })

  it('defaults to the exported cap', () => {
    const model = build(crowdedSnapshot())
    expect(model.groups.flatMap((g) => g.rows)).toHaveLength(DEFAULT_OVERVIEW_TOP_N)
  })
})

// ---------------------------------------------------------------------------
// overviewToOptions — row composition.
// ---------------------------------------------------------------------------

describe('overviewToOptions', () => {
  it('composes status, counts, session rows, truncation marker and board hint', () => {
    const snapshot = makeSnapshot([
      makeSession({ project: '/repo/proj-a', title: '重构 supervisor' }),
      makeSession({ project: '/repo/proj-a', status: 'waiting' }),
      makeSession({ project: '/repo/proj-a', status: 'idle' }),
    ])
    const options = overviewToOptions(build(snapshot, 2))
    const ids = optionIds(options)
    expect(ids[0]).toBe('daemon')
    expect(ids[1]).toBe('counts')
    expect(ids.filter((id) => id.startsWith('session:'))).toHaveLength(2)
    expect(ids).toContain('truncated')
    expect(ids[ids.length - 1]).toBe('board')
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('carries project, status and relative time in the session detail line', () => {
    const options = overviewToOptions(
      build(makeSnapshot([makeSession({ project: '/repo/proj-a', title: '重构 supervisor' })])),
    )
    const session = options.find((o) => o.id.startsWith('session:'))!
    expect(session.label).toBe('✳ 重构 supervisor')
    expect(session.detail).toContain('proj-a')
    expect(session.detail).toContain(commandZh['command.status.working'])
    expect(session.detail).toContain(commandZh['command.time.justNow'])
  })

  it('renders the counts row from the model counts', () => {
    const options = overviewToOptions(
      build(makeSnapshot([makeSession(), makeSession({ status: 'waiting' })])),
    )
    const counts = options.find((o) => o.id === 'counts')!
    expect(counts.label).toBe('1 个工作中 · 1 个等待中')
    expect(counts.detail).toBe('共 2 个会话')
  })

  it('offline guidance row precedes the counts row', () => {
    const options = overviewToOptions(
      build(makeSnapshot([makeSession()], { daemon: { state: 'failed', lastPing: null } })),
    )
    expect(optionIds(options).slice(0, 3)).toEqual(['daemon', 'guidance:daemon-failed', 'counts'])
  })
})

// ---------------------------------------------------------------------------
// Locale segment.
// ---------------------------------------------------------------------------

describe('command locale segment', () => {
  it('zh and en carry identical key sets', () => {
    expect(Object.keys(commandEn).sort()).toEqual(Object.keys(commandZh).sort())
  })

  it('every key sits in the command domain and every entry is non-empty', () => {
    for (const dict of [commandZh, commandEn]) {
      for (const [key, value] of Object.entries(dict)) {
        expect(key).toMatch(/^command\.[^.].*$/)
        expect(value, `empty copy for ${key}`).toBeTypeOf('string')
        expect((value as string).length, `empty copy for ${key}`).toBeGreaterThan(0)
      }
    }
  })

  it('buildOverview follows the shared active-locale switch', () => {
    setLocale('en')
    const model = build(makeSnapshot([]))
    expect(model.daemonLabel).toBe(commandEn['command.daemon.hosted'])
    expect(model.connectionLabel).toBe(commandEn['command.connection.ok'])
    expect(model.boardHint).toBe(commandEn['command.boardHint'])
  })
})

// ---------------------------------------------------------------------------
// Agent Center navigation store and surface.
// ---------------------------------------------------------------------------

describe('createCenterNavigation', () => {
  it('opens and closes idempotently, notifying only on state changes', () => {
    const navigation = createCenterNavigation()
    const listener = vi.fn()
    navigation.subscribe(listener)

    expect(navigation.getSnapshot()).toBe(false)
    expect(navigation.open()).toBe(true)
    expect(navigation.getSnapshot()).toBe(true)
    expect(listener).toHaveBeenCalledOnce()

    expect(navigation.open()).toBe(true)
    expect(listener).toHaveBeenCalledOnce()

    navigation.close()
    expect(navigation.getSnapshot()).toBe(false)
    expect(listener).toHaveBeenCalledTimes(2)

    navigation.close()
    expect(listener).toHaveBeenCalledTimes(2)
  })

  it('retains an early open and stops notifying unsubscribed listeners', () => {
    const navigation = createCenterNavigation()
    expect(navigation.open()).toBe(true)
    expect(navigation.getSnapshot()).toBe(true)

    const listener = vi.fn()
    const unsubscribe = navigation.subscribe(listener)
    navigation.close()
    expect(listener).toHaveBeenCalledOnce()

    unsubscribe()
    navigation.open()
    expect(listener).toHaveBeenCalledOnce()
    expect(navigation.getSnapshot()).toBe(true)
  })
})

describe('CenterOverlay', () => {
  it('renders the official modal seam without claiming effect attributes in SSR', () => {
    const html = renderToStaticMarkup(createElement(
      CenterOverlay,
      {
        open: true,
        onClose: () => {},
        title: 'Agent Center',
        closeLabel: 'Close',
      },
      createElement('span', null, 'board'),
    ))

    expect(html).toContain('role="dialog"')
    expect(html).toContain('aria-modal="true"')
    expect(html).toContain('aria-label="Agent Center"')
    expect(html).toContain('data-close-label="Close"')
    expect(html).not.toContain('data-dsh-plugin')
    expect(html).not.toContain('data-dsh-part')
    expect(html).toContain('board')
  })
})

// ---------------------------------------------------------------------------
// Contribution: shape and the options() data path.
// ---------------------------------------------------------------------------

describe('createSidecarCommandContribution', () => {
  it('exposes the /sidecar popupSelect contribution shape', () => {
    const contribution = createSidecarCommandContribution()
    expect(contribution.name).toBe(SIDECAR_COMMAND_NAME)
    expect(contribution.ui.kind).toBe('popupSelect')
    expect(contribution.available(undefined)).toBe(true)
    expect(contribution.description).toBe(commandZh['command.description'])
  })

  it('description is a live getter across locale switches', () => {
    const contribution = createSidecarCommandContribution()
    setLocale('en')
    expect(contribution.description).toBe(commandEn['command.description'])
  })

  it('options() fetches a snapshot (signal forwarded) and renders it', async () => {
    let seenSignal: unknown
    const contribution = createSidecarCommandContribution({
      fetchState: async (opts) => {
        seenSignal = opts?.signal
        return makeSnapshot([makeSession({ title: '重构 supervisor' })])
      },
      now: () => NOW,
    })
    const signal = new AbortController().signal
    const options = await contribution.ui.options(undefined, signal)
    expect(seenSignal).toBe(signal)
    const ids = optionIds([...options])
    expect(ids[0]).toBe('daemon')
    expect(ids.some((id) => id.startsWith('session:'))).toBe(true)
  })

  it('options() honors the topN dependency', async () => {
    const sessions = Array.from({ length: 4 }, () => makeSession({ project: '/p' }))
    const contribution = createSidecarCommandContribution({
      fetchState: async () => makeSnapshot(sessions),
      now: () => NOW,
      topN: 2,
    })
    const options = await contribution.ui.options(undefined, new AbortController().signal)
    expect(optionIds([...options]).filter((id) => id.startsWith('session:'))).toHaveLength(2)
    expect(optionIds([...options])).toContain('truncated')
  })

  it('a failed fetch degrades to the unreachable guidance (never throws)', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const contribution = createSidecarCommandContribution({
      fetchState: async () => {
        throw new ApiError('network', 'network_error')
      },
    })
    const options = await contribution.ui.options(undefined, new AbortController().signal)
    expect(optionIds([...options])).toContain('guidance:unreachable')
    expect(errorSpy).toHaveBeenCalledOnce()
  })

  it('a non-Api failure also degrades to the unreachable guidance', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const contribution = createSidecarCommandContribution({
      fetchState: async () => {
        throw new TypeError('boom')
      },
    })
    const options = await contribution.ui.options(undefined, new AbortController().signal)
    expect(optionIds([...options])).toContain('guidance:unreachable')
  })

  it('an abort propagates so the shell can drop the stale popup request', async () => {
    const contribution = createSidecarCommandContribution({
      fetchState: async () => {
        throw new ApiError('aborted', 'request_aborted')
      },
    })
    await expect(
      contribution.ui.options(undefined, new AbortController().signal),
    ).rejects.toMatchObject({ kind: 'aborted' })
  })

  it('opens Agent Center only when the board option is selected', () => {
    const openAgentCenter = vi.fn(() => true)
    const contribution = createSidecarCommandContribution({ openCenter: openAgentCenter })
    expect(
      contribution.ui.onSelect({ id: 'board', label: 'open board' }, undefined),
    ).toBeUndefined()
    expect(openAgentCenter).toHaveBeenCalledOnce()
  })

  it('leaves informational option selections inert', () => {
    const openAgentCenter = vi.fn(() => true)
    const contribution = createSidecarCommandContribution({ openCenter: openAgentCenter })
    contribution.ui.onSelect({ id: 'daemon', label: 'status' }, undefined)
    contribution.ui.onSelect({ id: 'session:s1', label: 'session' }, undefined)
    expect(openAgentCenter).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// Registration against a fake commandUi registry.
// ---------------------------------------------------------------------------

/** Fake of the ui-commands registry: duplicate names throw (real semantics). */
function makeFakeRegistry() {
  const contributions = new Map<string, SidecarCommandContribution>()
  return {
    contributions,
    register(contribution: SidecarCommandContribution): () => void {
      if (contributions.has(contribution.name)) {
        throw new Error(`duplicate contribution for /${contribution.name}`)
      }
      contributions.set(contribution.name, contribution)
      return () => {
        contributions.delete(contribution.name)
      }
    },
  }
}

/** Fake mount context: resolves `commandUi` synchronously, records deps. */
function makeFakeCtx(registry: ReturnType<typeof makeFakeRegistry>) {
  const injected: string[][] = []
  const disposers: Array<() => void> = []
  const ctx: CommandMountContext = {
    inject(deps, callback) {
      injected.push([...deps])
      const dispose = callback({ commandUi: registry })
      if (typeof dispose === 'function') disposers.push(dispose)
      return undefined
    },
  }
  return { ctx, injected, disposers }
}

describe('registerSidecarCommand', () => {
  it('registers /sidecar lazily on the commandUi service', () => {
    const registry = makeFakeRegistry()
    const { ctx, injected } = makeFakeCtx(registry)
    registerSidecarCommand(ctx)
    expect(injected).toEqual([['commandUi']])
    expect([...registry.contributions.keys()]).toEqual([SIDECAR_COMMAND_NAME])
    expect(registry.contributions.get(SIDECAR_COMMAND_NAME)!.ui.kind).toBe('popupSelect')
  })

  it('hands a double mount to the new command closure after old cleanup', () => {
    vi.useFakeTimers()
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const registry = makeFakeRegistry()
    const { ctx, disposers } = makeFakeCtx(registry)
    const oldOpen = vi.fn(() => true)
    const newOpen = vi.fn(() => true)
    registerSidecarCommand(ctx, { openCenter: oldOpen })
    registerSidecarCommand(ctx, { openCenter: newOpen })

    expect(registry.contributions.size).toBe(1)
    expect(disposers).toHaveLength(2)
    disposers[0]!()
    vi.advanceTimersByTime(8)
    registry.contributions.get(SIDECAR_COMMAND_NAME)!.ui.onSelect(
      { id: 'board', label: 'board' },
      undefined,
    )
    expect(oldOpen).not.toHaveBeenCalled()
    expect(newOpen).toHaveBeenCalledOnce()
    expect(errorSpy).not.toHaveBeenCalled()

    disposers[1]!()
    expect(registry.contributions.size).toBe(0)
  })

  it('an inject failure is contained (never takes the client half down)', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const ctx: CommandMountContext = {
      inject() {
        throw new Error('no such service')
      },
    }
    expect(() => registerSidecarCommand(ctx)).not.toThrow()
    expect(errorSpy).toHaveBeenCalledOnce()
  })

  it('the registered contribution serves options through the injected deps', async () => {
    const registry = makeFakeRegistry()
    const { ctx } = makeFakeCtx(registry)
    registerSidecarCommand(ctx, {
      fetchState: async () => makeSnapshot([makeSession()]),
      now: () => NOW,
    })
    const contribution = registry.contributions.get(SIDECAR_COMMAND_NAME)!
    const options = await contribution.ui.options(undefined, new AbortController().signal)
    expect(optionIds([...options])[0]).toBe('daemon')
    expect(optionIds([...options]).some((id) => id.startsWith('session:'))).toBe(true)
  })
})
