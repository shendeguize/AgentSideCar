/**
 * Optional better-sidebar mini tab (T6.3, design §5.2 / ADR-5 option C).
 *
 * dsh-better-sidebar (npm, ≥0.4.0) turns the sidebar into a registry
 * service: its client half runs `ctx.provide('betterSidebar', service)` and
 * consumers call `service.registerTab(descriptor)` (source of record:
 * better-sidebar docs/external-plugin-guide.md + src/client/service.ts).
 * This module integrates as a SOFT dependency, dsh-sentinel style:
 *
 * - NO value import and NO type import of `dsh-better-sidebar` — the client
 *   bundle purity gate stays react-only. The minimal service contract is
 *   restated locally ({@link BetterSidebarServiceFace}) and the live service
 *   is duck-typed at runtime ({@link probeBetterSidebar}).
 * - Absent → silent skip: {@link mountSidebarTab} parks the registration in
 *   `ctx.inject(['betterSidebar'], …)` — a cordis fiber that stays PENDING
 *   until some plugin provides the service (zero timers, zero polling, zero
 *   resources), plus one debug-level log line at mount time. If
 *   better-sidebar is installed later (any load order), the fiber activates
 *   and the tab registers; if never, nothing ever runs.
 * - Present → one compact "Sidecar" tab (`agent-sidecar:monitor`): daemon
 *   connection dot, working/waiting counts, and the most recently active
 *   sessions, all read from the SHARED {@link SidecarController} stores —
 *   no second poller, no second SSE.
 * - `visible === false` releases resources: better-sidebar hands tab
 *   components a `visible` prop (false while the panel is collapsed or
 *   another tab is active). The shared controller has no pause/refcount
 *   surface (its stream is plugin-lifetime, `stop()` is terminal), so the
 *   gate lives in this view: {@link VisibleGatedStore} unsubscribes from the
 *   controller while hidden (re-render churn stops; the view triggers no
 *   fetches of its own) and resubscribes + catches up on show.
 */

import { createElement as h, useEffect, useState, useSyncExternalStore } from 'react'
import type { CSSProperties, ReactElement, ReactNode } from 'react'
import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
import type { SidecarController, SidecarViewState } from './controller.ts'
import {
  agentGlyph,
  countWorking,
  deriveWidgetConnection,
  formatRelativeTime,
  normalizeStatus,
  projectDisplayName,
  widgetTitle,
  type SessionCardVM,
  type SessionStatusToken,
  type WidgetConnection,
} from './board/logic.ts'
import { t, type SidecarLocaleKey } from './locales/index.ts'

/** Registered tab type id (design §5.2 names it verbatim). */
export const SIDEBAR_TAB_ID = 'agent-sidecar:monitor'

/** `+`-menu order: after the built-ins (explorer=10 … browser=50). */
export const SIDEBAR_TAB_ORDER = 60

/** Compact view caps the session list at this many rows (task spec: 3-5). */
export const MAX_RECENT_SESSIONS = 5

// ---------------------------------------------------------------------------
// Duck-typed better-sidebar contract (locally restated, never imported).
// ---------------------------------------------------------------------------

/** Props better-sidebar hands every tab component; only `visible` is used. */
export interface SidebarTabComponentProps {
  /** False while the panel is collapsed or another tab is active. */
  visible: boolean
}

/** The one descriptor shape this module registers (subset of TabDescriptor). */
export interface SidebarTabDescriptor {
  id: string
  title: string | (() => string)
  icon?: (size: number) => ReactNode
  order?: number
  /** Single-instance sugar: opening again focuses the existing tab. */
  single?: boolean
  component: (props: SidebarTabComponentProps) => ReactNode
}

/** Minimal face of `ctx.betterSidebar` this module relies on. */
export interface BetterSidebarServiceFace {
  /** Registers a tab type; returns the disposer. Throws on duplicate id. */
  registerTab(descriptor: SidebarTabDescriptor): () => void
}

/**
 * Duck-type a candidate service value: any object exposing a callable
 * `registerTab` qualifies (the registry contract is stable since
 * better-sidebar v0.4.0). Anything else — absent service, or a foreign
 * object squatting on the service name — reads as "not installed".
 */
