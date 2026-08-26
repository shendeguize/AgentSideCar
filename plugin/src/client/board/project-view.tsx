/**
 * Cross-agent project correlation view (design §4.e.2 / §5.1 M3, T5.5).
 *
 * Presentation-only and fully controlled: no data fetching, no api/sse
 * imports. The owner fetches `GET <prefix>/projects`, hands the wire
 * groups in as props (the wire shape IS the input view model — camelCase,
 * epoch-ms `lastActivityAt`), and receives session clicks back through
 * `onSelectSession` (pass-through to the detail view, same as Board).
 *
 * Render precedence when there is nothing to show: error > loading >
 * empty state. When groups ARE available, an error renders as a banner
 * above the (possibly stale) content instead of replacing it, and
 * `loading` shows as a quiet header chip — honest degradation without
 * blanking data the user already has.
 */

import { Button, Pill, StateDot, type StateDotState } from '@deepseek-ai/dsh-client-ui-primitives'
import { useEffect, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import {
  buildProjectViewModel,
  LANE_SESSION_LIMIT,
  PROJECT_VIEW_STRINGS,
  type AgentLaneVM,
  type DerivedProjectGroupVM,
  type DerivedProjectSessionVM,
  type ProjectGroupVM,
} from './project-view-logic.ts'
import { formatTemplate, sliceCardsForDisplay } from './logic.ts'
import { surfaceProps } from '../theme/parts.ts'
import styles from './project-view.module.css'

interface SessionFocusTarget {
  agent: string
  sessionId: string
}

function matchesSessionFocusTarget(
  candidate: SessionFocusTarget,
  target: SessionFocusTarget | null,
): boolean {
  return target !== null
    && candidate.agent === target.agent
    && candidate.sessionId === target.sessionId
}

export interface ProjectViewProps {
  /** Wire groups of `GET projects` (owner passes the response through). */
  groups: ProjectGroupVM[]
  loading: boolean
  /** Human-readable fetch error, or null. */
  error: string | null
  onSelectSession: (target: SessionFocusTarget) => void
  /** Composite in-memory identity awaiting return-focus restoration. */
  returnFocusTarget: SessionFocusTarget | null
  /** Called only after the matching row or fallback heading has been focused. */
  onReturnFocusConsumed: () => void
  /** Current mounted scroll container; null on route/view unmount. */
  rootRef: (element: HTMLDivElement | null) => void
  /** Persist this view's independent scroll position in the route owner. */
  onScrollTopChange: (scrollTop: number) => void
  /** Clock injection for deterministic rendering; defaults to Date.now(). */
  nowMs?: number
}

function sessionDotState(status: DerivedProjectSessionVM['badge']['status']): StateDotState | null {
  if (status === 'working') return 'ongoing'
  if (status === 'waiting') return 'warning'
  return null
}

function SessionRow(props: {
  session: DerivedProjectSessionVM
  onSelect: (target: SessionFocusTarget) => void
  returnFocusTarget: SessionFocusTarget | null
  onReturnFocusConsumed: () => void
}): ReactElement {
  const { session, onSelect } = props
  const dotState = sessionDotState(session.badge.status)
  const openerRef = useRef<HTMLButtonElement>(null)
  const isReturnFocusTarget = matchesSessionFocusTarget(session, props.returnFocusTarget)
  useEffect(() => {
    if (!isReturnFocusTarget) return
    const opener = openerRef.current
    if (opener === null) return
    opener.focus({ preventScroll: true })
    props.onReturnFocusConsumed()
  }, [isReturnFocusTarget, props.onReturnFocusConsumed])
  return (
    <button
      ref={openerRef}
      type="button"
      className={styles['session']}
      onClick={() => onSelect({ agent: session.agent, sessionId: session.sessionId })}
      data-testid="agent-sidecar-project-session"
    >
      <Pill className={styles['statusPill']}>
        {dotState === null
          ? <span className={styles['dot']} data-tone={session.badge.tone} />
          : <StateDot state={dotState} size={8} />}
        {session.badge.label}
        {session.badge.attention !== null && (
          <span className={styles['attention']} data-kind={session.badge.attention}>
            {session.badge.attentionLabel}
          </span>
        )}
      </Pill>
      <span className={styles['sessionTitle']} title={session.title}>
        {session.displayTitle}
      </span>
      {session.live && <Pill className={styles['liveChip']}>{PROJECT_VIEW_STRINGS.liveChip}</Pill>}
      <span className={styles['sessionId']} title={session.sessionId}>
        {session.shortId}
      </span>
      <span className={styles['sessionTime']}>{session.relativeTime}</span>
    </button>
  )
}

function AgentLane(props: {
  lane: AgentLaneVM
  onSelect: (target: SessionFocusTarget) => void
  returnFocusTarget: SessionFocusTarget | null
  onReturnFocusConsumed: () => void
}): ReactElement {
  const { lane, onSelect } = props
  // UX-20: lanes fold past the row limit — ephemeral view state, same
  // pattern as the board's group truncation. Rows are status-sorted, so
  // the fold never hides a leading working/waiting run (slice guard).
  const [expanded, setExpanded] = useState(false)
  const returnTargetIndex = props.returnFocusTarget === null
    ? -1
    : lane.sessions.findIndex((session) =>
      matchesSessionFocusTarget(session, props.returnFocusTarget),
    )
  useEffect(() => {
    if (returnTargetIndex >= LANE_SESSION_LIMIT && !expanded) setExpanded(true)
  }, [expanded, returnTargetIndex])
  const { shown, hiddenCount } = sliceCardsForDisplay(lane.sessions, LANE_SESSION_LIMIT, expanded)
  return (
    <div className={styles['lane']}>
      <div className={styles['laneHead']}>
        <span className={styles['glyph']} aria-hidden>
          {lane.glyph}
        </span>
        {lane.agent}
      </div>
      <div className={styles['laneSessions']}>
        {shown.map((session) => (
          <SessionRow
            key={`${session.agent}:${session.sessionId}`}
            session={session}
            onSelect={onSelect}
            returnFocusTarget={props.returnFocusTarget}
            onReturnFocusConsumed={props.onReturnFocusConsumed}
          />
        ))}
      </div>
      {hiddenCount > 0 && (
        <Button
          size="sm"
          variant="outline"
          className={styles['showMore']}
          onClick={() => { setExpanded(true) }}
        >
          {formatTemplate(PROJECT_VIEW_STRINGS.showAllSessions, { n: lane.sessions.length })}
        </Button>
      )}
      {expanded && lane.sessions.length > LANE_SESSION_LIMIT && (
        <Button
          size="sm"
          variant="outline"
          className={styles['showMore']}
          onClick={() => { setExpanded(false) }}
        >
          {formatTemplate(PROJECT_VIEW_STRINGS.showLessSessions, { n: LANE_SESSION_LIMIT })}
        </Button>
      )}
    </div>
  )
}

function ProjectSection(props: {
  group: DerivedProjectGroupVM
  onSelect: (target: SessionFocusTarget) => void
  returnFocusTarget: SessionFocusTarget | null
  onReturnFocusConsumed: () => void
}): ReactElement {
  const { group, onSelect } = props
  return (
    <section className={styles['group']} data-testid="agent-sidecar-project-group">
      <div className={styles['groupHead']}>
        <span
          className={styles['groupName']}
          title={group.fullPath === '' ? undefined : group.fullPath}
        >
          {group.label}
        </span>
        <span className={styles['agentBadges']}>
          {group.agentBadges.map((badge) => (
            <span key={badge.agent} className={styles['agentBadge']} title={badge.agent}>
              <span className={styles['glyph']} aria-hidden>
                {badge.glyph}
              </span>
              {badge.agent}
            </span>
          ))}
          {group.crossAgentLabel !== null && (
            <span className={styles['crossBadge']}>{group.crossAgentLabel}</span>
          )}
        </span>
        <span className={styles['spacer']} />
        <span className={styles['groupMeta']}>{group.sessionCountLabel}</span>
        <span className={styles['groupMeta']}>{group.lastActiveLabel}</span>
      </div>
      <div className={styles['lanes']}>
        {group.lanes.map((lane) => (
          <AgentLane
            key={lane.agent}
            lane={lane}
            onSelect={onSelect}
            returnFocusTarget={props.returnFocusTarget}
            onReturnFocusConsumed={props.onReturnFocusConsumed}
          />
        ))}
      </div>
    </section>
  )
}

/** The project correlation view. Pure render of `buildProjectViewModel`. */
export function ProjectView(props: ProjectViewProps): ReactElement {
  const nowMs = props.nowMs ?? Date.now()
  const vm = buildProjectViewModel({ groups: props.groups, nowMs })
  const hasContent = vm.groups.length > 0
  const fallbackFocusRef = useRef<HTMLSpanElement>(null)
  const returnTargetVisible = props.returnFocusTarget !== null
    && vm.groups.some((group) =>
      group.lanes.some((lane) =>
        lane.sessions.some((session) =>
          matchesSessionFocusTarget(session, props.returnFocusTarget),
        ),
      ),
    )

  useEffect(() => {
    if (props.returnFocusTarget === null || returnTargetVisible) return
    const fallback = fallbackFocusRef.current
    if (fallback === null) return
    fallback.focus({ preventScroll: true })
    props.onReturnFocusConsumed()
  }, [props.onReturnFocusConsumed, props.returnFocusTarget, returnTargetVisible])

  return (
    <div
      {...surfaceProps('project-view', styles['root'])}
      ref={props.rootRef}
      onScroll={(event) => { props.onScrollTopChange(event.currentTarget.scrollTop) }}
      data-testid="agent-sidecar-project-view"
    >
      <header className={styles['topbar']}>
        <span
          ref={fallbackFocusRef}
          className={styles['title']}
          role="heading"
          aria-level={1}
          tabIndex={-1}
        >
          {PROJECT_VIEW_STRINGS.title}
        </span>
        <span className={styles['summary']}>{vm.summaryLabel}</span>
        {props.loading && (
          <span role="status">
            <Pill>{PROJECT_VIEW_STRINGS.loading}</Pill>
          </span>
        )}
      </header>

      {props.error !== null && hasContent && (
        <div className={styles['banner']} data-tone="danger" role="alert">
          {PROJECT_VIEW_STRINGS.errorTitle}: {props.error}
        </div>
      )}

      {hasContent ? (
        vm.groups.map((group) => (
          <ProjectSection
            key={group.key === '' ? '\u0000unknown' : group.key}
            group={group}
            onSelect={props.onSelectSession}
            returnFocusTarget={props.returnFocusTarget}
            onReturnFocusConsumed={props.onReturnFocusConsumed}
          />
        ))
      ) : props.error !== null ? (
        <div className={styles['empty']} data-kind="error" role="alert">
          <div className={styles['emptyTitle']}>{PROJECT_VIEW_STRINGS.errorTitle}</div>
          <div className={styles['emptyHint']}>{props.error}</div>
        </div>
      ) : props.loading ? (
        <div className={styles['empty']} data-kind="loading" role="status">
          <div className={styles['emptyHint']}>{PROJECT_VIEW_STRINGS.loading}</div>
        </div>
      ) : (
        vm.emptyState !== null && (
          <div className={styles['empty']} data-kind="no-projects">
            <div className={styles['emptyTitle']}>{vm.emptyState.title}</div>
            <div className={styles['emptyHint']}>{vm.emptyState.hint}</div>
          </div>
        )
      )}
    </div>
  )
}
