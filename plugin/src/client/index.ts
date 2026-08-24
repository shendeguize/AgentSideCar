/**
 * Agent Sidecar — browser client half (T2.4 full integration + S5/T4.9
 * injection & command wiring).
 *
 * Mounts the completed modules into dsh Web on three slot seats plus one
 * service contribution:
 *
 * - `conversation.view` (list slot, session scope): the cross-agent board
 *   as a "Sidecar" tab, order 30 — the official "multiple views per
 *   session" seat (registration shape follows the S0 skeleton and the
 *   dsh-project-kanban precedent). Selecting a session card opens the
 *   inject panel (T4.5) as a modal overlay: onPrepare/onExecute ride the
 *   action transport via inject-glue.ts, the capability bit comes from the
 *   state snapshot, and `inject.default-mode` is adopted late from the
 *   settings scope (seat 3) — 'queue', the schema default, until then;
 * - `sidebar.footer.action` (list slot, root scope): the connection-dot
 *   footer widget (slot declared by dsh-client-ui-sidebar's footer rail;
 *   type merge imported from its installed d.ts);
 * - `settings.plugin.item` (keyed slot, root scope): the settings card,
 *   keyed by the host-side settings namespace 'agent-sidecar' (the
 *   configurable-plugins tab dispatches one card per HOST-SERVED
 *   namespace — the host half registers it via `ctx.settings`);
 * - `/sidecar` slash command (T4.6): registered lazily on the `commandUi`
 *   service via registerSidecarCommand — a composition without the
 *   slash-menu runtime simply never gains the command, and a duplicate
 *   registration (double apply) degrades to a logged no-op;
 * - optional better-sidebar mini tab (T6.3): parked on the lazily-injected
 *   `betterSidebar` service via mountSidebarTab (optional peer, runtime
 *   duck-typed probe — never imported); not installed = silent skip.
 *
 * Lifecycle contract:
 * - every resource rides `ctx.effect`: the data stream + visibilitychange
 *   listener, the injected `<style data-plugin>` tags (tsdown's CSS-module
 *   loader injects them at factory execution — once per materialization —
 *   so a keeper effect caches their text and restores them when a
 *   re-apply of the same materialized module follows an unload);
 * - apply-guard idempotency: each mount checks the slot ledger for an
 *   entry with this plugin's id/key before registering, so a duplicate
 *   apply never double-registers (same cell + same priority would throw);
 * - graceful degradation: every mount is individually try/catch-ed —
 *   a failing seat logs and is skipped, never taking the GUI down;
 * - the settings card waits on the optional `settingsScope` service via
 *   `ctx.inject` (provided by dsh-client-ui-settings in the web app
 *   composition); board and widget mount without it.
 */

import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
// Type-only slot-contract merges into SlotMap (erased at runtime):
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type {} from '@deepseek-ai/dsh-client-ui-sidebar/client'
import type {} from '@deepseek-ai/dsh-client-ui-settings-plugins/client'
import type { SettingsScope, SettingsScopeSpec } from '@deepseek-ai/dsh-client-runtime/client'
import { registerSidecarCommand } from './commands.ts'
import { PLUGIN_ID, SidecarController } from './controller.ts'
import { createInjectActions } from './inject-glue.ts'
import type { InjectMode } from './inject/logic.ts'
import { createBoardTab, createFooterWidget, createSettingsCardEntry } from './mount.tsx'
import { mountSidebarTab } from './sidebar-tab.tsx'
import { createDefaultIntegration, type SidecarUiIntegration } from './detail-view.tsx'
import type { SidecarConfigView } from './settings-glue.ts'

export const name = 'agent-sidecar'

/** The slot registry is the only hard dependency; settingsScope is lazy. */
export const inject = ['slots']

/** Entry id (list slots) and cell key (settings keyed slot) in one. */
const ENTRY_ID = 'agent-sidecar'

/** Host-side settings namespace (host half registers it via ctx.settings). */
const SETTINGS_NAMESPACE = 'agent-sidecar'

/**
 * Structural face of the `settingsScope` binder service provided by
 * dsh-client-ui-settings (not a published type surface for plugins in this
 * dsh version; shape verified against harness
 * packages/client/ui-settings/src/client/settings-scope.ts).
 */
interface SettingsScopeBinderFace {
  bind<T>(spec: SettingsScopeSpec<T>): SettingsScope<T>
}

