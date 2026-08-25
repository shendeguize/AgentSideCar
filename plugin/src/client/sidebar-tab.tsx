/**
 * Soft dsh-better-sidebar integration (design §5.2 / ADR-5 option C).
 * The dependency stays duck-typed: absence parks a zero-resource inject
 * fiber, presence registers one compact tab over the shared controller.
 * `visible=false` drops only this view's subscription; the plugin-lifetime
 * controller remains untouched.
 */

import { createElement as h, useEffect, useState, useSyncExternalStore } from 'react'
import type { ReactElement, ReactNode } from 'react'
import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
import type { SidecarController, SidecarViewState } from './controller.ts'
import {
  acquireWithHandoff,
  isRegistrationCollision,
} from './lifecycle/handoff.ts'
import { SidebarTab, SidebarTabIcon } from './sidebar/SidebarTab.tsx'
import { deriveMiniVM } from './sidebar/model.ts'
import { t } from './locales/index.ts'

// Compatibility exports for consumers of the original integration module.
export {
  MAX_RECENT_SESSIONS,
  countWaiting,
  deriveMiniVM,
  recentActiveSessions,
} from './sidebar/model.ts'
export type { SidebarMiniVM } from './sidebar/model.ts'

/** Registered tab type id (design §5.2 names it verbatim). */
export const SIDEBAR_TAB_ID = 'agent-sidecar:monitor'

/** `+`-menu order: after the built-ins (explorer=10 … browser=50). */
export const SIDEBAR_TAB_ORDER = 60

// Duck-typed better-sidebar contract (locally restated, never imported).

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

// Visibility gate over the shared controller.

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

// React integration adapter.

/**
 * Bind the presentation-only tab to the shared controller. One
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
    return h(SidebarTab, { vm: deriveMiniVM(state), visible })
  }
}

// Mount.

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
 * inside `ctx.effect`, so plugin unload / HMR unregisters it. During overlap,
 * the new fiber waits briefly for the old tab to leave, then contributes its
 * own component closure. Foreign collisions time out without eviction.
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
    bctx.effect(() => acquireWithHandoff(
      () => service.registerTab({
        id: SIDEBAR_TAB_ID,
        title: () => t('sidebar.tabTitle'),
        icon: (size: number) => h(SidebarTabIcon, { size }),
        order: SIDEBAR_TAB_ORDER,
        single: true,
        component,
      }),
      {
        isCollision: isRegistrationCollision,
        onError: (error) => {
          console.error('agent-sidecar: better-sidebar tab registration failed', error)
        },
        onTimeout: () => {
          console.error('agent-sidecar: better-sidebar tab handoff timed out')
        },
      },
    ), 'agent-sidecar: better-sidebar tab')
  })
}
