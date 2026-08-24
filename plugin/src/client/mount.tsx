/**
 * Slot-facing React glue (T2.4): binds the {@link SidecarController} stores
 * to the three presentational modules via `useSyncExternalStore` and hands
 * back zero-prop components ready for slot registration. Factories close
 * over the controller so subscribe/getSnapshot identities stay stable
 * across renders (uSES resubscribes on identity change).
 *
 * The settings card entry additionally owns the staged-edit lifecycle over
 * a bound `SettingsScope` (browser mirror of the host settings namespace):
 * resolved values come from the scope snapshot, edits stage locally, save
 * writes one complete top-level group per changed group (see
 * settings-glue.ts for the write-granularity rationale), and success is
 * judged by comparing the post-write snapshot against the staged target —
 * `scope.set` settles without rejecting even when the host declines the
 * write (it recovers by reloading host state instead).
 */

import { useState, useSyncExternalStore } from 'react'
import type { ReactElement } from 'react'
import type { SettingsScope } from '@deepseek-ai/dsh-client-runtime/client'
import { Board } from './board/Board.tsx'
import { SidecarWidget } from './widget.tsx'
import { SettingsCard, type SettingsCardValues, type SidecarDaemonStatus } from './settings-card.tsx'
import { countWorking, deriveWidgetConnection } from './board/logic.ts'
import type { BoardFilterState } from './board/logic.ts'
import type { SidecarController, SidecarViewState } from './controller.ts'
import { InjectPanel } from './inject/InjectPanel.tsx'
import type { InjectMode } from './inject/logic.ts'
import { findInjectTarget, type InjectActions } from './inject-glue.ts'
import overlay from './inject/overlay.module.css'
import {
  DEFAULT_CONFIG_VIEW,
  cardValuesEqual,
  configToValues,
  diffGroups,
  type SidecarConfigView,
} from './settings-glue.ts'

/** What the board tab needs to host the inject panel (S5 wiring, T4.9). */
export interface BoardInjectIntegration {
  /** onPrepare/onExecute over the action transport (inject-glue.ts). */
  actions: InjectActions
  /**
   * Late-bound `inject.default-mode` reader: the settings scope resolves
   * after the tab mounts, so the value is read at panel-open time.
   */
  getDefaultMode: () => InjectMode
}

/**
 * Cross-agent board bound to the controller (the "Sidecar" conversation
 * tab). Selecting a session card opens the inject panel as a modal overlay
 * (design §5.1 view 3: 从卡片唤起,模态); the panel gates itself on the
 * snapshot's inject capability — the entry stays visible when injection is
 * off so the user learns it can be enabled in Settings. Without an
 * integration (owner chose not to wire injection) the board stays inert.
 */
export function createBoardTab(
  controller: SidecarController,
  inject?: BoardInjectIntegration,
): () => ReactElement {
  const subscribe = (cb: () => void): (() => void) => controller.subscribe(cb)
  const getState = (): SidecarViewState => controller.getState()
  const getFilters = (): BoardFilterState => controller.getFilters()
  return function SidecarBoardTab(): ReactElement {
    // Third argument (server snapshot) keeps the components renderable under
    // react-dom/server (DOM-level verification harness); same source.
    const state = useSyncExternalStore(subscribe, getState, getState)
    const filters = useSyncExternalStore(subscribe, getFilters, getFilters)
    const [injectTargetId, setInjectTargetId] = useState<string | null>(null)
    const closeInject = (): void => {
      setInjectTargetId(null)
    }
    return (
      <>
        <Board
          daemonState={state.daemonState}
          {...state.daemonDetail !== undefined ? { daemonDetail: state.daemonDetail } : {}}
          streamHealth={state.streamHealth}
          lastReconcileAtMs={state.lastReconcileAtMs}
          sessions={state.sessions}
          filters={filters}
          onFiltersChange={(next) => {
            controller.setFilters(next)
          }}
          onRefresh={() => {
            void controller.refresh()
          }}
          onSelectSession={(sessionId) => {
            if (inject !== undefined) setInjectTargetId(sessionId)
          }}
        />
        {inject !== undefined && injectTargetId !== null
          ? (
            <div className={overlay['backdrop']} role="presentation" onClick={closeInject}>
              <div
                className={overlay['dialog']}
                role="dialog"
                aria-modal="true"
                onClick={(event) => {
                  event.stopPropagation()
                }}
              >
                {/* key remounts the panel per target: a fresh two-phase
                    machine per session, no state bleed between targets. */}
                <InjectPanel
                  key={injectTargetId}
                  capability={{ inject: state.injectCapability }}
                  target={findInjectTarget(state.sessions, injectTargetId)}
                  defaultMode={inject.getDefaultMode()}
                  onPrepare={inject.actions.onPrepare}
                  onExecute={inject.actions.onExecute}
                  onClose={closeInject}
                />
              </div>
            </div>
          )
          : null}
      </>
    )
  }
}