/** The three slot seats this plugin occupies (all merged into SlotMap). */
type SidecarSlot = 'conversation.view' | 'sidebar.footer.action' | 'settings.plugin.item'

/**
 * Apply-guard: whether the slot ledger already holds this plugin's entry
 * (list slots match by `id`, the keyed settings slot by `key`; both use
 * the same 'agent-sidecar' token). `entries()` answers [] for undeclared
 * slots, and the check runs inside the deferred `slots.inject` callback —
 * i.e. at actual registration time, when the ledger is authoritative.
 */
function hasOwnEntry(ctx: ClientContext, slot: SidecarSlot): boolean {
  return ctx.slots
    .entries(slot)
    .some((entry) => entry.options.id === ENTRY_ID || entry.options.key === SETTINGS_NAMESPACE)
}

// ---------------------------------------------------------------------------
// Style lifecycle.
// ---------------------------------------------------------------------------

/**
 * Style text per data-plugin-css tag id, cached at module scope so it
 * survives unload → re-apply cycles of one materialized module (the CSS
 * factory body only runs once per materialization).
 */
const styleTextCache = new Map<string, string>()

/**
 * Keeper effect for the tsdown-injected `<style data-plugin>` tags: cache
 * their text, restore any tag a previous unload removed, and remove all of
 * this plugin's tags on dispose.
 */
function keepStylesAlive(): () => void {
  if (typeof document === 'undefined') return () => {}
  const ownTags = `style[data-plugin=${JSON.stringify(PLUGIN_ID)}]`
  for (const el of Array.from(document.querySelectorAll(ownTags))) {
    const key = (el as HTMLStyleElement).dataset['pluginCss']
    if (key !== undefined) styleTextCache.set(key, el.textContent ?? '')
  }
  for (const [key, cssText] of styleTextCache) {
    if (document.querySelector(`style[data-plugin-css=${JSON.stringify(key)}]`) === null) {
      const tag = document.createElement('style')
      tag.dataset['plugin'] = PLUGIN_ID
      tag.dataset['pluginCss'] = key
      tag.textContent = cssText
      document.head.appendChild(tag)
    }
  }
  return () => {
    for (const el of Array.from(document.querySelectorAll(ownTags))) el.remove()
  }
}

// ---------------------------------------------------------------------------
// Plugin entry.
// ---------------------------------------------------------------------------

/**
 * Mount the board tab, footer widget, and settings card, and run the data
 * controller for as long as the plugin fiber lives.
 * @param ctx - browser plugin context handed by the client loader.
 */