export function probeBetterSidebar(candidate: unknown): BetterSidebarServiceFace | null {
  if (typeof candidate !== 'object' || candidate === null) return null
  const face = candidate as { registerTab?: unknown }
  return typeof face.registerTab === 'function'
    ? (candidate as BetterSidebarServiceFace)
    : null
}

// ---------------------------------------------------------------------------
// Visibility gate over the shared controller.
// ---------------------------------------------------------------------------

/** The slice of {@link SidecarController} this view consumes (tests fake it). */
export type SidecarStateSource = Pick<SidecarController, 'subscribe' | 'getState'>

/**
 * A pausable read-through store between the shared controller and one tab
 * instance. While visible it mirrors the controller (subscribed, snapshots
 * flow, listeners notified); while hidden it holds NO controller
 * subscription — upstream notifications cost this view nothing — and serves
 * the last seen snapshot. Turning visible again resubscribes and catches up
 * once. Methods are bound fields so uSES sees stable identities.
 */
export class VisibleGatedStore {
  private readonly source: SidecarStateSource
  private readonly listeners = new Set<() => void>()
  private unsubscribe: (() => void) | null = null
  private snapshot: SidecarViewState
  private disposed = false

  constructor(source: SidecarStateSource) {
    this.source = source
    this.snapshot = source.getState()
  }

  /** Whether the upstream controller subscription is currently held. */
  get subscribed(): boolean {
    return this.unsubscribe !== null
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  readonly getState = (): SidecarViewState => this.snapshot

  /** Idempotent visibility switch: subscribe + catch up, or unsubscribe. */
  setVisible(visible: boolean): void {
    if (this.disposed || visible === this.subscribed) return
    if (visible) {
      this.unsubscribe = this.source.subscribe(() => {
        this.pull()
      })
      this.pull()
    } else {
      this.unsubscribe?.()
      this.unsubscribe = null
    }
  }

  /** Terminal teardown (component unmount): drop upstream and listeners. */
  dispose(): void {
    this.disposed = true
    this.unsubscribe?.()
    this.unsubscribe = null
    this.listeners.clear()
  }

  private pull(): void {
    const next = this.source.getState()
    if (next === this.snapshot) return
    this.snapshot = next
    for (const fn of [...this.listeners]) fn()
  }
}

// ---------------------------------------------------------------------------
// Pure view model (exported for tests).
// ---------------------------------------------------------------------------

/** Count of sessions currently observed as waiting. */
export function countWaiting(sessions: ReadonlyArray<{ status: string }>): number {
  let count = 0
  for (const session of sessions) {
    if (normalizeStatus(session.status) === 'waiting') count += 1
  }
  return count
}

/**
 * The compact list: non-dead sessions, most recently updated first, capped
 * at {@link MAX_RECENT_SESSIONS} (ties break by session id for stability).
 */
export function recentActiveSessions<T extends SessionCardVM>(
  sessions: readonly T[],
  limit: number = MAX_RECENT_SESSIONS,
): T[] {
  return sessions
    .filter((session) => normalizeStatus(session.status) !== 'dead')
    .sort((a, b) =>
      b.updatedAtMs !== a.updatedAtMs
        ? b.updatedAtMs - a.updatedAtMs
        : a.sessionId.localeCompare(b.sessionId))
    .slice(0, limit)
}

/** Everything the mini tab renders. */
export interface SidebarMiniVM {
  connection: WidgetConnection
  /** Hover text for the header dot (connection + working count). */
  connectionTitle: string
  workingCount: number
  waitingCount: number
  recent: SessionCardVM[]
  hasSnapshot: boolean
}

/** Fold the shared view state into the mini tab's view model (pure). */
export function deriveMiniVM(state: SidecarViewState): SidebarMiniVM {
  const connection = deriveWidgetConnection(state.daemonState, state.streamHealth)
  const workingCount = countWorking(state.sessions)
  return {
    connection,
    connectionTitle: widgetTitle(connection, workingCount),
    workingCount,
    waitingCount: countWaiting(state.sessions),
    recent: recentActiveSessions(state.sessions),
    hasSnapshot: state.hasSnapshot,
  }
}

// ---------------------------------------------------------------------------
// Presentation.
// ---------------------------------------------------------------------------

const STATUS_LABEL_KEY: Record<SessionStatusToken, SidecarLocaleKey> = {
  working: 'detail.status.working',
  waiting: 'detail.status.waiting',
  idle: 'detail.status.idle',
  dead: 'detail.status.dead',
  unknown: 'detail.status.unknown',
}

const DOT_COLORS: Record<WidgetConnection, string> = {
  ok: 'var(--dsw-alias-state-success-primary, #1a7f37)',
  degraded: 'var(--dsw-alias-state-warn-primary, #9a6700)',
  off: 'var(--dsw-alias-label-dimmed, #8c959f)',
}

const rootStyle: CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  gap: 8,
  padding: '10px 12px',
  fontSize: 12,
  color: 'var(--dsw-alias-label-primary, #1f2328)',
}

const headerStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
}

const dotStyle = (connection: WidgetConnection): CSSProperties => ({
  width: 8,
  height: 8,
  borderRadius: '50%',
  flex: 'none',
  background: DOT_COLORS[connection],
})

const countsStyle: CSSProperties = {
  fontVariantNumeric: 'tabular-nums',
  color: 'var(--dsw-alias-label-secondary, #57606a)',
}

const sectionTitleStyle: CSSProperties = {
  fontSize: 11,
  fontWeight: 600,
  color: 'var(--dsw-alias-label-secondary, #57606a)',
}

const rowStyle: CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  gap: 6,
  width: '100%',
  padding: '4px 6px',
  border: 'none',
  borderRadius: 6,
  background: 'transparent',
  cursor: 'pointer',
  font: 'inherit',
  textAlign: 'left',
  color: 'inherit',
}

const rowTitleStyle: CSSProperties = {
  flex: 1,
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
}

const rowMetaStyle: CSSProperties = {
  flex: 'none',
  fontSize: 11,
  color: 'var(--dsw-alias-label-dimmed, #8c959f)',
  whiteSpace: 'nowrap',
}

const detailStyle: CSSProperties = {
  margin: '0 6px 4px 20px',
  fontSize: 11,
  lineHeight: '16px',
  color: 'var(--dsw-alias-label-secondary, #57606a)',
  overflowWrap: 'anywhere',
}

const mutedStyle: CSSProperties = {
  color: 'var(--dsw-alias-label-dimmed, #8c959f)',
}

const hintStyle: CSSProperties = {
  fontSize: 11,
  color: 'var(--dsw-alias-label-dimmed, #8c959f)',
}

/** One session row + its inline expansion (last event, project, id). */
function SessionRow(props: {
  session: SessionCardVM
  nowMs: number
  expanded: boolean
  onToggle: () => void
}): ReactElement {
  const { session, nowMs, expanded } = props
  const status = normalizeStatus(session.status)
  const title = session.title.trim() === '' ? t('sidebar.untitled') : session.title
  const rowChildren: ReactNode[] = [
    h('span', { key: 'glyph', 'aria-hidden': true }, agentGlyph(session.agent)),
    h('span', { key: 'title', style: rowTitleStyle, title: session.sessionId }, title),
    h(
      'span',
      { key: 'meta', style: rowMetaStyle },
      `${t(STATUS_LABEL_KEY[status])} · ${formatRelativeTime(session.updatedAtMs, nowMs)}`,
    ),
  ]
  const children: ReactNode[] = [
    h(
      'button',
      {
        key: 'row',
        type: 'button',
        style: rowStyle,
        onClick: props.onToggle,
        'data-testid': 'agent-sidecar-sidebar-session',
        'data-session-id': session.sessionId,
        'data-status': status,
        'aria-expanded': expanded,
      },
      ...rowChildren,
    ),
  ]
  if (expanded) {
    const lastEvent =
      session.lastEvent === null
        ? h('span', { style: mutedStyle }, t('sidebar.noEvent'))
        : `${session.lastEvent.kind}: ${session.lastEvent.text}`
    children.push(
      h(
        'div',
        { key: 'detail', style: detailStyle, 'data-testid': 'agent-sidecar-sidebar-detail' },
        h('div', { key: 'project' }, projectDisplayName(session.project)),
        h('div', { key: 'event' }, lastEvent),
      ),
    )
  }
  return h('li', { style: { listStyle: 'none' } }, ...children)
}

/**
 * Build the tab component bound to the shared controller. One
 * {@link VisibleGatedStore} per mounted tab instance: the `visible` prop is
 * synced into it by effect, unmount disposes it.
 */
