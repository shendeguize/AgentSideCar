/**
 * Session-detail React container. It composes the timeline, lineage,
 * search, analysis, and optional injection surfaces over stores supplied
 * by {@link DetailUiPort}.
 *
 * Stores are scoped to one opened session and disposed on unmount. Each
 * controller state frame refreshes the header hint and drives the
 * detail store's bounded listen-mode refetch.
 *
 * @module
 */

import { Button, Modal } from '@deepseek-ai/dsh-client-ui-primitives'
import { useEffect, useState, useSyncExternalStore } from 'react'
import type { ReactElement } from 'react'
import { SessionDetail } from './detail/SessionDetail.tsx'
import { LineageTree } from './dsh-tools/LineageTree.tsx'
import { SearchPanel } from './dsh-tools/SearchPanel.tsx'
import { AnalysisPanel } from './analysis/AnalysisPanel.tsx'
import { InjectPanel } from './inject/InjectPanel.tsx'
import { findCardHint, type DetailHeaderHint } from './detail-glue.ts'
import type { SidecarController } from './controller.ts'
import { isDeliveredResult } from './inject/logic.ts'
import type { InjectActions } from './inject-glue.ts'
import type {
  AnalysisStorePort,
  DetailStorePort,
  DetailUiPort,
  SearchStorePort,
} from './ui-integration.ts'
import { t } from './locales/index.ts'
import { surfaceProps } from './theme/parts.ts'
import css from './detail-view.module.css'

const ANALYSIS_DISABLED_REASON_ID = 'agent-sidecar-analysis-disabled-reason'

export interface SidecarDetailViewProps {
  sessionId: string
  /** Header seed from the opening surface (board card / project row). */
  hint: DetailHeaderHint | null
  controller: SidecarController
  integration: DetailUiPort
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
  const [detailStore] = useState<DetailStorePort>(
    () => integration.createDetailStore(sessionId, props.hint),
  )
  const [searchStore] = useState<SearchStorePort>(() => integration.createSearchStore())
  const [analysisStore] = useState<AnalysisStorePort>(() => integration.createAnalysisStore())
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
  const analysisDisabledHint =
    analysisEnabled ? undefined : t('detail.actions.analyzeDisabledHint')
  const injectIntegration = props.integration.inject
  const closeInject = (): void => { setInjectOpen(false) }
  const title = detail.header.title.trim()

  // A delivered execute refreshes the newest timeline while preserving
  // the integration-owned two-phase flow and delivery callback.
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

  const observeReaction = (): void => {
    if (!detailStore.getState().listening) detailStore.toggleListen()
    closeInject()
  }

  return (
    <div
      {...surfaceProps('detail', css['detailRoot'])}
      data-testid="agent-sidecar-detail-view"
    >
      <div className={css['actionsRow']}>
        {injectIntegration !== undefined && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => { setInjectOpen(true) }}
            data-testid="agent-sidecar-detail-inject"
          >
            {t('detail.actions.inject')}
          </Button>
        )}
        <Button
          size="sm"
          variant="outline"
          disabled={!analysisEnabled}
          title={analysisDisabledHint}
          aria-describedby={analysisEnabled ? undefined : ANALYSIS_DISABLED_REASON_ID}
          onClick={() => { setAnalysisOpen(true) }}
          data-testid="agent-sidecar-detail-analyze"
        >
          {t('detail.actions.analyze')}
        </Button>
        {!analysisEnabled && (
          <span
            id={ANALYSIS_DISABLED_REASON_ID}
            className={css['analysisDisabledReason']}
          >
            {analysisDisabledHint}
          </span>
        )}
      </div>

      <div
        {...surfaceProps('dsh-tools', css['toolsSection'])}
        data-testid="agent-sidecar-detail-tools"
      >
        <Button
          size="sm"
          variant="ghost"
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
        </Button>
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

      {injectIntegration !== undefined && injectActions !== undefined && (
        <Modal
          open={injectOpen}
          onClose={closeInject}
          title={t('inject.title')}
          closeLabel={t('inject.close')}
          className={css['injectDialog']}
          headless
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
        </Modal>
      )}
    </div>
  )
}
