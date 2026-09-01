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
import type { DaemonStateToken } from './board/logic.ts'
import type { SidecarController } from './controller.ts'
import {
  fetchSession,
  injectBlockReason,
  INVALID_INJECT_ELIGIBILITY,
  sessionInjectEligibility,
} from './api.ts'
import type {
  AbortSignalLike,
  DisposeOutcome,
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

/** The one agent whose sessions the host can actually end. */
const DISPOSABLE_AGENT = 'dsh'

/**
 * Copy for the outcomes worth reporting. `disposed` and `not_found` are
 * absent by design: both mean the session is gone, and the page closes
 * instead of narrating.
 */
const DISPOSE_FAILURE_COPY = {
  unsupported: 'detail.dispose.outcome.unsupported',
  timeout: 'detail.dispose.outcome.timeout',
  failed: 'detail.dispose.outcome.failed',
} as const

/** The failure copy for an outcome, or null when the session is gone. */
export function disposeFailureKey(
  outcome: DisposeOutcome,
): (typeof DISPOSE_FAILURE_COPY)[keyof typeof DISPOSE_FAILURE_COPY] | null {
  return outcome === 'disposed' || outcome === 'not_found'
    ? null
    : DISPOSE_FAILURE_COPY[outcome]
}

export interface DetailDisposeButtonProps {
  /** Observed agent of the open session; only `dsh` can be disposed. */
  agent: string
  /** Host `capabilities.dispose`: false hides the control entirely. */
  capable: boolean
  onDispose: () => Promise<DisposeOutcome>
  /** Called after the session was actually ended. */
  onDisposed: () => void
}

/**
 * Detail-page dispose: a confirm dialog in front of the only irreversible
 * action this plugin offers.
 *
 * It renders nothing at all unless both the agent and the host can support
 * it — a disabled "end session" button invites the operator to hunt for a
 * setting, while archiving (which is right for almost every idle session)
 * is already one click away on the board.
 */
export function DetailDisposeButton(props: DetailDisposeButtonProps): ReactElement | null {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [outcome, setOutcome] = useState<DisposeOutcome | null>(null)
  if (props.agent !== DISPOSABLE_AGENT || !props.capable) return null
  const failureKey = outcome === null ? null : disposeFailureKey(outcome)

  const run = (): void => {
    if (busy) return
    setBusy(true)
    setOutcome(null)
    props.onDispose().then(
      (result) => {
        setBusy(false)
        setOutcome(result)
        // `not_found` means someone else already ended it: the board is
        // stale either way, so both settled-gone outcomes close the dialog
        // and hand control back to the owner.
        if (result === 'disposed' || result === 'not_found') {
          setConfirming(false)
          props.onDisposed()
        }
      },
      () => {
        setBusy(false)
        setOutcome('failed')
      },
    )
  }

  return (
    <>
      <Button
        size="sm"
        variant="outline"
        title={t('detail.actions.disposeHint')}
        onClick={() => {
          setOutcome(null)
          setConfirming(true)
        }}
        data-testid="agent-sidecar-detail-dispose"
      >
        {t('detail.actions.dispose')}
      </Button>
      {failureKey !== null && (
        <span
          className={css['analysisDisabledReason']}
          role="alert"
          data-testid="agent-sidecar-detail-dispose-outcome"
        >
          {t(failureKey)}
        </span>
      )}
      <Modal
        open={confirming}
        onClose={() => { if (!busy) setConfirming(false) }}
        title={t('detail.dispose.title')}
        closeLabel={t('detail.dispose.cancel')}
        description={t('detail.dispose.explain')}
      >
        <div data-testid="agent-sidecar-detail-dispose-confirm">
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => { setConfirming(false) }}
          >
            {t('detail.dispose.cancel')}
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={busy}
            onClick={run}
            data-testid="agent-sidecar-detail-dispose-confirm-run"
          >
            {busy ? t('detail.dispose.disposing') : t('detail.dispose.confirm')}
          </Button>
        </div>
      </Modal>
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
  /**
   * Supervisor state, when the surface knows it. A dead daemon is the most
   * common cause of a dead timeline and the only one the operator can act
   * on, so it replaces the generic retry line with the actual fix.
   */
  daemonState?: DaemonStateToken
  children: ReactNode
}

/** Daemon states in which no history source can answer until it comes back. */
const DAEMON_DOWN_STATES = new Set<DaemonStateToken>(['failed', 'backoff', 'defer'])

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
  const daemonDown =
    props.daemonState !== undefined && DAEMON_DOWN_STATES.has(props.daemonState)
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
          <span data-testid="agent-sidecar-timeline-degraded-hint">
            {daemonDown
              ? t(
                  props.daemonState === 'defer'
                    ? 'detail.timeline.daemonDeferHint'
                    : 'detail.timeline.daemonDownHint',
                )
              : t('detail.timeline.degradedRetry')}
          </span>
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
  const disposePort = props.integration.dispose
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

  // Unknown-delivery check: the panel asks the transport directly rather
  // than reading this page's timeline, so a stale or filtered view cannot
  // turn a delivered message into a false "not found".
  const verifyProbe = integration.inject?.createVerifyProbe?.(sessionId)

  // The injection target is always the session this page shows, so
  // "show the target's timeline" means dismiss the panel over a refreshed
  // newest window.
  const inspectTarget = (): void => {
    void detailStore.refreshNewest()
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
        {disposePort !== undefined && (
          <DetailDisposeButton
            agent={detail.header.agent}
            capable={view.disposeCapability}
            onDispose={() => disposePort.dispose(sessionId)}
            // A disposed session is gone from the host but still in this
            // board frame; leaving the detail page open would show a
            // timeline that can never advance again.
            onDisposed={() => {
              void controller.refresh()
              props.onClose()
            }}
          />
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
        // Degradation is scoped to the timeline body, so the header — back
        // button, status, ids, transcript path — survives a dead timeline.
        // Back therefore lives in the header only; the boundary keeps its own
        // optional close for standalone use.
        timelineBoundary={(body) => (
          <TimelineAvailabilityBoundary
            health={detail.timelineHealth}
            entryCount={detail.timeline.entries.length}
            refreshing={detail.refreshing}
            onRefresh={() => { void detailStore.refreshNewest() }}
            daemonState={view.daemonState}
          >
            {body}
          </TimelineAvailabilityBoundary>
        )}
      />

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
            verifyProbe={verifyProbe}
            onOpenTarget={inspectTarget}
          />
        </Modal>
      )}
    </div>
  )
}
