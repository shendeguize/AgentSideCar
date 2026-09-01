/**
 * Cross-agent session board (design §5.1 view 1).
 *
 * Presentation-only: no data fetching, no api/sse imports. Everything
 * arrives through props already shaped as the view models of `logic.ts`;
 * the integration layer (T2.4) owns state, transport, and the mapping
 * from the host wire types (epoch-seconds → epoch-ms conversion included).
 *
 * Interaction surface handed back to the owner:
 * - `onFiltersChange` — time-window select / show-dead checkbox / the
 *   top-bar status-filter badges (controlled, UX-01);
 * - `onRefresh`      — manual snapshot pull button; a Promise<boolean>
 *   return drives the in-flight/failure feedback (UX-07);
 * - `onSelectSession` — card click, pass-through for the M3 detail view.
 *
 * Local UI state (deliberately NOT lifted into the controller stores):
 * group collapse and per-group truncation (UX-02) are ephemeral view
 * concerns — useState here, reset on tab remount.
 */

import { Button, Pill, StateDot, type StateDotState } from '@deepseek-ai/dsh-client-ui-primitives'
import { useEffect, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import {
  agentDisplayName,
  agentFilterOptions,
  buildBoardViewModel,
  clusterSessions,
  formatTemplate,
  normalizeAgentFilter,
  partitionIdleCards,
  sliceCardsForDisplay,
  timeWindowLabel,
  withAgentFilter,
  GROUP_CARD_LIMIT,
  type ArchivedCardVM,
  type BoardFilterState,
  type BoardStatusFilter,
  type DaemonStateToken,
  type DerivedSessionCardVM,
  type ProjectGroupVM,
  type SessionCardVM,
  type StreamHealthToken,
} from './logic.ts'
import { ArchiveDialog, ArchivedSection, type BoardArchiveApi } from './ArchivePanel.tsx'
import type { AnalysisTarget } from '../analysis-glue.ts'
import { BOARD_STRINGS } from './strings.ts'
import { t } from '../locales/index.ts'
import { surfaceProps } from '../theme/parts.ts'
import styles from './board.module.css'

/** Time-window choices offered by the top bar (hours). */
export const TIME_WINDOW_OPTIONS: readonly number[] = [6, 12, 24, 48, 168]

/** In-memory identity used to restore focus without serializing it into DOM attributes. */
export interface SessionFocusTarget {
  agent: string
  sessionId: string
}

/** Exact identity comparison; session ids are only unique within an agent. */
export function matchesSessionFocusTarget(
  candidate: SessionFocusTarget,
  target: SessionFocusTarget | null,
): boolean {
  return target !== null
    && candidate.agent === target.agent
    && candidate.sessionId === target.sessionId
}

export interface BoardProps {
  daemonState: DaemonStateToken
  /** Optional raw detail for the daemon badge hover (e.g. "pid 123 · v0.6.0"). */
  daemonDetail?: string
  streamHealth: StreamHealthToken
  /** Epoch ms of the last authoritative snapshot reconcile, or null. */
  lastReconcileAtMs: number | null
  /** False only while the first snapshot/error outcome is still pending. */
  hasSnapshot: boolean
  /** The initial load settled without a snapshot; retry remains available. */
  initialLoadFailed: boolean
  sessions: SessionCardVM[]
  /** Controlled filter state (owner persists it to the settings namespace). */
  filters: BoardFilterState
  onFiltersChange: (next: BoardFilterState) => void
  /**
   * Manual snapshot pull. A `Promise<boolean>` return (true = snapshot
   * applied) lets the board render the in-flight state and a dismissible
   * failure notice; a void return keeps the button fire-and-forget.
   */
  onRefresh: () => void | Promise<boolean>
  onSelectSession: (target: SessionFocusTarget) => void
  onAnalyze?: (target: AnalysisTarget) => void
  /**
   * Batch-archive round-trips. Absent on hosts whose daemon predates the
   * archive ops, in which case the entry point is not rendered at all
   * (a disabled button that never explains itself is worse than no button).
   */
  archive?: BoardArchiveApi
  /** Sessions the daemon is currently hiding; empty when nothing is archived. */
  archived?: readonly ArchivedCardVM[]
  /** True while at least one dsh session could accept a real dispose. */
  disposeSupported?: boolean
  /** Composite in-memory identity awaiting return-focus restoration. */
  returnFocusTarget: SessionFocusTarget | null
  /** Called only after the matching card or fallback heading has been focused. */
  onReturnFocusConsumed: () => void
  /** Current mounted scroll container; null on route/view unmount. */
  rootRef: (element: HTMLDivElement | null) => void
  /** Persist this view's independent scroll position in the route owner. */
  onScrollTopChange: (scrollTop: number) => void
  /** Clock injection for deterministic rendering; defaults to Date.now(). */
  nowMs?: number
}

function sessionDotState(status: DerivedSessionCardVM['badge']['status']): StateDotState | null {
  if (status === 'working') return 'ongoing'
  if (status === 'waiting') return 'warning'
  return null
}

function SessionCard(props: {
  card: DerivedSessionCardVM
  onSelect: (target: SessionFocusTarget) => void
  returnFocusTarget: SessionFocusTarget | null
  onReturnFocusConsumed: () => void
}): ReactElement {
  const { card, onSelect } = props
  const [copied, setCopied] = useState(false)
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const copyAliveRef = useRef(true)
  const openerRef = useRef<HTMLButtonElement>(null)
  const dotState = sessionDotState(card.badge.status)
  const isReturnFocusTarget = matchesSessionFocusTarget(card, props.returnFocusTarget)

  useEffect(() => {
    copyAliveRef.current = true
    return () => {
      copyAliveRef.current = false
      if (copyTimerRef.current !== null) {
        clearTimeout(copyTimerRef.current)
        copyTimerRef.current = null
      }
    }
  }, [])

  useEffect(() => {
    if (!isReturnFocusTarget) return
    const opener = openerRef.current
    if (opener === null) return
    opener.focus({ preventScroll: true })
    props.onReturnFocusConsumed()
  }, [isReturnFocusTarget, props.onReturnFocusConsumed])

  // UX-17: opening and copying are sibling buttons inside a semantic card,
  // so both actions are independently keyboard reachable.
  const onCopyId = (): void => {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clipboard === undefined) return
    clipboard.writeText(card.sessionId).then(
      () => {
        if (!copyAliveRef.current) return
        if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current)
        setCopied(true)
        copyTimerRef.current = setTimeout(() => {
          copyTimerRef.current = null
          setCopied(false)
        }, 2000)
      },
      () => {
        // Clipboard permission denied: the hover title still carries the id.
      },
    )
  }

  return (
    <article
      {...surfaceProps('board-card', styles['card'])}
      data-testid="agent-sidecar-card"
    >
      <button
        ref={openerRef}
        type="button"
        className={styles['cardOpen']}
        onClick={() => onSelect({ agent: card.agent, sessionId: card.sessionId })}
      >
        <span className={styles['cardHead']}>
          <span className={styles['agent']}>
            <span className={styles['glyph']} aria-hidden>
              {card.glyph}
            </span>
            {card.agent}
          </span>
          <span title={card.hoverTitle}>
            <Pill className={styles['statusPill']}>
              {dotState === null
                ? <span className={styles['dot']} data-tone={card.badge.tone} />
                : <StateDot state={dotState} size={8} />}
              {card.badge.label}
              {card.badge.attention !== null && (
                <span className={styles['attention']} data-kind={card.badge.attention}>
                  {card.badge.attentionLabel}
                </span>
              )}
            </Pill>
          </span>
        </span>
        <span className={styles['cardTitle']} title={card.title}>
          {card.title.trim() === '' ? BOARD_STRINGS.card.untitled : card.title}
        </span>
      </button>
      <Button
        size="sm"
        variant="ghost"
        className={styles['cardId']}
        title={`${card.sessionId}\n${BOARD_STRINGS.card.copyId}`}
        onClick={onCopyId}
        data-testid="agent-sidecar-card-id"
      >
        {card.shortId}
        {copied && <span className={styles['copied']} role="status">{BOARD_STRINGS.card.copied}</span>}
      </Button>
      <div className={styles['cardEvent']}>
        {card.lastEvent === null
          ? BOARD_STRINGS.card.noEvent
          : `${card.lastEvent.kind} · ${card.lastEvent.text}`}
      </div>
      <div className={styles['cardTime']}>{card.relativeTime}</div>
    </article>
  )
}