export function apply(ctx: ClientContext): void {
  const controller = new SidecarController()

  // Inject integration (S5/T4.9): the detail view hosts the panel; a
  // delivered execute pulls one fresh snapshot so the board reflects the
  // injection promptly. The default mode is a mutable box because the
  // settings scope (seat 3) resolves after the board mounts — the reader
  // is late-bound and the box holds the schema default until then.
  const injectPrefs: { defaultMode: InjectMode } = { defaultMode: 'queue' }

  // Analysis capability (T5.10b): the state snapshot carries no analysis
  // bit, so the UI gate mirrors the live `analysis.enabled` setting the
  // same late-bound way (schema default false = fail-closed until the
  // scope resolves); the server 403 gate stays authoritative regardless.
  const analysisPrefs: { enabled: boolean } = { enabled: false }

  // M3 integration handed to the board tab (design §5.1): detail routing,
  // project view, dsh deep-query tools, and the analysis panel, each over
  // its glue store on the real transports.
  const uiIntegration: SidecarUiIntegration = createDefaultIntegration({
    inject: {
      actions: createInjectActions({
        onDelivered: () => {
          void controller.refresh()
        },
      }),
      getDefaultMode: () => injectPrefs.defaultMode,
    },
    getAnalysisEnabled: () => analysisPrefs.enabled,
  })

  // Data feed + visibility resume (design §5.3: visibilitychange → pollNow).
  ctx.effect(() => {
    try {
      controller.start()
    } catch (err) {
      // e.g. an environment without EventSource: the board renders its
      // empty/unknown state instead of taking the plugin fiber down.
      console.error('agent-sidecar: data stream start failed', err)
    }
    if (typeof document === 'undefined') {
      return () => {
        controller.stop()
      }
    }
    const onVisibility = (): void => {
      if (!document.hidden) controller.pollNow()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      controller.stop()
    }
  }, 'agent-sidecar: client data feed')

  ctx.effect(keepStylesAlive, 'agent-sidecar: injected styles')

  // Seat 1: cross-agent board as the "Sidecar" conversation tab — since
  // T5.10b the shell of the M3 information architecture: board/project
  // switcher, card-click detail routing (timeline + inject + analysis +
  // dsh lineage/search), all riding the integration seams above.
  try {
    const SidecarBoardTab = createBoardTab(controller, uiIntegration)
    ctx.slots.inject('conversation.view', () => {
      if (hasOwnEntry(ctx, 'conversation.view')) return () => {}
      try {
        return ctx.slots.register({
          name: 'conversation.view',
          id: ENTRY_ID,
          order: 30,
          label: 'Sidecar',
        }, SidecarBoardTab)
      } catch (err) {
        console.error('agent-sidecar: board tab registration failed', err)
        return () => {}
      }
    })
  } catch (err) {
    console.error('agent-sidecar: board tab mount failed', err)
  }

  // Seat 2: footer status widget.
  try {
    const SidecarFooterWidget = createFooterWidget(controller)
    ctx.slots.inject('sidebar.footer.action', () => {
      if (hasOwnEntry(ctx, 'sidebar.footer.action')) return () => {}
      try {
        return ctx.slots.register({
          name: 'sidebar.footer.action',
          id: ENTRY_ID,
          order: 30,
        }, SidecarFooterWidget)
      } catch (err) {
        console.error('agent-sidecar: footer widget registration failed', err)
        return () => {}
      }
    })
  } catch (err) {
    console.error('agent-sidecar: footer widget mount failed', err)
  }

  // Seat 3: settings card, once the optional settingsScope service resolves.
  try {
    // The inject callback receives the base cordis Context; narrow once
    // (slots/effect are inherited, settingsScope is the resolved service).
    ctx.inject(['settingsScope'], (injected) => {
      const sctx = injected as ClientContext & { settingsScope: SettingsScopeBinderFace }
      try {
        const scope = sctx.settingsScope.bind<SidecarConfigView>({
          namespace: SETTINGS_NAMESPACE,
        })

        // ui.* settings defaults seed the board filters until the user
        // touches them (localStorage value or filter gesture wins);
        // inject.default-mode feeds the panel's late-bound mode reader;
        // analysis.enabled feeds the analysis entry's late-bound gate.
        const adoptDefaults = (): void => {
          const value = scope.getSnapshot().value
          if (value?.ui !== undefined) controller.adoptConfigDefaults(value.ui)
          if (value?.inject !== undefined) injectPrefs.defaultMode = value.inject.defaultMode
          if (value?.analysis !== undefined) analysisPrefs.enabled = value.analysis.enabled
        }
        sctx.effect(
          () => scope.subscribe(adoptDefaults),
          'agent-sidecar: settings→filter defaults',
        )
        adoptDefaults()

        const SidecarSettingsCardEntry = createSettingsCardEntry(controller, scope)
        sctx.slots.inject('settings.plugin.item', () => {
          if (hasOwnEntry(sctx, 'settings.plugin.item')) return () => {}
          try {
            return sctx.slots.register({
              name: 'settings.plugin.item',
              key: SETTINGS_NAMESPACE,
            }, SidecarSettingsCardEntry)
          } catch (err) {
            console.error('agent-sidecar: settings card registration failed', err)
            return () => {}
          }
        })
      } catch (err) {
        console.error('agent-sidecar: settings card mount failed', err)
      }
    })
  } catch (err) {
    console.error('agent-sidecar: settings scope injection failed', err)
  }

  // Seat 4: the `/sidecar` slash command, lazy on the commandUi service
  // (T4.6). registerSidecarCommand catches its own registration failures
  // (duplicate name on a double apply logs and no-ops); this try/catch
  // keeps the degradation posture uniform with the slot seats. Disposal
  // rides the injected fiber — unloading the plugin unregisters it.
  try {
    registerSidecarCommand(ctx)
  } catch (err) {
    console.error('agent-sidecar: /sidecar command mount failed', err)
  }

  // Seat 5 (optional, T6.3): the better-sidebar mini tab, parked on the
  // lazily-injected `betterSidebar` service (optional peer; runtime
  // duck-typed probe, never imported). Not installed → the fiber stays
  // pending forever: silent skip, zero resources. A probe/registration
  // failure degrades to a log and never touches the other seats.
  try {
    mountSidebarTab(ctx, controller)
  } catch (err) {
    console.error('agent-sidecar: better-sidebar tab mount failed', err)
  }
}
