/**
 * Agent Sidecar browser composition root.
 *
 * Builds the controller and narrow UI ports, then mounts:
 *
 * - `shell.overlay` (list slot, root scope): the first-class Agent Center,
 *   reachable with or without an active conversation;
 * - `conversation.view` (list slot, session scope): a second entry to the
 *   same cross-agent board and its project/detail routes;
 * - a self-healing Agent Center row in the host sidebar;
 * - `sidebar.footer.action` (list slot, root scope): the connection-dot
 *   footer widget;
 * - `settings.plugin.item` (keyed slot, root scope): the settings card,
 *   keyed by the host-side namespace `agent-sidecar`;
 * - the lazily registered `/sidecar` command and optional better-sidebar
 *   mini tab.
 *
 * Lifecycle contract:
 * - every resource rides `ctx.effect`: the data stream + visibilitychange
 *   listener, the injected `<style data-plugin>` tags (tsdown's CSS-module
 *   loader resets a current-bundle manifest and injects them at factory
 *   execution — once per materialization — so a keeper effect prunes stale
 *   tags and restores authoritative manifest text when a re-apply of the
 *   same materialized module follows an unload);
 * - the optional Host locale bridge rides a `locale`-injected child fiber;
 *   without that service the local table stays on zh, while every independent
 *   React root subscribes once and refreshes its complete descendant tree;
 * - HMR handoff: each slot waits briefly while an overlapping old fiber owns
 *   this plugin's id/key, then registers the new component closure;
 * - graceful degradation: every mount is individually try/catch-ed —
 *   a failing seat logs and is skipped, never taking the GUI down;
 * - the settings card waits on the optional `settingsScope` service via
 *   `ctx.inject` (provided by dsh-client-ui-settings in the web app
 *   composition); board and widget mount without it.
 */

import type { ClientContext } from '@deepseek-ai/dsh-client-runtime/client'
// Type-only slot-contract merges into SlotMap (erased at runtime):
import type {} from '@deepseek-ai/dsh-client-ui-conversation/client'
import type {} from '@deepseek-ai/dsh-client-ui-layout/client'
import type {} from '@deepseek-ai/dsh-client-ui-sidebar/client'
import type {} from '@deepseek-ai/dsh-client-ui-settings-plugins/client'
import type { SettingsScope, SettingsScopeSpec } from '@deepseek-ai/dsh-client-runtime/client'
import { registerSidecarCommand } from './commands.ts'
import { PLUGIN_ID, SidecarController } from './controller.ts'
import { createInjectActions, createVerifyProbe } from './inject-glue.ts'
import type { InjectMode } from './inject/logic.ts'
import {
  acquireWithHandoff,
  isRegistrationCollision,
} from './lifecycle/handoff.ts'
import type { HostLocalePort } from './locales/host.ts'
import { attachHostLocale, subscribeLocale, t } from './locales/index.ts'
import {
  createBoardTab,
  createCenterOverlay,
  createFooterWidget,
  createSettingsCardEntry,
} from './mount.tsx'
import { createCenterNavigation } from './navigation/center.ts'
import {
  mountSidebarEntry,
  type SidebarEntryCopyPort,
} from './navigation/sidebar-entry.ts'
import { mountSidebarTab } from './sidebar-tab.tsx'
import { createDefaultIntegration, type BoardUiPort } from './ui-integration.ts'
import type { SidecarConfigView } from './settings-glue.ts'

export const name = 'agent-sidecar'

/** The slot registry is the only hard dependency; settingsScope and locale are lazy. */
export const inject = ['slots']

/** Entry id (list slots) and cell key (settings keyed slot) in one. */
const ENTRY_ID = 'agent-sidecar'

/** Distinct list-seat id for the frame-wide Agent Center surface. */
const CENTER_ENTRY_ID = 'agent-sidecar-center'

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

/** Structural Cordis face keeps the unpublished locale package out of runtime imports. */
interface LocaleInjectContextFace {
  inject(deps: string[], callback: (ctx: unknown) => void): unknown
}