/** Footer connection dot + working counter bound to the controller. */
export function createFooterWidget(controller: SidecarController): () => ReactElement {
  const subscribe = (cb: () => void): (() => void) => controller.subscribe(cb)
  const getState = (): SidecarViewState => controller.getState()
  return function SidecarFooterWidget(): ReactElement {
    const state = useSyncExternalStore(subscribe, getState, getState)
    return (
      <SidecarWidget
        connection={deriveWidgetConnection(state.daemonState, state.streamHealth)}
        workingCount={countWorking(state.sessions)}
      />
    )
  }
}

/** Fallback card values while the scope snapshot is not ready. */
const FALLBACK_VALUES: SettingsCardValues = configToValues(DEFAULT_CONFIG_VIEW)

/**
 * Settings card bound to the controller (daemon status row) and to the
 * namespace scope (values + persistence). See the module doc for the
 * staged-edit / save-verification contract.
 */
export function createSettingsCardEntry(
  controller: SidecarController,
  scope: SettingsScope<SidecarConfigView>,
): () => ReactElement {
  const subscribeState = (cb: () => void): (() => void) => controller.subscribe(cb)
  const getState = (): SidecarViewState => controller.getState()
  const subscribeScope = (cb: () => void): (() => void) => scope.subscribe(cb)
  const getSnapshot = (): ReturnType<typeof scope.getSnapshot> => scope.getSnapshot()

  return function SidecarSettingsCardEntry(): ReactElement {
    const snapshot = useSyncExternalStore(subscribeScope, getSnapshot, getSnapshot)
    const state = useSyncExternalStore(subscribeState, getState, getState)
    const [staged, setStaged] = useState<Partial<SettingsCardValues>>({})
    const [saving, setSaving] = useState(false)
    const [saveFailed, setSaveFailed] = useState(false)

    const resolved =
      snapshot.value !== undefined ? configToValues(snapshot.value) : FALLBACK_VALUES
    const values: SettingsCardValues = { ...resolved, ...staged }
    const writable =
      snapshot.status === 'ready' && snapshot.writable && snapshot.mode === 'host'
    const dirty = !cardValuesEqual(values, resolved)

    const onChange = <K extends keyof SettingsCardValues>(
      field: K,
      value: SettingsCardValues[K],
    ): void => {
      setSaveFailed(false)
      setStaged((prev) => {
        const next = { ...prev }
        if (resolved[field] === value) delete next[field]
        else next[field] = value
        return next
      })
    }

    const onSave = (): void => {
      const target = { ...resolved, ...staged }
      setSaving(true)
      setSaveFailed(false)
      void (async () => {
        try {
          for (const { group, patch } of diffGroups(resolved, target)) {
            await scope.set(group, patch)
          }
          const after = scope.getSnapshot().value
          if (after !== undefined && cardValuesEqual(configToValues(after), target)) {
            setStaged({})
          } else {
            setSaveFailed(true)
          }
        } catch (err) {
          console.error('agent-sidecar: settings save failed', err)
          setSaveFailed(true)
        } finally {
          setSaving(false)
        }
      })()
    }

    const daemon: SidecarDaemonStatus = {
      state: state.daemonState,
      ...state.lastPing !== null
        ? { pid: state.lastPing.pid, version: state.lastPing.version }
        : {},
    }

    return (
      <SettingsCard
        values={values}
        onChange={onChange}
        onSave={onSave}
        onDiscard={() => {
          setStaged({})
          setSaveFailed(false)
        }}
        writable={writable}
        dirty={dirty}
        saving={saving}
        saveFailed={saveFailed}
        daemon={daemon}
      />
    )
  }
}
