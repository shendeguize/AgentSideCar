/**
 * Session-detail container (T5.10b, design §5.1 view 2): the full-tab
 * detail surface opened by clicking a session card on the board or in the
 * project view. Composes the M3 presentational components over their glue
 * stores:
 *
 * - SessionDetail (timeline) ← DetailStore (fetchSessionDetail +
 *   fetchTimelinePage pagination + SSE-triggered listen refetch);
 * - action row: 注入 (M2 InjectPanel as a modal, reused verbatim) and
 *   AI 分析 (AnalysisPanel over AnalysisStore; disabled with an honest
 *   hint while `analysis.enabled` is off);
 * - dsh 会话专属区: LineageTree ← the DetailStore lineage slice (non-dsh
 *   sessions degrade client-side, no dialing) and SearchPanel ←
 *   SearchStore (full-text or filter-only degradation; a result click
 *   navigates the detail view to that session).
 *
 * The stores live in component state (one set per opened session id — the
 * owner keys this component by session id) and are disposed on unmount.
 * SSE coupling: the controller's subscribe seam notifies the DetailStore
 * on every state frame (header refresh + listen-mode refetch trigger).
 *
 * @module
 */

import { useEffect, useState, useSyncExternalStore } from 'react'
import type { ReactElement } from 'react'
import { SessionDetail } from './detail/SessionDetail.tsx'
import { LineageTree } from './dsh-tools/LineageTree.tsx'
import { SearchPanel } from './dsh-tools/SearchPanel.tsx'
import { AnalysisPanel } from './analysis/AnalysisPanel.tsx'
import { InjectPanel } from './inject/InjectPanel.tsx'
import { DetailStore, findCardHint, type DetailHeaderHint } from './detail-glue.ts'
import { SearchStore } from './search-glue.ts'
import { AnalysisStore } from './analysis-glue.ts'
import { ProjectsStore } from './project-glue.ts'
import type { SidecarController } from './controller.ts'
import { isDeliveredResult, type InjectMode } from './inject/logic.ts'
import type { InjectActions } from './inject-glue.ts'
import { t } from './locales/index.ts'
import css from './detail-view.module.css'
import overlay from './inject/overlay.module.css'

/**
 * Everything the integrated M3 surfaces need from the entry (index.ts
 * builds one; mount.tsx and this container share it). Store factories are
 * seams so tests/materialization can substitute injected transports.
 */
export interface SidecarUiIntegration {
  /** M2 injection wiring; absent = injection not assembled (entry hidden). */
  inject?: {
    actions: InjectActions
    getDefaultMode: () => InjectMode
  }
  /** Live `analysis.enabled` reader (settings scope box; default false). */
  getAnalysisEnabled: () => boolean
  createDetailStore: (sessionId: string, hint: DetailHeaderHint | null) => DetailStore
  createSearchStore: () => SearchStore
  createAnalysisStore: () => AnalysisStore
  createProjectsStore: () => ProjectsStore
}

/** Default integration over the real transports (analysis off until read). */
export function createDefaultIntegration(
  base: Omit<
    SidecarUiIntegration,
    'createDetailStore' | 'createSearchStore' | 'createAnalysisStore' | 'createProjectsStore'
  >,
): SidecarUiIntegration {
  return {
    ...base,
    createDetailStore: (sessionId, hint) => new DetailStore(sessionId, { hint }),
    createSearchStore: () => new SearchStore(),
    createAnalysisStore: () => new AnalysisStore(),
    createProjectsStore: () => new ProjectsStore(),
  }
}

export interface SidecarDetailViewProps {
  sessionId: string
  /** Header seed from the opening surface (board card / project row). */
  hint: DetailHeaderHint | null
  controller: SidecarController
  integration: SidecarUiIntegration
  onClose: () => void
  /** Provenance/search jump: navigate the detail view to another session. */
  onSelectSession: (sessionId: string) => void
}

/**
 * The detail view. Owner remounts it per session id (`key={sessionId}`),
 * so every store below is scoped to exactly one session.
 */