/** The four slot seats this plugin occupies (all merged into SlotMap). */
type SidecarSlot =
  | 'shell.overlay'
  | 'conversation.view'
  | 'sidebar.footer.action'
  | 'settings.plugin.item'

/**
 * Apply-guard: whether the slot ledger already holds this plugin's entry
 * (list slots match their seat-specific `id`, the keyed settings slot by
 * namespace `key`). `entries()` answers [] for undeclared slots, and the
 * check runs inside the deferred `slots.inject` callback — i.e. at actual
 * registration time, when the ledger is authoritative.
 */
function hasOwnEntry(ctx: ClientContext, slot: SidecarSlot): boolean {
  const entryId = slot === 'shell.overlay' ? CENTER_ENTRY_ID : ENTRY_ID
  return ctx.slots
    .entries(slot)
    .some((entry) =>
      entry.options.id === entryId ||
      (slot === 'settings.plugin.item' && entry.options.key === SETTINGS_NAMESPACE))
}

/**
 * Lease the optional Host locale service for this injected child fiber.
 * The locale core arbitrates overlapping fibers that share one service.
 */
function mountHostLocale(ctx: ClientContext): void {
  const mount = ctx as unknown as LocaleInjectContextFace
  mount.inject(['locale'], (injected) => {
    const lctx = injected as ClientContext & HostLocalePort
    lctx.effect(
      () => attachHostLocale(lctx),
      'agent-sidecar: host locale bridge',
    )
  })
}

// ---------------------------------------------------------------------------
// Style lifecycle.
// ---------------------------------------------------------------------------

const STYLE_OWNER = Symbol.for('@shendeguize/dsh-agent-sidecar/style-owner')
const STYLE_MANIFEST = Symbol.for('@shendeguize/dsh-agent-sidecar/style-manifest')
const STYLE_GENERATION = Symbol.for('@shendeguize/dsh-agent-sidecar/style-generation')

type StyleGlobals = Record<PropertyKey, unknown>

/**
 * A fallback only for a sequential re-apply of this exact materialization.
 * A new intro installs a new generation object, so it can never inherit an
 * earlier materialization's cached CSS.
 */
let cachedStyleManifest:
  | { generation: object; styles: ReadonlyMap<string, string> }
  | undefined

function setStyleOwner(tag: HTMLStyleElement, owner: object): void {
  const sharedTag = tag as unknown as Record<PropertyKey, unknown>
  sharedTag[STYLE_OWNER] = owner
}

function isStyleOwner(tag: Element, owner: object): boolean {
  return (tag as unknown as Record<PropertyKey, unknown>)[STYLE_OWNER] === owner
}

/**
 * Freeze the current materialization's CSS before another bundle can reset
 * it. A present Map is authoritative, including when it is empty.
 */
function snapshotStyleManifest(globals: StyleGlobals): ReadonlyMap<string, string> {
  const generation = globals[STYLE_GENERATION]
  const manifest = globals[STYLE_MANIFEST]
  if (manifest instanceof Map) {
    const styles = new Map<string, string>()
    for (const [tagId, cssText] of manifest) {
      if (typeof tagId === 'string' && typeof cssText === 'string') {
        styles.set(tagId, cssText)
      }
    }
    if (typeof generation === 'object' && generation !== null) {
      cachedStyleManifest = { generation, styles }
    }
    return styles
  }
  if (
    typeof generation === 'object' &&
    generation !== null &&
    cachedStyleManifest?.generation === generation
  ) {
    return new Map(cachedStyleManifest.styles)
  }
  return new Map()
}

/**
 * Keeper effect for the tsdown-injected `<style data-plugin>` tags. Ownership
 * lives on the DOM node through Symbol.for so the latest HMR fiber wins. The
 * current bundle manifest is authoritative: plugin tags for CSS modules
 * absent from it are stale and removed.
 */
