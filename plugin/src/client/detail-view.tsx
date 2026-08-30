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
import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import type { ReactElement, ReactNode } from 'react'
import { SessionDetail } from './detail/SessionDetail.tsx'
import { LineageTree } from './dsh-tools/LineageTree.tsx'
import { SearchPanel } from './dsh-tools/SearchPanel.tsx'
import { InjectPanel, injectErrorCopy } from './inject/InjectPanel.tsx'
import { findCardHint, type DetailHeaderHint } from './detail-glue.ts'
import type { AnalysisTarget } from './analysis-glue.ts'
import type { SidecarController } from './controller.ts'
import {
  fetchSession,
  injectBlockReason,
  INVALID_INJECT_ELIGIBILITY,
  sessionInjectEligibility,
} from './api.ts'
import type {
  AbortSignalLike,
  InjectBlockReason,
  InjectEligibility,
  SessionDetail as SessionDetailWire,
  TimelineHealth,
} from './api.ts'
import { isDeliveredResult } from './inject/logic.ts'
import type { InjectActions } from './inject-glue.ts'
import type {
  DetailStorePort,
  DetailUiPort,
  SearchStorePort,
} from './ui-integration.ts'
import { t } from './locales/index.ts'
import { surfaceProps } from './theme/parts.ts'
import css from './detail-view.module.css'

const ANALYSIS_DISABLED_REASON_ID = 'agent-sidecar-analysis-disabled-reason'

type FetchSessionForEligibility = (
  sessionId: string,
  opts?: { signal?: AbortSignalLike },
) => Promise<SessionDetailWire>

export interface InjectEligibilityRefresher {
  refresh(): Promise<void>
  dispose(): void
}

/**
 * Coalesce target-verdict refreshes driven by the existing detail/SSE
 * notification stream. There is deliberately no timer or independent poll.
 */
export function createInjectEligibilityRefresher(
  sessionId: string,
  getExpectedAgent: () => string,
  onEligibility: (eligibility: InjectEligibility) => void,
  fetchSessionFn: FetchSessionForEligibility = fetchSession,
): InjectEligibilityRefresher {
  let disposed = false
  let generation = 0
  let activeController: AbortController | null = null

  return {
    refresh: () => {
      if (disposed) return Promise.resolve()
      const requestGeneration = ++generation
      activeController?.abort()
      const controller = new AbortController()
      activeController = controller
      const expectedAgent = getExpectedAgent()
      // Close synchronously on every live notification. The prior allow
      // verdict must not survive while the matching detail verdict is being
      // re-read (for example waiting → working on an external target).
      onEligibility(INVALID_INJECT_ELIGIBILITY)
      const request = (async (): Promise<void> => {
        let eligibility = INVALID_INJECT_ELIGIBILITY
        try {
          const wire = await fetchSessionFn(sessionId, { signal: controller.signal })
          eligibility = sessionInjectEligibility(wire.session, {
            sessionId,
            ...(expectedAgent === '' ? {} : { agent: expectedAgent }),
          })
        } catch {
          // A missing/failed legacy detail response cannot authorize injection.
        }
        // Aborted transports are not guaranteed to reject promptly (or at
        // all), so generation and target identity remain the authority.
        if (
          disposed ||
          controller.signal.aborted ||
          requestGeneration !== generation ||
          getExpectedAgent() !== expectedAgent
        ) return
        onEligibility(eligibility)
      })()
      return request.finally(() => {
        if (activeController === controller) activeController = null
      })
    },
    dispose: () => {
      disposed = true
      generation += 1
      activeController?.abort()
      activeController = null
    },
  }
}

/** Guard retained even behind native `disabled` for synthetic activation. */
export function requestInjectOpen(
  reason: InjectBlockReason | null,
  onOpen: () => void,
): boolean {
  if (reason !== null) return false
  onOpen()
  return true
}

export interface DetailInjectTriggerProps {
  injectEnabled: boolean
  eligibility: InjectEligibility | null | undefined
  onOpen: () => void
}

/** Native-disabled, visibly explained detail-page injection trigger. */
export function DetailInjectTrigger(props: DetailInjectTriggerProps): ReactElement {
  const reasonId = useId()
  const reason = injectBlockReason(props.injectEnabled, props.eligibility)
  const hint = reason === null ? undefined : t(injectErrorCopy(reason).key)
  return (
    <>
      <Button
        size="sm"
        variant="outline"
        disabled={reason !== null}
        title={hint}
        aria-describedby={reason === null ? undefined : reasonId}
        onClick={() => { requestInjectOpen(reason, props.onOpen) }}
        data-testid="agent-sidecar-detail-inject"
      >
        {t('detail.actions.inject')}
      </Button>
      {reason !== null && (
        <span
          id={reasonId}
          className={css['analysisDisabledReason']}
          data-testid="agent-sidecar-detail-inject-reason"
        >
          {hint}
        </span>
      )}
    </>
  )
}

export interface SidecarDetailViewProps {
  sessionId: string
  /** Header seed from the opening surface (board card / project row). */
  hint: DetailHeaderHint | null
  controller: SidecarController
  integration: DetailUiPort
  onClose: () => void
  /** Open the tab-scoped full-page analysis route for this session. */
  onAnalyze: (target: AnalysisTarget) => void
  /** Provenance/search jump: navigate the detail view to another session. */
  onSelectSession: (sessionId: string) => void
}