export function SidecarDetailView(props: SidecarDetailViewProps): ReactElement {
  const { controller, integration, sessionId } = props
  const [detailStore] = useState(() => integration.createDetailStore(sessionId, props.hint))
  const [searchStore] = useState(() => integration.createSearchStore())
  const [analysisStore] = useState(() => integration.createAnalysisStore())
  const [injectOpen, setInjectOpen] = useState(false)
  const [analysisOpen, setAnalysisOpen] = useState(false)
  const [toolsOpen, setToolsOpen] = useState(false)

  useEffect(() => {
    void detailStore.open()
    // One notification per SSE state frame / poll settle: header refresh
    // from the live board card + listen-mode newest-window refetch.
    const unsubscribe = controller.subscribe(() => {
      detailStore.notifySnapshot(findCardHint(controller.getState().sessions, sessionId))
    })
    return () => {
      unsubscribe()
      detailStore.dispose()
      searchStore.dispose()
      analysisStore.dispose()
    }
  }, [controller, detailStore, searchStore, analysisStore, sessionId])

  const detail = useSyncExternalStore(
    detailStore.subscribe, detailStore.getState, detailStore.getState)
  const search = useSyncExternalStore(
    searchStore.subscribe, searchStore.getState, searchStore.getState)
  const analysis = useSyncExternalStore(
    analysisStore.subscribe, analysisStore.getState, analysisStore.getState)
  const view = useSyncExternalStore(
    (cb) => controller.subscribe(cb),
    () => controller.getState(),
    () => controller.getState(),
  )

  const analysisEnabled = integration.getAnalysisEnabled()
  const injectIntegration = props.integration.inject
  const closeInject = (): void => { setInjectOpen(false) }
  const title = detail.header.title.trim()

  // UX-05 observation loop, part 1: a delivered execute refetches the
  // newest timeline window at once, so closing the panel never lands on a
  // stale pre-injection timeline. Wraps (not replaces) the integration
  // callback — the two-phase flow and the owner's onDelivered hook (board
  // snapshot refresh) stay untouched.
  const injectActions: InjectActions | undefined =
    injectIntegration === undefined
      ? undefined
      : {
          onPrepare: injectIntegration.actions.onPrepare,
          onExecute: async (req) => {
            const result = await injectIntegration.actions.onExecute(req)
            if (isDeliveredResult(result)) void detailStore.refreshNewest()
            return result
          },
        }

  // UX-05 part 2: the delivered result page offers「开启监听观察反应」—
  // flip listen mode on (if off) and hand the view back to the timeline.
  const observeReaction = (): void => {
    if (!detailStore.getState().listening) detailStore.toggleListen()
    closeInject()
  }

  return (
    <div className={css['detailRoot']} data-testid="agent-sidecar-detail-view">
      <div className={css['actionsRow']}>
        {injectIntegration !== undefined && (
          <button
            type="button"
            className={css['actionButton']}
            onClick={() => { setInjectOpen(true) }}
            data-testid="agent-sidecar-detail-inject"
          >
            {t('detail.actions.inject')}
          </button>
        )}
        <button
          type="button"
          className={css['actionButton']}
          disabled={!analysisEnabled}
          title={analysisEnabled ? undefined : t('detail.actions.analyzeDisabledHint')}
          onClick={() => { setAnalysisOpen(true) }}
          data-testid="agent-sidecar-detail-analyze"
        >
          {t('detail.actions.analyze')}
        </button>
      </div>

      {/* Collapsible dsh deep-query tools ABOVE the timeline (UX-09):
          discoverable without scrolling past a long event list. */}
      <div className={css['toolsSection']} data-testid="agent-sidecar-detail-tools">
        <button
          type="button"
          className={css['toolsToggle']}
          aria-expanded={toolsOpen}
          onClick={() => { setToolsOpen((open) => !open) }}
        >
          <span className={css['toolsToggleGlyph']} aria-hidden>
            {toolsOpen ? '▾' : '▸'}
          </span>
          {t('detail.tools.title')}
          <span className={css['toolsToggleGlyph']}>
            {toolsOpen ? t('detail.tools.hide') : t('detail.tools.show')}
          </span>
        </button>
        {toolsOpen && (
          <>
            <LineageTree
              trace={detail.lineage.trace}
              available={detail.lineage.available}
              reason={detail.lineage.reason}
              detail={detail.lineage.detail}
              currentSessionId={sessionId}
              onSelectSession={props.onSelectSession}
              loading={detail.lineage.loading}
              error={detail.lineage.error}
            />
            <SearchPanel
              query={search.query}
              project={search.project}
              mode={search.mode}
              items={search.items}
              loading={search.loading}
              error={search.error}
              onQueryChange={(query) => { searchStore.setQuery(query) }}
              onSubmit={() => { void searchStore.submit() }}
              onSelectSession={props.onSelectSession}
            />
          </>
        )}
      </div>

      <SessionDetail
        sessionId={sessionId}
        header={detail.header}
        timeline={detail.timeline}
        loading={detail.loading}
        error={detail.error}
        hasMore={detail.hasMore}
        listening={detail.listening}
        refreshing={detail.refreshing}
        onLoadMore={() => { void detailStore.loadMore() }}
        onToggleListen={() => { detailStore.toggleListen() }}
        onRefresh={() => { void detailStore.refreshNewest() }}
        onClose={props.onClose}
      />

      {analysisOpen && (
        <AnalysisPanel
          enabled={analysisEnabled}
          state={analysis}
          onStart={() => {
            void analysisStore.start({ targetKind: 'session', targetId: sessionId })
          }}
          onFollowup={(question) => { void analysisStore.followup(question) }}
          onStop={() => { void analysisStore.stop() }}
          onClose={() => { setAnalysisOpen(false) }}
        />
      )}

      {injectIntegration !== undefined && injectActions !== undefined && injectOpen && (
        <div className={overlay['backdrop']} role="presentation" onClick={closeInject}>
          <div
            className={overlay['dialog']}
            role="dialog"
            aria-modal="true"
            onClick={(event) => { event.stopPropagation() }}
          >
            <InjectPanel
              capability={{ inject: view.injectCapability }}
              target={{
                agent: detail.header.agent,
                sessionId,
                ...(title !== '' ? { title } : {}),
              }}
              defaultMode={injectIntegration.getDefaultMode()}
              onPrepare={injectActions.onPrepare}
              onExecute={injectActions.onExecute}
              onClose={closeInject}
              onObserve={observeReaction}
            />
          </div>
        </div>
      )}
    </div>
  )
}
