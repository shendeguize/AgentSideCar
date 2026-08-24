/**
 * Optional better-sidebar integration (T6.3, src/client/sidebar-tab.tsx):
 * the duck-typed service probe, the silent skip when the service is absent
 * (pending inject fiber, one debug line, zero registrations), the
 * registered tab descriptor shape, the visible=false resource gate
 * (VisibleGatedStore drops its controller subscription while hidden and
 * catches up on show), unmount/unload cleanup, and the compact view's
 * markup (react-dom/server over injected fakes; node environment).
 */

import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
import type { SessionCardVM } from '../src/client/board/logic.ts'
import type { SidecarViewState } from '../src/client/controller.ts'
import { zh } from '../src/client/locales/zh.ts'
import {
  MAX_RECENT_SESSIONS,
  SIDEBAR_TAB_ID,
  SIDEBAR_TAB_ORDER,
  VisibleGatedStore,
  countWaiting,
  createSidebarTabComponent,
  deriveMiniVM,
  mountSidebarTab,
  probeBetterSidebar,
  recentActiveSessions,
  type SidebarTabDescriptor,
} from '../src/client/sidebar-tab.tsx'

afterEach(() => {
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// Fakes and fixtures.
// ---------------------------------------------------------------------------

function card(overrides: Partial<SessionCardVM> = {}): SessionCardVM {
  return {
    agent: 'claude',
    sessionId: 'sess-1',
    status: 'working',
    title: 'Fix the flux capacitor',
    project: '/home/u/proj',
    updatedAtMs: 1_700_000_000_000,
    lastEvent: { kind: 'assistant', text: 'on it' },
    gap: false,
    ...overrides,
  }
}

function viewState(overrides: Partial<SidecarViewState> = {}): SidecarViewState {
  return {
    daemonState: 'adopted',
    lastPing: null,
    daemonDetail: undefined,
    streamHealth: 'ok',
    streamStatus: 'open',
    lastReconcileAtMs: null,
    sessions: [],
    injectCapability: false,
    hasSnapshot: true,
    ...overrides,
  }
}

/** Fake controller slice: mutable state + observable listener count. */
class FakeSource {
  private readonly listeners = new Set<() => void>()
  private state: SidecarViewState

  constructor(state: SidecarViewState = viewState()) {
    this.state = state
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  readonly getState = (): SidecarViewState => this.state

  get listenerCount(): number {
    return this.listeners.size
  }

  emit(next: SidecarViewState): void {
    this.state = next
    for (const fn of [...this.listeners]) fn()
  }
}

/** Fake better-sidebar registry: real register/dispose semantics. */
class FakeSidebarRegistry {
  readonly tabs = new Map<string, SidebarTabDescriptor>()
  registerCalls = 0

  readonly registerTab = (descriptor: SidebarTabDescriptor): (() => void) => {
    this.registerCalls += 1
    if (this.tabs.has(descriptor.id)) {
      throw new Error(`tab type "${descriptor.id}" already registered`)
    }
    this.tabs.set(descriptor.id, descriptor)
    return () => {
      this.tabs.delete(descriptor.id)
    }
  }
}

/**
 * Fake mount context mirroring the cordis semantics this module rides:
 * `get` reads the service store without inject requirements; `inject`
 * activates its callback ONLY when the dependency is present (otherwise the
 * fiber stays pending — modelled by simply not calling back); `effect`
 * captures disposers the way a fiber would.
 */
function fakeCtx(service: unknown): {
  ctx: ClientContext
  disposers: Array<() => void>
  injectCalls: string[][]
} {
  const disposers: Array<() => void> = []
  const injectCalls: string[][] = []
  const ctx = {
    get: (name: string) => (name === 'betterSidebar' ? service : undefined),
    inject: (deps: string[], callback: (c: unknown) => void) => {
      injectCalls.push([...deps])
      if (service !== undefined) callback(ctx)
    },
    effect: (fn: () => (() => void) | void) => {
      const disposer = fn()
      if (typeof disposer === 'function') disposers.push(disposer)
    },
  }
  return { ctx: ctx as unknown as ClientContext, disposers, injectCalls }
}

// ---------------------------------------------------------------------------
// Service probe.
// ---------------------------------------------------------------------------

describe('probeBetterSidebar', () => {
  it.each([
    ['undefined', undefined],
    ['null', null],
    ['a primitive', 42],
    ['an object without registerTab', {}],
    ['a non-callable registerTab', { registerTab: 'yes' }],
  ])('answers null for %s', (_label, candidate) => {
    expect(probeBetterSidebar(candidate)).toBeNull()
  })

  it('answers the service itself when registerTab is callable', () => {
    const registry = new FakeSidebarRegistry()
    expect(probeBetterSidebar(registry)).toBe(registry)
  })
})

// ---------------------------------------------------------------------------
// Mount: absent → silent skip; present → registration; unload → cleanup.
// ---------------------------------------------------------------------------

describe('mountSidebarTab', () => {
  it('silently skips when better-sidebar is not installed (one debug line)', () => {
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {})
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { ctx, disposers, injectCalls } = fakeCtx(undefined)
    mountSidebarTab(ctx, new FakeSource())
    // The registration is parked on a pending inject fiber, nothing ran:
    expect(injectCalls).toEqual([['betterSidebar']])
    expect(disposers).toHaveLength(0)
    expect(debugSpy).toHaveBeenCalledTimes(1)
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it('registers the mini tab when the service is present', () => {
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {})
    const registry = new FakeSidebarRegistry()
    const { ctx } = fakeCtx(registry)
    mountSidebarTab(ctx, new FakeSource())
    expect(registry.registerCalls).toBe(1)
    const descriptor = registry.tabs.get(SIDEBAR_TAB_ID)
    expect(descriptor).toBeDefined()
    expect(descriptor?.order).toBe(SIDEBAR_TAB_ORDER)
    expect(descriptor?.single).toBe(true)
    expect(typeof descriptor?.component).toBe('function')
    // i18n-friendly title thunk resolving through the locale table:
    expect(typeof descriptor?.title).toBe('function')
    expect((descriptor?.title as () => string)()).toBe(zh['sidebar.tabTitle'])
    // Service detected at mount time: no "not detected" debug line.
    expect(debugSpy).not.toHaveBeenCalled()
  })

  it('rides ctx.effect: disposing the fiber unregisters the tab', () => {
    const registry = new FakeSidebarRegistry()
    const { ctx, disposers } = fakeCtx(registry)
    mountSidebarTab(ctx, new FakeSource())
    expect(registry.tabs.has(SIDEBAR_TAB_ID)).toBe(true)
    expect(disposers).toHaveLength(1)
    for (const dispose of disposers) dispose()
    expect(registry.tabs.has(SIDEBAR_TAB_ID)).toBe(false)
  })

  it('degrades a duplicate registration to a logged no-op', () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const registry = new FakeSidebarRegistry()
    registry.registerTab({
      id: SIDEBAR_TAB_ID,
      title: 'squatter',
      component: () => null,
    })
    const { ctx, disposers } = fakeCtx(registry)
    expect(() => {
      mountSidebarTab(ctx, new FakeSource())
    }).not.toThrow()
    expect(errorSpy).toHaveBeenCalledTimes(1)
    // The squatter entry stays; disposing our noop disposer changes nothing.
    for (const dispose of disposers) dispose()
    expect(registry.tabs.get(SIDEBAR_TAB_ID)?.title).toBe('squatter')
  })

  it('skips a provided service that fails the duck-type probe', () => {
    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {})
    const { ctx, disposers } = fakeCtx({ notRegisterTab: true })
    mountSidebarTab(ctx, new FakeSource())
    expect(disposers).toHaveLength(0)
    expect(debugSpy).toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// Visibility gate.
// ---------------------------------------------------------------------------

describe('VisibleGatedStore', () => {
  it('starts hidden: no upstream subscription, construction snapshot served', () => {
    const initial = viewState()
    const source = new FakeSource(initial)
    const gate = new VisibleGatedStore(source)
    expect(gate.subscribed).toBe(false)
    expect(source.listenerCount).toBe(0)
    expect(gate.getState()).toBe(initial)
  })

  it('visible=true subscribes upstream and propagates snapshots', () => {
    const source = new FakeSource()
    const gate = new VisibleGatedStore(source)
    let notified = 0
    gate.subscribe(() => {
      notified += 1
    })
    gate.setVisible(true)
    expect(source.listenerCount).toBe(1)
    const next = viewState({ sessions: [card()] })
    source.emit(next)
    expect(gate.getState()).toBe(next)
    expect(notified).toBe(1)
  })

  it('visible=false drops the upstream subscription and freezes the snapshot', () => {
    const source = new FakeSource()
    const gate = new VisibleGatedStore(source)
    let notified = 0
    gate.subscribe(() => {
      notified += 1
    })
    gate.setVisible(true)
    const seen = viewState({ sessions: [card()] })
    source.emit(seen)
    gate.setVisible(false)
    expect(gate.subscribed).toBe(false)
    expect(source.listenerCount).toBe(0)
    const missed = viewState({ sessions: [card(), card({ sessionId: 'sess-2' })] })
    source.emit(missed)
    // Hidden: nothing propagated, last seen snapshot still served.
    expect(notified).toBe(1)
    expect(gate.getState()).toBe(seen)
  })

  it('turning visible again resubscribes and catches up once', () => {
    const source = new FakeSource()
    const gate = new VisibleGatedStore(source)
    let notified = 0
    gate.subscribe(() => {
      notified += 1
    })
    gate.setVisible(true)
    gate.setVisible(false)
    const missed = viewState({ sessions: [card()] })
    source.emit(missed)
    gate.setVisible(true)
    expect(source.listenerCount).toBe(1)
    expect(gate.getState()).toBe(missed)
    expect(notified).toBe(1)
  })

  it('is idempotent per visibility value', () => {
    const source = new FakeSource()
    const gate = new VisibleGatedStore(source)
    gate.setVisible(true)
    gate.setVisible(true)
    expect(source.listenerCount).toBe(1)
    gate.setVisible(false)
    gate.setVisible(false)
    expect(source.listenerCount).toBe(0)
  })

  it('skips notifications when the upstream reference is unchanged', () => {
    const initial = viewState()
    const source = new FakeSource(initial)
    const gate = new VisibleGatedStore(source)
    let notified = 0
    gate.subscribe(() => {
      notified += 1
    })
    gate.setVisible(true)
    source.emit(initial) // same reference: no state change to report
    expect(notified).toBe(0)
  })

  it('dispose() is terminal: upstream dropped, later setVisible is a no-op', () => {
    const source = new FakeSource()
    const gate = new VisibleGatedStore(source)
    gate.setVisible(true)
    gate.dispose()
    expect(source.listenerCount).toBe(0)
    gate.setVisible(true)
    expect(source.listenerCount).toBe(0)
    expect(gate.subscribed).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Pure view model.
// ---------------------------------------------------------------------------

describe('countWaiting', () => {
  it('counts normalized waiting sessions only', () => {
    expect(countWaiting([
      { status: 'waiting' },
      { status: ' Waiting ' },
      { status: 'working' },
      { status: 'dead' },
      { status: 'mystery' },
    ])).toBe(2)
  })
})

describe('recentActiveSessions', () => {
  it('excludes dead, sorts by recency, breaks ties by id, caps the list', () => {
    const sessions = [
      card({ sessionId: 'dead-1', status: 'dead', updatedAtMs: 9_000 }),
      card({ sessionId: 'b-tie', status: 'idle', updatedAtMs: 5_000 }),
      card({ sessionId: 'a-tie', status: 'waiting', updatedAtMs: 5_000 }),
      card({ sessionId: 'newest', status: 'working', updatedAtMs: 8_000 }),
      card({ sessionId: 'old-1', status: 'idle', updatedAtMs: 1_000 }),
      card({ sessionId: 'old-2', status: 'unknownish', updatedAtMs: 2_000 }),
      card({ sessionId: 'old-3', status: 'idle', updatedAtMs: 3_000 }),
      card({ sessionId: 'old-4', status: 'idle', updatedAtMs: 4_000 }),
    ]
    const recent = recentActiveSessions(sessions)
    expect(recent.map((s) => s.sessionId))
      .toEqual(['newest', 'a-tie', 'b-tie', 'old-4', 'old-3'])
    expect(recent).toHaveLength(MAX_RECENT_SESSIONS)
  })

  it('does not mutate the input order', () => {
    const sessions = [
      card({ sessionId: 's1', updatedAtMs: 1 }),
      card({ sessionId: 's2', updatedAtMs: 2 }),
    ]
    recentActiveSessions(sessions)
    expect(sessions.map((s) => s.sessionId)).toEqual(['s1', 's2'])
  })
})

describe('deriveMiniVM', () => {
  it('folds counts, recency list, and the connection dot', () => {
    const vm = deriveMiniVM(viewState({
      daemonState: 'adopted',
      streamHealth: 'ok',
      sessions: [
        card({ sessionId: 'w1', status: 'working', updatedAtMs: 4_000 }),
        card({ sessionId: 'w2', status: 'working', updatedAtMs: 3_000 }),
        card({ sessionId: 'q1', status: 'waiting', updatedAtMs: 2_000 }),
        card({ sessionId: 'd1', status: 'dead', updatedAtMs: 9_000 }),
      ],
    }))
    expect(vm.connection).toBe('ok')
    expect(vm.workingCount).toBe(2)
    expect(vm.waitingCount).toBe(1)
    expect(vm.recent.map((s) => s.sessionId)).toEqual(['w1', 'w2', 'q1'])
    expect(vm.hasSnapshot).toBe(true)
  })

  it('degrades the dot with the daemon state', () => {
    expect(deriveMiniVM(viewState({ daemonState: 'failed' })).connection).toBe('off')
    expect(deriveMiniVM(viewState({ daemonState: 'backoff' })).connection).toBe('degraded')
  })
})

// ---------------------------------------------------------------------------
// Component markup (react-dom/server; effects do not run, so rendering
// alone must hold no upstream subscription).
// ---------------------------------------------------------------------------

describe('sidebar tab component', () => {
  function render(source: FakeSource, visible = true): string {
    const Component = createSidebarTabComponent(source)
    return renderToStaticMarkup(createElement(Component, { visible }))
  }

  it('renders the counts row, recent sessions, and the board hint', () => {
    const source = new FakeSource(viewState({
      sessions: [
        card({ sessionId: 'w1', status: 'working', title: 'Alpha task' }),
        card({ sessionId: 'q1', status: 'waiting', title: 'Beta task', agent: 'dsh' }),
      ],
    }))
    const html = render(source)
    expect(html).toContain('data-testid="agent-sidecar-sidebar-tab"')
    expect(html).toContain('1 工作中 · 1 等待中')
    expect(html).toContain('Alpha task')
    expect(html).toContain('Beta task')
    expect(html).toContain(zh['detail.status.working'])
    expect(html).toContain(zh['detail.status.waiting'])
    expect(html).toContain(zh['sidebar.recentTitle'])
    expect(html).toContain(zh['sidebar.boardHint'])
  })

  it('shows the connecting notice before the first snapshot', () => {
    const source = new FakeSource(viewState({ hasSnapshot: false }))
    expect(render(source)).toContain(zh['sidebar.connecting'])
  })

  it('shows the empty notice when no session is active', () => {
    const source = new FakeSource(viewState({
      sessions: [card({ sessionId: 'd1', status: 'dead' })],
    }))
    expect(render(source)).toContain(zh['sidebar.noSessions'])
  })

  it('falls back to the untitled label for blank titles', () => {
    const source = new FakeSource(viewState({
      sessions: [card({ title: '  ' })],
    }))
    expect(render(source)).toContain(zh['sidebar.untitled'])
  })

  it('caps the list at MAX_RECENT_SESSIONS rows', () => {
    const sessions = Array.from({ length: MAX_RECENT_SESSIONS + 2 }, (_, i) =>
      card({ sessionId: `sess-${i}`, updatedAtMs: 1_700_000_000_000 + i }))
    const source = new FakeSource(viewState({ sessions }))
    const rows = render(source).match(/data-testid="agent-sidecar-sidebar-session"/g) ?? []
    expect(rows).toHaveLength(MAX_RECENT_SESSIONS)
  })

  it('holds no upstream subscription from rendering alone', () => {
    const source = new FakeSource(viewState({ sessions: [card()] }))
    render(source, true)
    render(source, false)
    expect(source.listenerCount).toBe(0)
  })
})