export interface TimelineAvailabilityBoundaryProps {
  health: TimelineHealth
  entryCount: number
  refreshing: boolean
  onRefresh: () => void
  onClose?: () => void
  children: ReactNode
}

/**
 * Content-free timeline degradation surface. A degraded empty page replaces
 * the healthy-empty body; partial failures with entries keep those entries
 * visible. The existing manual newest-window refresh is reused as retry.
 */
export function TimelineAvailabilityBoundary(
  props: TimelineAvailabilityBoundaryProps,
): ReactElement {
  const { health } = props
  const degraded = health.kind !== 'healthy'
  const blocksHealthyEmpty = degraded && props.entryCount === 0
  const messageKey =
    health.kind === 'partial'
      ? 'detail.timeline.degradedPartial'
      : health.kind === 'failed'
        ? 'detail.timeline.degradedAll'
        : 'detail.timeline.degradedUnverified'

  return (
    <>
      {degraded && (
        <div
          className={css['toolsSection']}
          role={health.kind === 'partial' ? 'status' : 'alert'}
          data-kind={health.kind}
          data-testid="agent-sidecar-timeline-degraded"
        >
          <span>{t(messageKey)}</span>
          {health.summary !== null && (
            <span data-testid="agent-sidecar-timeline-source-summary">
              {t('detail.sources.healthSummary', { ...health.summary })}
            </span>
          )}
          <span>{t('detail.timeline.degradedRetry')}</span>
          {blocksHealthyEmpty && (
            <span>
              {props.onClose !== undefined && (
                <Button type="button" size="sm" variant="outline" onClick={props.onClose}>
                  {t('detail.header.close')}
                </Button>
              )}
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={props.refreshing}
                title={t('detail.header.refreshHint')}
                onClick={props.onRefresh}
                data-testid="agent-sidecar-timeline-retry"
              >
                {props.refreshing
                  ? t('detail.header.refreshing')
                  : t('detail.header.refresh')}
              </Button>
            </span>
          )}
        </div>
      )}
      {!blocksHealthyEmpty && props.children}
    </>
  )
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
  const [injectOpen, setInjectOpen] = useState(false)
  const [injectEligibility, setInjectEligibility] =
    useState<InjectEligibility>(INVALID_INJECT_ELIGIBILITY)
  const [toolsOpen, setToolsOpen] = useState(false)
  const detailRootRef = useRef<HTMLDivElement | null>(null)
  const focusFrameRef = useRef<number | null>(null)

  useEffect(() => {
    const eligibilityRefresher = createInjectEligibilityRefresher(
      sessionId,
      () => detailStore.getState().header.agent,
      setInjectEligibility,
    )
    void detailStore.open()
    void eligibilityRefresher.refresh()
    // One notification per SSE state frame / poll settle: header refresh
    // from the live board card + listen-mode newest-window refetch.
    const unsubscribe = controller.subscribe(() => {
      detailStore.notifySnapshot(findCardHint(controller.getState().sessions, sessionId))
      void eligibilityRefresher.refresh()
    })
    return () => {
      unsubscribe()
      eligibilityRefresher.dispose()
      detailStore.dispose()
      searchStore.dispose()
    }
  }, [controller, detailStore, searchStore, sessionId])

  useEffect(() => {
    if (typeof window === 'undefined') return
    const root = detailRootRef.current
    if (root === null) return
    focusFrameRef.current = window.requestAnimationFrame(() => {
      focusFrameRef.current = null
      if (detailRootRef.current !== root || !root.isConnected) return
      // SessionDetail always renders Back as the first enabled header
      // control when onClose is supplied. Keep focus off the outer dialog
      // and detail body while avoiding a raw-session-derived DOM id.
      const back = root.querySelector<HTMLElement>(
        '[data-testid="agent-sidecar-detail"] header button:not([disabled])',
      )
      const heading = root.querySelector<HTMLElement>('h1,[role="heading"]')
      ;(back ?? heading)?.focus({ preventScroll: true })
    })
    return () => {
      if (focusFrameRef.current === null) return
      window.cancelAnimationFrame(focusFrameRef.current)
      focusFrameRef.current = null
    }
  }, [sessionId])

  const detail = useSyncExternalStore(
    detailStore.subscribe, detailStore.getState, detailStore.getState)
  const search = useSyncExternalStore(
    searchStore.subscribe, searchStore.getState, searchStore.getState)
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
      ref={detailRootRef}
      data-testid="agent-sidecar-detail-view"
    >
      <div className={css['actionsRow']}>
        {injectIntegration !== undefined && (
          <DetailInjectTrigger
            injectEnabled={view.injectCapability}
            eligibility={injectEligibility}
            onOpen={() => { setInjectOpen(true) }}
          />
        )}
        <Button
          size="sm"
          variant="outline"
          disabled={!analysisEnabled}
          title={analysisDisabledHint}
          aria-describedby={analysisEnabled ? undefined : ANALYSIS_DISABLED_REASON_ID}
          onClick={() => {
            props.onAnalyze({ targetKind: 'session', targetId: sessionId })
          }}
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

      <TimelineAvailabilityBoundary
        health={detail.timelineHealth}
        entryCount={detail.timeline.entries.length}
        refreshing={detail.refreshing}
        onRefresh={() => { void detailStore.refreshNewest() }}
        onClose={props.onClose}
      >
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
      </TimelineAvailabilityBoundary>

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
            eligibility={injectEligibility}
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