function ProjectGroup(props: {
  group: ProjectGroupVM<DerivedSessionCardVM>
  onSelect: (target: SessionFocusTarget) => void
  collapseIdle: boolean
  returnFocusTarget: SessionFocusTarget | null
  onReturnFocusConsumed: () => void
}): ReactElement {
  const { group, onSelect } = props
  // UX-02: collapse + truncation are per-group ephemeral view state.
  const [collapsed, setCollapsed] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const [idleExpanded, setIdleExpanded] = useState(false)
  const idlePartition = partitionIdleCards(group.cards)
  const displayCards = props.collapseIdle ? idlePartition.active : group.cards
  const returnTargetIndex = props.returnFocusTarget === null
    ? -1
    : group.cards.findIndex((card) => matchesSessionFocusTarget(card, props.returnFocusTarget))
  useEffect(() => {
    if (returnTargetIndex >= GROUP_CARD_LIMIT && !expanded) setExpanded(true)
  }, [expanded, returnTargetIndex])
  useEffect(() => {
    if (
      props.collapseIdle &&
      props.returnFocusTarget !== null &&
      idlePartition.idle.some((card) => matchesSessionFocusTarget(card, props.returnFocusTarget))
    ) {
      setIdleExpanded(true)
    }
  }, [idlePartition.idle, props.collapseIdle, props.returnFocusTarget])
  const { shown, hiddenCount } = sliceCardsForDisplay(displayCards, GROUP_CARD_LIMIT, expanded)
  // Honesty guard: a collapsed group must not silently hide waiting
  // sessions, so the header keeps a waiting counter while folded.
  const waitingInGroup = group.cards.filter((card) => card.badge.status === 'waiting').length
  return (
    <section className={styles['group']}>
      <button
        type="button"
        className={styles['groupHead']}
        aria-expanded={!collapsed}
        title={collapsed ? BOARD_STRINGS.group.expandTitle : BOARD_STRINGS.group.collapseTitle}
        onClick={() => { setCollapsed(!collapsed) }}
      >
        <span className={styles['chevron']} aria-hidden>
          {collapsed ? '▸' : '▾'}
        </span>
        <span
          className={styles['groupName']}
          title={group.fullPath === '' ? undefined : group.fullPath}
        >
          {group.label}
        </span>
        <span className={styles['groupCount']}>
          {formatTemplate(BOARD_STRINGS.groupCount, { n: group.cards.length })}
        </span>
        {collapsed && waitingInGroup > 0 && (
          <span className={styles['groupAttention']}>
            {formatTemplate(BOARD_STRINGS.topbar.countWaiting, { n: waitingInGroup })}
          </span>
        )}
      </button>
      {!collapsed && (
        <>
          <div className={styles['grid']}>
            {shown.map((card) => (
              <SessionCard
                key={`${card.agent}:${card.sessionId}`}
                card={card}
                onSelect={onSelect}
                returnFocusTarget={props.returnFocusTarget}
                onReturnFocusConsumed={props.onReturnFocusConsumed}
              />
            ))}
          </div>
          {props.collapseIdle && idlePartition.idle.length > 0 && (
            <>
              <button
                type="button"
                className={styles['idleSummary']}
                aria-expanded={idleExpanded}
                title={idleExpanded
                  ? BOARD_STRINGS.group.collapseIdle
                  : BOARD_STRINGS.group.expandIdle}
                onClick={() => { setIdleExpanded((open) => !open) }}
                data-testid="agent-sidecar-idle-summary"
              >
                <span className={styles['chevron']} aria-hidden>
                  {idleExpanded ? '▾' : '▸'}
                </span>
                {formatTemplate(BOARD_STRINGS.group.idleSummary, {
                  n: idlePartition.idle.length,
                })}
              </button>
              {idleExpanded && (
                <div className={styles['grid']}>
                  {idlePartition.idle.map((card) => (
                    <SessionCard
                      key={`${card.agent}:${card.sessionId}`}
                      card={card}
                      onSelect={onSelect}
                      returnFocusTarget={props.returnFocusTarget}
                      onReturnFocusConsumed={props.onReturnFocusConsumed}
                    />
                  ))}
                </div>
              )}
            </>
          )}
          {hiddenCount > 0 && (
            <Button
              size="sm"
              variant="outline"
              className={styles['showMore']}
              onClick={() => { setExpanded(true) }}
            >
              {formatTemplate(BOARD_STRINGS.group.showAll, { n: group.cards.length })}
            </Button>
          )}
          {expanded && group.cards.length > GROUP_CARD_LIMIT && (
            <Button
              size="sm"
              variant="outline"
              className={styles['showMore']}
              onClick={() => { setExpanded(false) }}
            >
              {formatTemplate(BOARD_STRINGS.group.showLess, { n: GROUP_CARD_LIMIT })}
            </Button>
          )}
        </>
      )}
    </section>
  )
}