export function createSidebarTabComponent(
  controller: SidecarStateSource,
): (props: SidebarTabComponentProps) => ReactElement {
  return function SidecarSidebarTab({ visible }: SidebarTabComponentProps): ReactElement {
    const [gate] = useState(() => new VisibleGatedStore(controller))
    useEffect(() => () => {
      gate.dispose()
    }, [gate])
    useEffect(() => {
      gate.setVisible(visible)
    }, [gate, visible])
    const state = useSyncExternalStore(gate.subscribe, gate.getState, gate.getState)
    const [expandedId, setExpandedId] = useState<string | null>(null)

    const vm = deriveMiniVM(state)
    const nowMs = Date.now()

    let body: ReactNode
    if (!vm.hasSnapshot) {
      body = h('div', { style: mutedStyle }, t('sidebar.connecting'))
    } else if (vm.recent.length === 0) {
      body = h('div', { style: mutedStyle }, t('sidebar.noSessions'))
    } else {
      body = h(
        'ul',
        { style: { margin: 0, padding: 0 } },
        ...vm.recent.map((session) =>
          h(SessionRow, {
            key: session.sessionId,
            session,
            nowMs,
            expanded: expandedId === session.sessionId,
            onToggle: () => {
              setExpandedId((prev) => (prev === session.sessionId ? null : session.sessionId))
            },
          })),
      )
    }

    return h(
      'div',
      { style: rootStyle, 'data-testid': 'agent-sidecar-sidebar-tab', 'data-visible': visible },
      h(
        'div',
        { style: headerStyle, title: vm.connectionTitle },
        h('span', { 'aria-hidden': true, style: dotStyle(vm.connection) }),
        h(
          'span',
          { style: countsStyle, 'data-testid': 'agent-sidecar-sidebar-counts' },
          t('sidebar.countsRow', { working: vm.workingCount, waiting: vm.waitingCount }),
        ),
      ),
      h('div', { style: sectionTitleStyle }, t('sidebar.recentTitle')),
      body,
      h('div', { style: hintStyle }, t('sidebar.boardHint')),
    )
  }
}

// ---------------------------------------------------------------------------
// Mount.
// ---------------------------------------------------------------------------

/** Context slice {@link mountSidebarTab} consumes (tests hand in a fake). */
export interface SidebarMountContext {
  get(name: string): unknown
  inject(deps: string[], callback: (ctx: unknown) => void): unknown
}

/**
 * Park the better-sidebar integration on the optional service. Not
 * installed → the inject fiber never activates (silent skip, one debug
 * line, zero resources). Installed (before or after this plugin, order
 * does not matter) → duck-type the service and register the mini tab
 * inside `ctx.effect`, so plugin unload / HMR unregisters it. A duplicate
 * registration (double apply) or a service throw degrades to a logged
 * no-op — never past this seat.
 */
export function mountSidebarTab(ctx: ClientContext, controller: SidecarStateSource): void {
  const mount = ctx as unknown as SidebarMountContext
  if (probeBetterSidebar(mount.get('betterSidebar')) === null) {
    console.debug(
      'agent-sidecar: better-sidebar not detected; the optional sidebar tab stays idle',
    )
  }
  mount.inject(['betterSidebar'], (injected) => {
    const bctx = injected as ClientContext & SidebarMountContext
    const service = probeBetterSidebar(bctx.get('betterSidebar'))
    if (service === null) {
      // Provided but not duck-compatible (e.g. a future breaking rewrite).
      console.debug('agent-sidecar: betterSidebar service lacks registerTab; skipping tab')
      return
    }
    const component = createSidebarTabComponent(controller)
    bctx.effect(() => {
      try {
        return service.registerTab({
          id: SIDEBAR_TAB_ID,
          title: () => t('sidebar.tabTitle'),
          icon: (size: number) =>
            h('span', { 'aria-hidden': true, style: { fontSize: Math.round(size * 0.75) } }, '◈'),
          order: SIDEBAR_TAB_ORDER,
          single: true,
          component,
        })
      } catch (err) {
        console.error('agent-sidecar: better-sidebar tab registration failed', err)
        return () => {}
      }
    }, 'agent-sidecar: better-sidebar tab')
  })
}
