/**
 * React bindings for the board tab, footer widget, and settings card.
 * Factories close over the controller so subscribe/getSnapshot identities
 * stay stable across renders. The board depends only on {@link BoardUiPort}
 * and passes its nested detail port to the detail container.
 * Each exported root factory's top-level component subscribes once to the
 * active locale, refreshing its complete descendant tree without leaf subscriptions.
 *
 * The settings card owns the staged-edit lifecycle over a bound
 * `SettingsScope` (browser mirror of the host settings namespace):
 * resolved values come from the scope snapshot, edits stage locally, save
 * writes one complete top-level group per changed group (see
 * settings-glue.ts for the write-granularity rationale), and success is
 * judged by comparing the post-write snapshot against the staged target —
 * `scope.set` settles without rejecting even when the host declines the
 * write (it recovers by reloading host state instead).
 */

import { useEffect, useState, useSyncExternalStore } from 'react'
import type { ReactElement } from 'react'
import type { SettingsScope } from '@deepseek-ai/dsh-client-runtime/client'
import { Board } from './board/Board.tsx'
import { ProjectView } from './board/project-view.tsx'
import { SidecarWidget } from './widget.tsx'
import { SettingsCard, type SettingsCardValues, type SidecarDaemonStatus } from './settings-card.tsx'
import { countWorking, deriveWidgetConnection } from './board/logic.ts'
import type { BoardFilterState } from './board/logic.ts'
import type { SidecarController, SidecarViewState } from './controller.ts'
import { SidecarDetailView } from './detail-view.tsx'
import { findCardHint, type DetailHeaderHint } from './detail-glue.ts'
import { findProjectSessionHint } from './project-glue.ts'
import type { BoardUiPort, ProjectsStorePort } from './ui-integration.ts'
import { detailErrorText } from './detail/logic.ts'
import { t } from './locales/index.ts'
import { useActiveLocale } from './locales/react.ts'
import { CenterOverlay } from './navigation/CenterOverlay.tsx'
import type { CenterNavigation, CenterNavigationStore } from './navigation/center.ts'
import css from './detail-view.module.css'
import {
  DEFAULT_CONFIG_VIEW,
  cardValuesEqual,
  configToValues,
  diffGroups,
  type SidecarConfigView,
} from './settings-glue.ts'

/** Board-tab main views (detail is an overlay route on top of either). */
type MainView = 'board' | 'projects'

/**
 * Project-correlation view bound to its store: refresh on entry, then
 * throttled SSE-driven refreshes for as long as the view is on screen.
 */
function ProjectsContainer(props: {
  controller: SidecarController
  store: ProjectsStorePort
  onSelectSession: (sessionId: string) => void
}): ReactElement {
  const { controller, store } = props
  useEffect(() => {
    void store.refresh()
    return controller.subscribe(() => { store.notifySnapshot() })
  }, [controller, store])
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState)
  return (
    <ProjectView
      groups={state.groups}
      loading={state.loading}
      error={state.error === null ? null : detailErrorText(state.error)}
      onSelectSession={props.onSelectSession}
    />
  )
}

/**
 * Cross-agent board content and its project/detail routes:
 *
 * - view 1: the session board, with a 「会话看板 / 项目视图」 switcher
 *   (ProjectView over `GET projects`);
 * - view 2: clicking a session card in EITHER view routes to the full-tab
 *   session-detail view (timeline + 注入 + AI 分析 + dsh 谱系/检索);
 *   detail-internal jumps (lineage nodes, search hits) re-route in place;
 * - view 3: the inject panel opens as a modal from the detail view.
 *
 * Without an integration the board renders read-only and inert (no detail
 * routing).
 */