/** The board view. Pure render of `buildBoardViewModel` over the props. */
export function Board(props: BoardProps): ReactElement {
  const nowMs = props.nowMs ?? Date.now()
  const vm = buildBoardViewModel({
    sessions: props.sessions,
    filters: props.filters,
    daemonState: props.daemonState,
    streamHealth: props.streamHealth,
    lastReconcileAtMs: props.lastReconcileAtMs,
    nowMs,
  })
  // Keep the select renderable even when the persisted setting is not one
  // of the stock options (e.g. a hand-edited settings.yaml value).
  const windowOptions = TIME_WINDOW_OPTIONS.includes(props.filters.timeWindowHours)
    ? TIME_WINDOW_OPTIONS
    : [...TIME_WINDOW_OPTIONS, props.filters.timeWindowHours].sort((a, b) => a - b)
  const selectedAgent = normalizeAgentFilter(props.filters.agentFilter)
  const availableAgents = agentFilterOptions(props.sessions, props.filters.agentFilter)
  const [showClusters, setShowClusters] = useState(false)
  const clusters = clusterSessions(vm.groups.flatMap((group) => group.cards))
  const fallbackFocusRef = useRef<HTMLSpanElement>(null)
  const returnTargetVisible = props.returnFocusTarget !== null
    && vm.groups.some((group) =>
      group.cards.some((card) => matchesSessionFocusTarget(card, props.returnFocusTarget)),
    )

  // UX-07: manual-refresh feedback (in-flight + dismissible failure line).
  const [refreshing, setRefreshing] = useState(false)
  const [refreshFailed, setRefreshFailed] = useState(false)
  const [archiveOpen, setArchiveOpen] = useState(false)
  const archivedRows = props.archived ?? []
  const onRefreshClick = (): void => {
    if (refreshing) return
    setRefreshFailed(false)
    const result = props.onRefresh()
    if (result instanceof Promise) {
      setRefreshing(true)
      result
        .then((ok) => { setRefreshFailed(!ok) })
        .catch(() => { setRefreshFailed(true) })
        .finally(() => { setRefreshing(false) })
    }
  }

  // UX-01: the working/waiting badges toggle the status-only view.
  const toggleStatusFilter = (status: BoardStatusFilter): void => {
    const next: BoardFilterState = { ...props.filters }
    if (next.statusFilter === status) delete next.statusFilter
    else next.statusFilter = status
    props.onFiltersChange(next)
  }
  const statusBadgeTitle = (status: BoardStatusFilter): string =>
    props.filters.statusFilter === status
      ? BOARD_STRINGS.topbar.clearStatusFilterTitle
      : formatTemplate(BOARD_STRINGS.topbar.filterByStatusTitle, {
          label: BOARD_STRINGS.status[status],
        })
  const daemonDotState: StateDotState =
    props.daemonState === 'failed'
      ? 'error'
      : props.daemonState === 'defer' || props.daemonState === 'backoff'
        ? 'warning'
        : props.daemonState === 'adopted' || props.daemonState === 'hosted'
          ? 'done'
          : 'ongoing'
  const streamDotState: StateDotState =
    props.streamHealth === 'ok' ? 'done' : props.streamHealth === 'degraded' ? 'warning' : 'ongoing'

  useEffect(() => {
    if (props.returnFocusTarget === null || returnTargetVisible) return
    const fallback = fallbackFocusRef.current
    if (fallback === null) return
    fallback.focus({ preventScroll: true })
    props.onReturnFocusConsumed()
  }, [props.onReturnFocusConsumed, props.returnFocusTarget, returnTargetVisible])

  return (
    <div
      {...surfaceProps('board', styles['root'])}
      ref={props.rootRef}
      onScroll={(event) => { props.onScrollTopChange(event.currentTarget.scrollTop) }}
      data-testid="agent-sidecar-board"
      aria-busy={!props.hasSnapshot}
    >
      <header {...surfaceProps('board-toolbar', styles['topbar'])}>
        <span
          ref={fallbackFocusRef}
          className={styles['title']}
          role="heading"
          aria-level={1}
          tabIndex={-1}
        >
          {BOARD_STRINGS.topbar.title}
        </span>
        <span title={props.daemonDetail}>
          <Pill>
            <StateDot state={daemonDotState} size={8} />
            {vm.daemonBadge.label}
          </Pill>
        </span>
        <Pill>
          <StateDot state={streamDotState} size={8} />
          {vm.streamLabel}
        </Pill>
        <Pill
          className={styles['countBadge']}
          active={props.filters.statusFilter === 'working'}
          aria-pressed={props.filters.statusFilter === 'working'}
          title={statusBadgeTitle('working')}
          onClick={() => { toggleStatusFilter('working') }}
          data-testid="agent-sidecar-count-working"
        >
          {vm.workingCount > 0
            ? <StateDot state="ongoing" size={8} />
            : <span className={styles['dot']} data-tone="neutral" />}
          {formatTemplate(BOARD_STRINGS.topbar.countWorking, { n: vm.workingCount })}
        </Pill>
        <Pill
          className={styles['countBadge']}
          active={props.filters.statusFilter === 'waiting'}
          aria-pressed={props.filters.statusFilter === 'waiting'}
          title={statusBadgeTitle('waiting')}
          onClick={() => { toggleStatusFilter('waiting') }}
          data-testid="agent-sidecar-count-waiting"
        >
          {vm.waitingCount > 0
            ? <StateDot state="warning" size={8} />
            : <span className={styles['dot']} data-tone="neutral" />}
          {formatTemplate(BOARD_STRINGS.topbar.countWaiting, { n: vm.waitingCount })}
        </Pill>
        <span className={styles['countTotal']} data-testid="agent-sidecar-count-total">
          {formatTemplate(BOARD_STRINGS.topbar.countTotal, { n: vm.totalCount })}
        </span>
        <span className={styles['spacer']} />
        {props.onAnalyze !== undefined && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => { props.onAnalyze?.({ targetKind: 'cross-agent' }) }}
            data-testid="agent-sidecar-analyze-cross-agent"
          >
            {BOARD_STRINGS.topbar.analyzeCrossAgent}
          </Button>
        )}
        {props.archive !== undefined && (
          <Button
            size="sm"
            variant="outline"
            title={BOARD_STRINGS.archive.openTitle}
            onClick={() => { setArchiveOpen(true) }}
            data-testid="agent-sidecar-archive-open"
          >
            {BOARD_STRINGS.archive.open}
          </Button>
        )}
        <Button
          size="sm"
          variant="outline"
          onClick={() => { setShowClusters((visible) => !visible) }}
          aria-pressed={showClusters}
          data-testid="agent-sidecar-cluster-toggle"
        >
          {BOARD_STRINGS.topbar.cluster}
        </Button>
        <label className={styles['control']}>
          {t('board.topbar.agentFilter')}
          <select
            className={styles['select']}
            aria-label={t('board.topbar.agentFilterAria')}
            value={selectedAgent ?? ''}
            onChange={(ev) => {
              props.onFiltersChange(withAgentFilter(props.filters, ev.target.value))
            }}
            data-testid="agent-sidecar-agent-filter"
          >
            <option value="">{t('board.topbar.allAgents')}</option>
            {availableAgents.map((agent) => (
              <option key={agent} value={agent}>
                {agentDisplayName(agent)}
              </option>
            ))}
          </select>
        </label>
        <label className={styles['control']}>
          {BOARD_STRINGS.topbar.timeWindow}
          <select
            className={styles['select']}
            value={String(props.filters.timeWindowHours)}
            onChange={(ev) =>
              props.onFiltersChange({
                ...props.filters,
                timeWindowHours: Number(ev.target.value),
              })
            }
          >
            {windowOptions.map((hours) => (
              <option key={hours} value={String(hours)}>
                {timeWindowLabel(hours)}
              </option>
            ))}
          </select>
        </label>
        <label className={styles['control']}>
          <input
            type="checkbox"
            className={styles['checkbox']}
            checked={props.filters.showDead}
            onChange={(ev) =>
              props.onFiltersChange({ ...props.filters, showDead: ev.target.checked })
            }
          />
          {BOARD_STRINGS.topbar.showDead}
        </label>
        <label className={styles['control']}>
          <input
            type="checkbox"
            className={styles['checkbox']}
            checked={props.filters.collapseIdle === true}
            title={BOARD_STRINGS.topbar.collapseIdleTitle}
            onChange={(ev) =>
              props.onFiltersChange({ ...props.filters, collapseIdle: ev.target.checked })
            }
            data-testid="agent-sidecar-collapse-idle"
          />
          {BOARD_STRINGS.topbar.collapseIdle}
        </label>
        <Button
          size="sm"
          variant="toolbar"
          title={BOARD_STRINGS.topbar.refreshTitle}
          disabled={refreshing}
          onClick={onRefreshClick}
        >
          {refreshing ? BOARD_STRINGS.topbar.refreshing : BOARD_STRINGS.topbar.refresh}
        </Button>
      </header>

      {props.hasSnapshot && !props.initialLoadFailed && refreshFailed && (
        <div className={styles['banner']} data-tone="warn" role="status">
          {BOARD_STRINGS.topbar.refreshFailed}
          <Button
            size="sm"
            variant="ghost"
            className={styles['bannerDismiss']}
            onClick={() => { setRefreshFailed(false) }}
          >
            {BOARD_STRINGS.topbar.dismiss}
          </Button>
        </div>
      )}

      {props.hasSnapshot && vm.banner !== null && (
        <div className={styles['banner']} data-tone={vm.banner.tone} role="status">
          {vm.banner.text}
        </div>
      )}

      {showClusters && (
        <section className={styles['clusterPanel']} data-testid="agent-sidecar-clusters">
          <header className={styles['clusterHead']}>
            <span className={styles['clusterTitle']}>{BOARD_STRINGS.cluster.title}</span>
            <span className={styles['groupCount']}>
              {formatTemplate(BOARD_STRINGS.cluster.count, { n: clusters.length })}
            </span>
          </header>
          {clusters.length === 0
            ? <div className={styles['clusterEmpty']}>{BOARD_STRINGS.cluster.empty}</div>
            : (
              <div className={styles['clusterList']}>
                {clusters.map((cluster) => (
                  <article className={styles['clusterCard']} key={cluster.key}>
                    <div className={styles['clusterSummary']}>
                      <strong>{cluster.project}</strong>
                      <span>{cluster.agent}</span>
                      <span>{cluster.model}</span>
                      <span>
                        {formatTemplate(BOARD_STRINGS.cluster.sessions, { n: cluster.count })}
                      </span>
                    </div>
                    <div className={styles['clusterMeta']}>
                      {cluster.modelProvider !== 'unknown'
                        ? `${cluster.modelProvider} · `
                        : ''}
                      {cluster.sessionIds.join(', ')}
                    </div>
                  </article>
                ))}
              </div>
            )}
        </section>
      )}

      {props.archive !== undefined && archiveOpen && (
        <ArchiveDialog
          open={archiveOpen}
          onClose={() => { setArchiveOpen(false) }}
          api={props.archive}
          disposeSupported={props.disposeSupported === true}
          onArchived={() => { void props.onRefresh() }}
          nowMs={nowMs}
        />
      )}

      {props.archive !== undefined && (
        <ArchivedSection
          rows={archivedRows}
          onUnarchive={props.archive.unarchive}
          onRestored={() => { void props.onRefresh() }}
          nowMs={nowMs}
        />
      )}

      {!props.hasSnapshot ? (
        <div
          className={styles['empty']}
          role="status"
          data-testid="agent-sidecar-board-loading"
        >
          {t('board.states.loading')}
        </div>
      ) : props.initialLoadFailed ? (
        <div className={styles['empty']} data-kind="error" role="alert">
          <div className={styles['emptyTitle']}>{BOARD_STRINGS.topbar.refreshFailed}</div>
        </div>
      ) : vm.emptyState !== null ? (
        <div className={styles['empty']} data-kind={vm.emptyState.kind}>
          <div className={styles['emptyTitle']}>{vm.emptyState.title}</div>
          <div className={styles['emptyHint']}>{vm.emptyState.hint}</div>
        </div>
      ) : (
        vm.groups.map((group) => (
          <ProjectGroup
            key={group.key === '' ? '\u0000unknown' : group.key}
            group={group}
            onSelect={props.onSelectSession}
            collapseIdle={props.filters.collapseIdle === true}
            returnFocusTarget={props.returnFocusTarget}
            onReturnFocusConsumed={props.onReturnFocusConsumed}
          />
        ))
      )}
    </div>
  )
}
