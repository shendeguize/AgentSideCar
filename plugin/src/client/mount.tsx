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

import { Button } from '@deepseek-ai/dsh-client-ui-primitives'
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { ReactElement } from 'react'
import type { SettingsScope } from '@deepseek-ai/dsh-client-runtime/client'
import { Board, type SessionFocusTarget } from './board/Board.tsx'
import { ProjectView } from './board/project-view.tsx'
import { AnalysisPanel } from './analysis/AnalysisPanel.tsx'
import analysisCss from './analysis/analysis.module.css'
import { SidecarWidget } from './widget.tsx'
import { SettingsCard, type SettingsCardValues, type SidecarDaemonStatus } from './settings-card.tsx'
import { countWorking, deriveWidgetConnection } from './board/logic.ts'
import type { BoardFilterState } from './board/logic.ts'
import type { SidecarController, SidecarViewState } from './controller.ts'
import { SidecarDetailView } from './detail-view.tsx'
import { findCardHint, type DetailHeaderHint } from './detail-glue.ts'
import { findProjectSessionHint } from './project-glue.ts'
import type { AnalysisStorePort, BoardUiPort, ProjectsStorePort } from './ui-integration.ts'
import type { AnalysisGlueState, AnalysisTarget } from './analysis-glue.ts'
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

/** Board-tab source views (detail and analysis are full-page routes). */
type SourceView = 'board' | 'projects'
type MainView = SourceView | 'analysis'

const EMPTY_ANALYSIS_STATE: AnalysisGlueState = {
  phase: 'idle',
  analysisSessionId: null,
  exchanges: [],
  messages: [],
  disclaimer: null,
  errorCode: null,
  noticeCode: null,
  progressStep: 0,
}

type DetailRoute = {
  id: string
  hint: DetailHeaderHint | null
  returnRequest: ReturnRequest
}

type ReturnRequest = {
  view: SourceView
  focusTarget: SessionFocusTarget | null
}

type AnalysisRoute = {
  target: AnalysisTarget
  source: {
    view: SourceView
    detail: DetailRoute | null
  }
}

/**
 * Reserved in-memory identity that cannot match a real adapter/session pair.
 * It asks Board/ProjectView to take their existing missing-card fallback.
 */
const FALLBACK_FOCUS_TARGET = Object.freeze({
  agent: Symbol('agent-sidecar-fallback-agent'),
  sessionId: Symbol('agent-sidecar-fallback-session'),
}) as unknown as SessionFocusTarget

interface AnimationFrameScheduler {
  requestAnimationFrame: (callback: FrameRequestCallback) => number
  cancelAnimationFrame: (handle: number) => void
}

/** Clamp a remembered offset to the currently rendered scroll range. */
export function clampScrollTop(
  requested: number,
  scrollHeight: number,
  clientHeight: number,
): number {
  const normalized = Number.isFinite(requested) ? requested : 0
  const maximum = Math.max(0, scrollHeight - clientHeight)
  return Math.min(Math.max(0, normalized), maximum)
}

/**
 * Run restoration after two animation frames and return an idempotent
 * cancellation handle for rapid re-navigation, StrictMode, HMR, and unmount.
 */
export function scheduleAfterLayout(
  scheduler: AnimationFrameScheduler,
  callback: () => void,
): () => void {
  let active = true
  let frame: number | null = scheduler.requestAnimationFrame(() => {
    if (!active) return
    frame = scheduler.requestAnimationFrame(() => {
      frame = null
      if (active) callback()
    })
  })
  return () => {
    if (!active) return
    active = false
    if (frame !== null) scheduler.cancelAnimationFrame(frame)
    frame = null
  }
}

/**
 * Project-correlation view bound to its store: refresh on entry, then
 * throttled SSE-driven refreshes for as long as the view is on screen.
 */
function ProjectsContainer(props: {
  controller: SidecarController
  store: ProjectsStorePort
  onSelectSession: (target: SessionFocusTarget) => void
  onAnalyzeProject?: (target: AnalysisTarget) => void
  rootRef: (element: HTMLDivElement | null) => void
  onScrollTopChange: (scrollTop: number) => void
  returnFocusTarget: SessionFocusTarget | null
  onReturnFocusConsumed: () => void
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
      onAnalyzeProject={props.onAnalyzeProject}
      rootRef={props.rootRef}
      onScrollTopChange={props.onScrollTopChange}
      returnFocusTarget={props.returnFocusTarget}
      onReturnFocusConsumed={props.onReturnFocusConsumed}
    />
  )
}