function createBoardContent(
  controller: SidecarController,
  integration?: BoardUiPort,
): () => ReactElement {
  const subscribe = (cb: () => void): (() => void) => controller.subscribe(cb)
  const getState = (): SidecarViewState => controller.getState()
  const getFilters = (): BoardFilterState => controller.getFilters()

  return function BoardContent(): ReactElement {
    // Third argument (server snapshot) keeps the components renderable under
    // react-dom/server (DOM-level verification harness); same source.
    const state = useSyncExternalStore(subscribe, getState, getState)
    const filters = useSyncExternalStore(subscribe, getFilters, getFilters)
    const [mainView, setMainView] = useState<MainView>('board')
    const [detail, setDetail] = useState<{ id: string; hint: DetailHeaderHint | null } | null>(
      null,
    )
    // One project store per tab mount, created lazily with the integration
    // seam; state survives board↔projects↔detail switches within the tab.
    const [projectsStore] = useState<ProjectsStorePort | null>(
      () => integration?.createProjectsStore() ?? null,
    )
    useEffect(() => () => { projectsStore?.dispose() }, [projectsStore])

    const openDetail = (sessionId: string): void => {
      if (integration === undefined) return
      const hint =
        findCardHint(state.sessions, sessionId) ??
        (projectsStore !== null
          ? findProjectSessionHint(projectsStore.getState().groups, sessionId)
          : null)
      setDetail({ id: sessionId, hint })
    }

    if (integration !== undefined && detail !== null) {
      return (
        <SidecarDetailView
          // key remounts per session: fresh stores, no state bleed on jumps.
          key={detail.id}
          sessionId={detail.id}
          hint={detail.hint}
          controller={controller}
          integration={integration.detail}
          onClose={() => { setDetail(null) }}
          onSelectSession={openDetail}
        />
      )
    }

    return (
      <>
        <div className={css['switcherBar']} data-testid="agent-sidecar-view-switcher">
          <button
            type="button"
            className={css['switcherButton']}
            data-active={mainView === 'board' || undefined}
            aria-pressed={mainView === 'board'}
            onClick={() => { setMainView('board') }}
          >
            {t('board.viewBoard')}
          </button>
          <button
            type="button"
            className={css['switcherButton']}
            data-active={mainView === 'projects' || undefined}
            aria-pressed={mainView === 'projects'}
            onClick={() => { setMainView('projects') }}
          >
            {t('board.viewProjects')}
          </button>
        </div>
        {mainView === 'projects' && projectsStore !== null
          ? (
            <ProjectsContainer
              controller={controller}
              store={projectsStore}
              onSelectSession={openDetail}
            />
          )
          : (
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
              // Hand the promise through so the board can render the
              // in-flight state and a failure notice (UX-07).
              onRefresh={() => controller.refresh()}
              onSelectSession={openDetail}
            />
          )}
      </>
    )
  }
}

/** Bind board content to its independent React root and locale subscription. */
export function createBoardTab(
  controller: SidecarController,
  integration?: BoardUiPort,
): () => ReactElement {
  const BoardContent = createBoardContent(controller, integration)
  return function SidecarBoardTab(): ReactElement {
    useActiveLocale()
    return <BoardContent />
  }
}

/** Bind the shared navigation source to the shell overlay and existing board. */
export function createCenterOverlay(
  controller: SidecarController,
  integration: BoardUiPort,
  navigation: CenterNavigationStore,
): () => ReactElement {
  const BoardContent = createBoardContent(controller, integration)
  return function SidecarCenterOverlay(): ReactElement {
    useActiveLocale()
    const open = useSyncExternalStore(
      navigation.subscribe,
      navigation.getSnapshot,
      navigation.getSnapshot,
    )
    return (
      <CenterOverlay
        open={open}
        onClose={navigation.close}
        title={t('board.topbar.title')}
        closeLabel={t('inject.close')}
      >
        {open ? <BoardContent /> : null}
      </CenterOverlay>
    )
  }
}

/** Footer connection dot + working counter bound to the controller. */
export function createFooterWidget(
  controller: SidecarController,
  onOpen: CenterNavigation,
): () => ReactElement {
  const subscribe = (cb: () => void): (() => void) => controller.subscribe(cb)
  const getState = (): SidecarViewState => controller.getState()
  return function SidecarFooterWidget(): ReactElement {
    useActiveLocale()
    const state = useSyncExternalStore(subscribe, getState, getState)
    return (
      <SidecarWidget
        connection={deriveWidgetConnection(state.daemonState, state.streamHealth)}
        workingCount={countWorking(state.sessions)}
        onOpen={onOpen}
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
    useActiveLocale()
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