function keepStylesAlive(
  documentRef: Document | undefined = typeof document === 'undefined' ? undefined : document,
  globals: StyleGlobals = globalThis as unknown as StyleGlobals,
): () => void {
  if (documentRef === undefined) return () => {}
  const manifest = snapshotStyleManifest(globals)
  const owner = {}
  const ownTags = `style[data-plugin=${JSON.stringify(PLUGIN_ID)}]`
  for (const el of Array.from(documentRef.querySelectorAll(ownTags))) {
    const tag = el as HTMLStyleElement
    const key = tag.dataset['pluginCss']
    const cssText = key === undefined ? undefined : manifest.get(key)
    if (cssText === undefined) {
      tag.remove()
      continue
    }
    tag.textContent = cssText
    setStyleOwner(tag, owner)
  }
  for (const [key, cssText] of manifest) {
    const selector = `${ownTags}[data-plugin-css=${JSON.stringify(key)}]`
    if (documentRef.querySelector(selector) === null) {
      const tag = documentRef.createElement('style')
      tag.dataset['plugin'] = PLUGIN_ID
      tag.dataset['pluginCss'] = key
      tag.textContent = cssText
      setStyleOwner(tag, owner)
      documentRef.head.appendChild(tag)
    }
  }
  return () => {
    for (const el of Array.from(documentRef.querySelectorAll(ownTags))) {
      if (isStyleOwner(el, owner)) el.remove()
    }
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
  const navigation = createCenterNavigation()
  const openAgentCenter = navigation.open
  const sidebarEntryCopy: SidebarEntryCopyPort = {
    get label() { return t('sidebar.centerEntryLabel') },
    get accessibilityLabel() { return t('sidebar.centerEntryAria') },
    subscribe: subscribeLocale,
  }

  // Optional service: a pending inject fiber allocates no resources and keeps
  // the module-owned zh fallback. A bridge failure never blocks other seats.
  try {
    mountHostLocale(ctx)
  } catch {
    // Optional locale injection must not block the remaining client seats.
  }

  // The detail view hosts injection. A delivered execute pulls one fresh
  // snapshot so the board reflects it promptly. The default mode remains
  // late-bound because settings resolve after the board mounts.
  const injectPrefs: { defaultMode: InjectMode } = { defaultMode: 'queue' }

  // The state snapshot carries no analysis bit, so the UI mirrors the live
  // `analysis.enabled` setting. False is fail-closed until settings resolve;
  // the server's 403 gate remains authoritative.
  const analysisPrefs: { enabled: boolean } = { enabled: false }

  // Compose the production stores and detail capabilities behind the
  // board's narrow port.
  const uiIntegration: BoardUiPort = createDefaultIntegration({
    inject: {
      actions: createInjectActions({
        onDelivered: () => {
          void controller.refresh()
        },
      }),
      getDefaultMode: () => injectPrefs.defaultMode,
      createVerifyProbe: (sessionId) => createVerifyProbe(sessionId),
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

  // Seat 1: frame-wide Agent Center. Root scope keeps it reachable from an
  // empty conversation, and the retained navigation snapshot makes early
  // open requests visible as soon as the layout declares this official seat.
  try {
    const SidecarCenterOverlay = createCenterOverlay(controller, uiIntegration, navigation)
    ctx.slots.inject('shell.overlay', () => acquireWithHandoff(
      () => hasOwnEntry(ctx, 'shell.overlay')
        ? undefined
        : ctx.slots.register({
            name: 'shell.overlay',
            id: CENTER_ENTRY_ID,
            order: 30,
          }, SidecarCenterOverlay),
      {
        isCollision: isRegistrationCollision,
        onError: (error) => {
          console.error('agent-sidecar: center overlay registration failed', error)
        },
        onTimeout: () => {
          console.error('agent-sidecar: center overlay handoff timed out')
        },
      },
    ))
  } catch (err) {
    console.error('agent-sidecar: center overlay mount failed', err)
  }

  // Sidebar navigation shares the same narrow callback as the footer and
  // `/sidecar`; this adapter owns only its host-side DOM placement.
  try {
    ctx.effect(
      () => mountSidebarEntry(openAgentCenter, sidebarEntryCopy),
      'agent-sidecar: sidebar entry',
    )
  } catch (err) {
    console.error('agent-sidecar: sidebar entry mount failed', err)
  }

  // Seat 2: cross-agent board as the "Sidecar" conversation tab.
  try {
    const SidecarBoardTab = createBoardTab(controller, uiIntegration)
    ctx.slots.inject('conversation.view', () => acquireWithHandoff(
      () => hasOwnEntry(ctx, 'conversation.view')
        ? undefined
        : ctx.slots.register({
            name: 'conversation.view',
            id: ENTRY_ID,
            order: 30,
            label: 'Sidecar',
          }, SidecarBoardTab),
      {
        isCollision: isRegistrationCollision,
        onError: (error) => {
          console.error('agent-sidecar: board tab registration failed', error)
        },
        onTimeout: () => {
          console.error('agent-sidecar: board tab handoff timed out')
        },
      },
    ))
  } catch (err) {
    console.error('agent-sidecar: board tab mount failed', err)
  }

  // Seat 3: footer status widget.
  try {
    const SidecarFooterWidget = createFooterWidget(controller, openAgentCenter)
    ctx.slots.inject('sidebar.footer.action', () => acquireWithHandoff(
      () => hasOwnEntry(ctx, 'sidebar.footer.action')
        ? undefined
        : ctx.slots.register({
            name: 'sidebar.footer.action',
            id: ENTRY_ID,
            order: 30,
          }, SidecarFooterWidget),
      {
        isCollision: isRegistrationCollision,
        onError: (error) => {
          console.error('agent-sidecar: footer widget registration failed', error)
        },
        onTimeout: () => {
          console.error('agent-sidecar: footer widget handoff timed out')
        },
      },
    ))
  } catch (err) {
    console.error('agent-sidecar: footer widget mount failed', err)
  }

  // Seat 4: settings card, once the optional settingsScope service resolves.
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
        sctx.slots.inject('settings.plugin.item', () => acquireWithHandoff(
          () => hasOwnEntry(sctx, 'settings.plugin.item')
            ? undefined
            : sctx.slots.register({
                name: 'settings.plugin.item',
                key: SETTINGS_NAMESPACE,
              }, SidecarSettingsCardEntry),
          {
            isCollision: isRegistrationCollision,
            onError: (error) => {
              console.error('agent-sidecar: settings card registration failed', error)
            },
            onTimeout: () => {
              console.error('agent-sidecar: settings card handoff timed out')
            },
          },
        ))
      } catch (err) {
        console.error('agent-sidecar: settings card mount failed', err)
      }
    })
  } catch (err) {
    console.error('agent-sidecar: settings scope injection failed', err)
  }

  // Seat 5: the `/sidecar` slash command, lazy on the commandUi service.
  // registerSidecarCommand catches its own registration failures
  // and owns the bounded overlap handoff; this try/catch keeps the
  // degradation posture uniform with the slot seats. Disposal rides the
  // injected fiber — unloading the plugin unregisters it.
  try {
    registerSidecarCommand(ctx, { openCenter: openAgentCenter })
  } catch (err) {
    console.error('agent-sidecar: /sidecar command mount failed', err)
  }

  // Seat 6 (optional): the better-sidebar mini tab, parked on the
  // lazily injected `betterSidebar` service (optional peer; runtime
  // duck-typed probe, never imported). Not installed → the fiber stays
  // pending forever: silent skip, zero resources. A probe/registration
  // failure degrades to a log and never touches the other seats.
  try {
    mountSidebarTab(ctx, controller)
  } catch (err) {
    console.error('agent-sidecar: better-sidebar tab mount failed', err)
  }
}