/** Full-page analysis route; the store stays owned by BoardContent. */
function AnalysisMainView(props: {
  enabled: boolean
  state: AnalysisGlueState
  store: AnalysisStorePort
  target: AnalysisTarget
  onBack: () => void
}): ReactElement {
  return (
    <main
      className={analysisCss['view']}
      data-testid="agent-sidecar-analysis-view"
    >
      <header className={analysisCss['viewHead']}>
        <Button
          type="button"
          size="sm"
          variant="outline"
          onClick={props.onBack}
          data-testid="agent-sidecar-analysis-back"
        >
          {t('analysis.back')}
        </Button>
        <span className={analysisCss['viewTitle']}>{t('analysis.title')}</span>
      </header>
      <div className={analysisCss['viewBody']}>
        <AnalysisPanel
          enabled={props.enabled}
          state={props.state}
          onStart={() => { void props.store.start(props.target) }}
          onFollowup={(question) => { void props.store.followup(question) }}
          onStop={() => { void props.store.stop() }}
        />
      </div>
    </main>
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
    const [detail, setDetail] = useState<DetailRoute | null>(null)
    const [analysisRoute, setAnalysisRoute] = useState<AnalysisRoute | null>(null)
    const [returnFocusRequest, setReturnFocusRequest] = useState<ReturnRequest | null>(null)
    const viewRootsRef = useRef<Record<SourceView, HTMLDivElement | null>>({
      board: null,
      projects: null,
    })
    const scrollTopsRef = useRef<Record<SourceView, number>>({ board: 0, projects: 0 })
    const returnRequestRef = useRef<ReturnRequest | null>(null)
    const pendingFocusCancelRef = useRef<(() => void) | null>(null)
    // One project store per tab mount, created lazily with the integration
    // seam; state survives board↔projects↔detail switches within the tab.
    const [projectsStore] = useState<ProjectsStorePort | null>(
      () => integration?.createProjectsStore() ?? null,
    )
    const [boardAnalysisStore] = useState<AnalysisStorePort | null>(
      () => integration?.createAnalysisStore() ?? null,
    )
    useEffect(() => () => { projectsStore?.dispose() }, [projectsStore])
    useEffect(() => () => { boardAnalysisStore?.dispose() }, [boardAnalysisStore])
    const analysisState = useSyncExternalStore(
      boardAnalysisStore?.subscribe ?? (() => () => {}),
      boardAnalysisStore?.getState ?? (() => EMPTY_ANALYSIS_STATE),
      boardAnalysisStore?.getState ?? (() => EMPTY_ANALYSIS_STATE),
    )
    const openAnalysis = (target: AnalysisTarget): void => {
      if (boardAnalysisStore === null) return
      const sourceView: SourceView = mainView === 'analysis' ? 'board' : mainView
      setAnalysisRoute({
        target,
        source: { view: sourceView, detail },
      })
      setMainView('analysis')
    }

    const closeAnalysis = (): void => {
      if (analysisRoute === null) return
      setAnalysisRoute(null)
      setMainView(analysisRoute.source.view)
      setDetail(analysisRoute.source.detail)
    }

    const cancelPendingFocus = useCallback((): void => {
      pendingFocusCancelRef.current?.()
      pendingFocusCancelRef.current = null
    }, [])

    const clearReturnFocus = useCallback((): void => {
      returnRequestRef.current = null
      setReturnFocusRequest(null)
    }, [])

    const consumeReturnFocus = useCallback((): void => {
      setReturnFocusRequest((current) => {
        if (current !== null && returnRequestRef.current === current) {
          returnRequestRef.current = null
        }
        return null
      })
    }, [])

    const setBoardRoot = useCallback((element: HTMLDivElement | null): void => {
      viewRootsRef.current.board = element
    }, [])
    const setProjectsRoot = useCallback((element: HTMLDivElement | null): void => {
      viewRootsRef.current.projects = element
    }, [])

    const saveVisibleScroll = (view: SourceView): void => {
      const root = viewRootsRef.current[view]
      if (root !== null) scrollTopsRef.current[view] = root.scrollTop
    }

    const detailHintFor = (sessionId: string): DetailHeaderHint | null =>
      findCardHint(state.sessions, sessionId) ??
      (projectsStore !== null
        ? findProjectSessionHint(projectsStore.getState().groups, sessionId)
        : null)

    const openDetail = (target: SessionFocusTarget, source: SourceView): void => {
      if (integration === undefined) return
      cancelPendingFocus()
      clearReturnFocus()
      saveVisibleScroll(source)
      const returnRequest: ReturnRequest = { view: source, focusTarget: target }
      setDetail({
        id: target.sessionId,
        hint: detailHintFor(target.sessionId),
        returnRequest,
      })
    }

    const switchDetailSession = (sessionId: string): void => {
      setDetail((current) => current === null
        ? null
        : {
            id: sessionId,
            hint: detailHintFor(sessionId),
            returnRequest: {
              view: current.returnRequest.view,
              focusTarget: null,
            },
          })
    }

    const closeDetail = (): void => {
      if (detail === null) return
      cancelPendingFocus()
      returnRequestRef.current = detail.returnRequest
      setDetail(null)
    }

    const switchMainView = (next: SourceView): void => {
      if (next === mainView) return
      cancelPendingFocus()
      clearReturnFocus()
      if (mainView !== 'analysis') saveVisibleScroll(mainView)
      setMainView(next)
    }

    useEffect(() => {
      if (detail !== null || analysisRoute !== null || typeof window === 'undefined') return
      cancelPendingFocus()
      const request = returnRequestRef.current
      const view: SourceView = request?.view
        ?? (mainView === 'analysis' ? 'board' : mainView)
      pendingFocusCancelRef.current = scheduleAfterLayout(window, () => {
        pendingFocusCancelRef.current = null
        if (request !== null && returnRequestRef.current !== request) return
        const root = viewRootsRef.current[view]
        if (root === null || !root.isConnected) return
        root.scrollTop = clampScrollTop(
          scrollTopsRef.current[view],
          root.scrollHeight,
          root.clientHeight,
        )
        if (request === null) return
        setReturnFocusRequest(request)
      })
      return cancelPendingFocus
    }, [analysisRoute, cancelPendingFocus, detail, mainView])

    useEffect(() => () => {
      returnRequestRef.current = null
      cancelPendingFocus()
    }, [cancelPendingFocus])

    if (analysisRoute !== null && boardAnalysisStore !== null) {
      return (
        <AnalysisMainView
          enabled={integration?.detail.getAnalysisEnabled() === true}
          state={analysisState}
          store={boardAnalysisStore}
          target={analysisRoute.target}
          onBack={closeAnalysis}
        />
      )
    }

    if (integration !== undefined && detail !== null) {
      return (
        <SidecarDetailView
          // key remounts per session: fresh stores, no state bleed on jumps.
          key={`${detail.returnRequest.focusTarget?.agent ?? 'internal'}:${detail.id}`}
          sessionId={detail.id}
          hint={detail.hint}
          controller={controller}
          integration={integration.detail}
          onAnalyze={openAnalysis}
          onClose={closeDetail}
          onSelectSession={switchDetailSession}
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
            onClick={() => { switchMainView('board') }}
          >
            {t('board.viewBoard')}
          </button>
          <button
            type="button"
            className={css['switcherButton']}
            data-active={mainView === 'projects' || undefined}
            aria-pressed={mainView === 'projects'}
            onClick={() => { switchMainView('projects') }}
          >
            {t('board.viewProjects')}
          </button>
        </div>
        {mainView === 'projects' && projectsStore !== null
          ? (
            <ProjectsContainer
              controller={controller}
              store={projectsStore}
              onSelectSession={(target) => { openDetail(target, 'projects') }}
              onAnalyzeProject={openAnalysis}
              rootRef={setProjectsRoot}
              onScrollTopChange={(scrollTop) => {
                scrollTopsRef.current.projects = scrollTop
              }}
              returnFocusTarget={
                returnFocusRequest?.view === 'projects'
                  ? returnFocusRequest.focusTarget ?? FALLBACK_FOCUS_TARGET
                  : null
              }
              onReturnFocusConsumed={consumeReturnFocus}
            />
          )
          : (
            <Board
              daemonState={state.daemonState}
              {...state.daemonDetail !== undefined ? { daemonDetail: state.daemonDetail } : {}}
              streamHealth={state.streamHealth}
              lastReconcileAtMs={state.lastReconcileAtMs}
              hasSnapshot={state.hasSnapshot}
              initialLoadFailed={state.initialLoadFailed}
              sessions={state.sessions}
              filters={filters}
              onFiltersChange={(next) => {
                controller.setFilters(next)
              }}
              // Hand the promise through so the board can render the
              // in-flight state and a failure notice (UX-07).
              onRefresh={() => controller.refresh()}
              onSelectSession={(target) => { openDetail(target, 'board') }}
              onAnalyze={openAnalysis}
              rootRef={setBoardRoot}
              onScrollTopChange={(scrollTop) => {
                scrollTopsRef.current.board = scrollTop
              }}
              returnFocusTarget={
                returnFocusRequest?.view === 'board'
                  ? returnFocusRequest.focusTarget ?? FALLBACK_FOCUS_TARGET
                  : null
              }
              onReturnFocusConsumed={consumeReturnFocus}
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
