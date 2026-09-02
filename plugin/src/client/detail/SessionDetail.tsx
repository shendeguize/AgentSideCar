/**
 * Session-detail view: header + merged event timeline (design §5.1 view 2).
 *
 * Presentation-only and fully controlled: no data fetching, no api/sse
 * imports. The integration layer (S7) owns transport and accumulation —
 * it feeds the {@link TimelineVM} built via logic.ts (`applyTimelinePage`
 * for history pages, `applyListenPage` for listen-mode refetches) and
 * handles `onLoadMore` / `onToggleListen` / `onRefresh`.
 *
 * Row pipeline (all pure, logic.ts): buildTimelineRows (gaps on the FULL
 * entry list) → filterTimelineRows (UX-03 kind filter, conversation-first
 * by default with an honest hidden count) → aggregateChunkRows (UX-03
 * adjacent empty streaming chunks collapse into one expandable run) →
 * limitTimelineRows (render cap with a 全部显示 escape hatch).
 *
 * Long-list posture (task report): no full virtualization — history only
 * grows page-by-page on explicit 加载更多, and rendering is additionally
 * capped at {@link DEFAULT_MAX_RENDER_ROWS} newest rows. View-local
 * concerns (expanded bodies/runs, filter mode, the lift-cap flag, the
 * UX-04 initial landing, copy feedback) are component state; everything
 * else comes through props.
 */

import {
  Button,
  Pill,
  StateDot,
  writeClipboard,
  type StateDotState,
} from '@deepseek-ai/dsh-client-ui-primitives'
import { Fragment, useEffect, useRef, useState, type ReactElement } from 'react'
import {
  aggregateChunkRows,
  buildTimelineRows,
  countTimelineKinds,
  deriveDetailBodyState,
  deriveDetailStatus,
  deriveSourceBadges,
  agentGlyph,
  filterTimelineRows,
  formatDuration,
  formatRelativeTime,
  formatTemplate,
  limitTimelineRows,
  shouldStickToLatest,
  DEFAULT_MAX_RENDER_ROWS,
  type TimelineEventRowVM,
  type TimelineFilterMode,
  type TimelineRowVM,
  type TimelineVM,
} from './logic.ts'
import { DETAIL_STRINGS } from './strings.ts'
import type { TimelineHistoryScope } from '../api.ts'
import { StaticPill } from '../primitives/StaticPill.tsx'
import { surfaceProps } from '../theme/parts.ts'
import styles from './detail.module.css'

export interface SessionDetailHeaderVM {
  agent: string
  title: string
  project: string
  /** Raw observed status string (open vocabulary, normalized in logic.ts). */
  status: string
  /**
   * Optional adapter metadata. Each field is independently absent-able:
   * adapters differ in what they can report, and a row the header hides is
   * honest while a row it invents is not.
   */
  updatedAtMs?: number
  createdAtMs?: number
  transcript?: string
  model?: string
  modelProvider?: string
}

export interface SessionDetailProps {
  sessionId: string
  header: SessionDetailHeaderVM
  /** Accumulated timeline state (logic.ts createTimelineVM/apply* output). */
  timeline: TimelineVM
  /** True while the owner has a fetch in flight (initial or older page). */
  loading: boolean
  /** Machine reason code of the last failure, or null. */
  error: string | null
  /** True when an older history page can still be fetched. */
  hasMore: boolean
  /** Listen mode (SSE-triggered newest-page refetch) currently on. */
  listening: boolean
  /** True while a manual newest-window refresh is in flight (UX-07). */
  refreshing?: boolean
  onLoadMore: () => void
  onToggleListen: () => void
  /** Manual newest-window refetch; the button renders only when given. */
  onRefresh?: () => void
  onClose?: () => void
  /**
   * How far back the loaded events reach. Under `volatile_only` the oldest
   * event is only where the in-memory ring begins, so the pager must not
   * claim the start of history and the count stays marked partial.
   */
  historyScope?: TimelineHistoryScope
  /** Clock injection for deterministic rendering; defaults to Date.now(). */
  nowMs?: number
  /** Render cap override (segmented rendering); mostly for tests/tuning. */
  maxRenderRows?: number
  /**
   * Wrap ONLY the timeline body. Timeline availability degradation replaces
   * the event list, never the header: losing the timeline must not also cost
   * the operator the back button, the status, and the metadata rows.
   */
  timelineBoundary?: (body: ReactElement) => ReactElement
}

/** Which header value the transient 「已复制」 bubble belongs to. */
type CopyField = 'id' | 'project' | 'transcript'

function EventRow(props: {
  row: TimelineEventRowVM
  expanded: boolean
  onToggleExpand: (key: string) => void
}): ReactElement {
  const { row, expanded, onToggleExpand } = props
  const entry = row.entry
  return (
    <li
      className={styles['event']}
      data-kind={entry.kind}
      data-new={row.isNew || undefined}
      data-testid="agent-sidecar-detail-event"
    >
      <div className={styles['eventHead']} title={row.hoverTitle}>
        <span className={styles['eventGlyph']} aria-hidden>
          {entry.glyph}
        </span>
        <span className={styles['eventLabel']}>{entry.label}</span>
        {entry.seq !== null && (
          <span className={styles['eventSeq']}>
            {DETAIL_STRINGS.timeline.seq.replace('{n}', String(entry.seq))}
          </span>
        )}
        {row.isNew && <Pill className={styles['eventNew']}>{DETAIL_STRINGS.timeline.newBadge}</Pill>}
        <span className={styles['eventSpacer']} />
        <span className={styles['eventTime']}>{row.timeLabel}</span>
      </div>
      {entry.summary !== '' && <div className={styles['eventSummary']}>{entry.summary}</div>}
      {entry.expandable && (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className={styles['expandButton']}
          aria-expanded={expanded}
          onClick={() => onToggleExpand(entry.key)}
        >
          {expanded ? DETAIL_STRINGS.timeline.collapse : DETAIL_STRINGS.timeline.expand}
        </Button>
      )}
      {entry.expandable && expanded && entry.body !== null && (
        <pre className={styles['eventBody']}>{entry.body}</pre>
      )}
    </li>
  )
}

/** Collapsed run of adjacent streaming chunks (UX-03); expandable lossless. */
function ChunkRunRow(props: {
  row: Extract<TimelineRowVM, { type: 'chunks' }>
  expanded: boolean
  onToggleRun: (key: string) => void
  expandedKeys: ReadonlySet<string>
  onToggleExpand: (key: string) => void
}): ReactElement {
  const { row } = props
  return (
    <Fragment>
      <li
        className={styles['chunkRun']}
        data-new={row.isNew || undefined}
        title={row.hoverTitle}
        data-testid="agent-sidecar-detail-chunks"
      >
        <span className={styles['chunkRunLabel']}>{row.label}</span>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className={styles['expandButton']}
          aria-expanded={props.expanded}
          onClick={() => props.onToggleRun(row.key)}
        >
          {props.expanded ? DETAIL_STRINGS.timeline.collapse : DETAIL_STRINGS.timeline.expand}
        </Button>
        <span className={styles['eventSpacer']} />
        <span className={styles['eventTime']}>{row.timeLabel}</span>
      </li>
      {props.expanded &&
        row.members.map((member) => (
          <EventRow
            key={member.key}
            row={member}
            expanded={props.expandedKeys.has(member.key)}
            onToggleExpand={props.onToggleExpand}
          />
        ))}
    </Fragment>
  )
}

/** The session-detail view. Pure render of the logic.ts pipelines over props. */
export function SessionDetail(props: SessionDetailProps): ReactElement {
  const nowMs = props.nowMs ?? Date.now()
  const [expandedKeys, setExpandedKeys] = useState<ReadonlySet<string>>(new Set())
  const [expandedRuns, setExpandedRuns] = useState<ReadonlySet<string>>(new Set())
  const [filterMode, setFilterMode] = useState<TimelineFilterMode>('conversation')
  const [renderAll, setRenderAll] = useState(false)
  const [copied, setCopied] = useState<CopyField | null>(null)
  const listRef = useRef<HTMLOListElement | null>(null)
  const positionedRef = useRef(false)
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const copyAliveRef = useRef(true)

  const status = deriveDetailStatus(props.header.status)
  const sourceBadges = deriveSourceBadges(props.timeline.sources)
  const bodyState = deriveDetailBodyState({
    loading: props.loading,
    error: props.error,
    entryCount: props.timeline.entries.length,
  })

  const allRows = buildTimelineRows(props.timeline, nowMs)
  const filtered = filterTimelineRows(allRows, filterMode)
  const aggregated = aggregateChunkRows(filtered.rows)
  const limited = renderAll
    ? { rows: aggregated, hiddenCount: 0, notice: null }
    : limitTimelineRows(aggregated, props.maxRenderRows ?? DEFAULT_MAX_RENDER_ROWS)

  const entryCount = props.timeline.entries.length
  const listening = props.listening

  // Header facts. Every one of these is optional at the adapter level, so each
  // renders only when its input arrived: an absent row reads as "this agent
  // does not report it", which is the truth, while a zero or an em dash would
  // read as a measurement.
  const lastActivity =
    props.header.updatedAtMs !== undefined
      ? formatTemplate(DETAIL_STRINGS.header.lastActivity, {
          time: formatRelativeTime(props.header.updatedAtMs, nowMs),
        })
      : null
  const durationSpan =
    props.header.createdAtMs !== undefined && props.header.updatedAtMs !== undefined
      ? formatDuration(props.header.updatedAtMs - props.header.createdAtMs)
      : null
  const duration =
    durationSpan !== null
      ? formatTemplate(DETAIL_STRINGS.header.duration, { span: durationSpan })
      : null
  const modelLabel =
    props.header.model !== undefined && props.header.model !== ''
      ? formatTemplate(DETAIL_STRINGS.header.model, { name: props.header.model })
      : null
  const kindCounts = countTimelineKinds(props.timeline.entries)
  // Paging to the end of the volatile ring is not reaching the start of the
  // session: the ring drops its oldest events and its floor moves.
  const reachedStart = props.timeline.reachedStart && props.historyScope !== 'volatile_only'
  const loadedEvents = formatTemplate(
    reachedStart ? DETAIL_STRINGS.header.loadedEvents : DETAIL_STRINGS.header.loadedEventsPartial,
    { n: entryCount },
  )
  const kindBreakdown = kindCounts
    .map((row) => formatTemplate(DETAIL_STRINGS.header.kindCount, { label: row.label, n: row.count }))
    .join(' · ')
  useEffect(() => {
    // UX-04 landing + listen-mode tail pinning (shouldStickToLatest):
    // first non-empty render lands on the newest events; listen appends
    // keep them in view; paging back never yanks the viewport.
    const list = listRef.current
    if (list === null) return
    if (
      shouldStickToLatest({
        entryCount,
        positioned: positionedRef.current,
        listening,
      })
    ) {
      list.scrollTop = list.scrollHeight
      positionedRef.current = true
    }
  }, [listening, entryCount])

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

  const toggleExpand = (key: string): void => {
    setExpandedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const toggleRun = (key: string): void => {
    setExpandedRuns((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const copyValue = async (field: CopyField, value: string): Promise<void> => {
    if (!(await writeClipboard(value))) return
    if (!copyAliveRef.current) return
    if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current)
    setCopied(field)
    copyTimerRef.current = setTimeout(() => {
      copyTimerRef.current = null
      setCopied(null)
    }, 2000)
  }

  const boundary = props.timelineBoundary ?? ((body: ReactElement) => body)

  const statusDotState: StateDotState | null =
    status.status === 'working'
      ? 'ongoing'
      : status.status === 'waiting'
        ? 'warning'
        : null

  return (
    <div {...surfaceProps('timeline', styles['root'])} data-testid="agent-sidecar-detail">
      <header className={styles['header']}>
        <div className={styles['headerTop']}>
          {props.onClose !== undefined && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={props.onClose}
              data-testid="agent-sidecar-detail-back"
            >
              {DETAIL_STRINGS.header.close}
            </Button>
          )}
          <span className={styles['agent']}>
            <span className={styles['agentGlyph']} aria-hidden>
              {agentGlyph(props.header.agent)}
            </span>
            {props.header.agent}
          </span>
          <StaticPill className={styles['badge']} title={DETAIL_STRINGS.header.observedDisclaimer}>
            {statusDotState === null
              ? <span className={styles['dot']} data-tone={status.tone} />
              : <StateDot state={statusDotState} size={8} />}
            {status.label}
          </StaticPill>
          <span className={styles['spacer']} />
          {props.onRefresh !== undefined && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={props.refreshing === true}
              title={DETAIL_STRINGS.header.refreshHint}
              onClick={props.onRefresh}
              data-testid="agent-sidecar-detail-refresh"
            >
              {props.refreshing === true
                ? DETAIL_STRINGS.header.refreshing
                : DETAIL_STRINGS.header.refresh}
            </Button>
          )}
          <Pill
            type="button"
            active={props.listening}
            aria-pressed={props.listening}
            title={DETAIL_STRINGS.header.listenHint}
            onClick={props.onToggleListen}
          >
            {props.listening ? DETAIL_STRINGS.header.listenOn : DETAIL_STRINGS.header.listenOff}
          </Pill>
        </div>
        <div className={styles['title']} title={props.header.title}>
          {props.header.title.trim() === '' ? DETAIL_STRINGS.header.untitled : props.header.title}
        </div>
        <div className={styles['meta']}>
          {props.header.project.trim() === '' ? (
            <span className={styles['project']}>
              {DETAIL_STRINGS.header.unknownProject}
            </span>
          ) : (
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className={styles['project']}
              title={`${props.header.project} · ${DETAIL_STRINGS.header.copyProjectTitle}`}
              onClick={() => { void copyValue('project', props.header.project) }}
              data-testid="agent-sidecar-detail-copy-project"
            >
              {props.header.project}
            </Button>
          )}
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className={styles['sessionId']}
            title={`${props.sessionId} · ${DETAIL_STRINGS.header.copyIdTitle}`}
            onClick={() => { void copyValue('id', props.sessionId) }}
            data-testid="agent-sidecar-detail-copy-id"
          >
            {props.sessionId}
          </Button>
          {copied !== null && (
            <StaticPill className={styles['copiedBubble']} role="status">
              {DETAIL_STRINGS.header.copied}
            </StaticPill>
          )}
        </div>
        <div className={styles['metaRow']} data-testid="agent-sidecar-detail-facts">
          {lastActivity !== null && (
            <span className={styles['fact']}>{lastActivity}</span>
          )}
          {duration !== null && <span className={styles['fact']}>{duration}</span>}
          {modelLabel !== null && <span className={styles['fact']}>{modelLabel}</span>}
          <span className={styles['fact']} title={kindBreakdown}>
            {loadedEvents}
          </span>
        </div>
        {props.header.transcript !== undefined && props.header.transcript !== '' && (
          <div className={styles['metaRow']}>
            <span className={styles['factLabel']}>{DETAIL_STRINGS.header.transcript}</span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className={styles['transcriptPath']}
              title={`${props.header.transcript} · ${DETAIL_STRINGS.header.copyPathTitle}`}
              onClick={() => { void copyValue('transcript', props.header.transcript ?? '') }}
              data-testid="agent-sidecar-detail-copy-transcript"
            >
              {props.header.transcript}
            </Button>
          </div>
        )}
        <div className={styles['metaRow']}>
          <span className={styles['disclaimer']}>{DETAIL_STRINGS.header.observedDisclaimer}</span>
          {sourceBadges.length > 0 && (
            <span className={styles['sourceList']} title={DETAIL_STRINGS.sources.title}>
              {sourceBadges.map((badge) => (
                <StaticPill
                  key={badge.id}
                  className={styles['sourceBadge']}
                  data-tone={badge.tone}
                >
                  {badge.label}
                </StaticPill>
              ))}
            </span>
          )}
        </div>
      </header>

      {boundary(
        <Fragment>
      {bodyState.errorBanner !== null && (
        <div className={styles['banner']} role="alert">
          {bodyState.errorBanner}
        </div>
      )}

      {bodyState.kind !== 'list' ? (
        <div
          className={styles['bodyState']}
          data-kind={bodyState.kind}
          role={bodyState.kind === 'error' ? 'alert' : bodyState.kind === 'loading' ? 'status' : undefined}
        >
          <div className={styles['bodyStateTitle']}>{bodyState.title}</div>
          {bodyState.hint !== null && <div className={styles['bodyStateHint']}>{bodyState.hint}</div>}
        </div>
      ) : (
        <>
          <div className={styles['filterRow']} data-testid="agent-sidecar-detail-filter">
            {(['conversation', 'all'] as const).map((mode) => (
              <Pill
                key={mode}
                type="button"
                active={filterMode === mode}
                aria-pressed={filterMode === mode}
                onClick={() => {
                  setFilterMode(mode)
                }}
              >
                {DETAIL_STRINGS.filter[mode]}
              </Pill>
            ))}
            {filtered.hiddenCount > 0 && (
              <span className={styles['filterHiddenNote']}>
                {formatTemplate(DETAIL_STRINGS.filter.hiddenNotice, { n: filtered.hiddenCount })}
              </span>
            )}
          </div>
          <div className={styles['pager']}>
            {props.hasMore ? (
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={props.loading}
                onClick={props.onLoadMore}
              >
                {props.loading
                  ? DETAIL_STRINGS.timeline.loadingMore
                  : DETAIL_STRINGS.timeline.loadMore}
              </Button>
            ) : (
              reachedStart && (
                <span className={styles['pagerNote']}>{DETAIL_STRINGS.timeline.noMore}</span>
              )
            )}
          </div>
          {limited.notice !== null && (
            <div className={styles['hiddenNotice']}>
              {limited.notice}
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => setRenderAll(true)}
              >
                {DETAIL_STRINGS.timeline.showAll}
              </Button>
            </div>
          )}
          <ol className={styles['timeline']} ref={listRef}>
            {limited.rows.map((row) =>
              row.type === 'gap' ? (
                <li
                  key={row.key}
                  className={styles['gap']}
                  role="note"
                  data-testid="agent-sidecar-detail-gap"
                >
                  {row.label}
                </li>
              ) : row.type === 'chunks' ? (
                <ChunkRunRow
                  key={row.key}
                  row={row}
                  expanded={expandedRuns.has(row.key)}
                  onToggleRun={toggleRun}
                  expandedKeys={expandedKeys}
                  onToggleExpand={toggleExpand}
                />
              ) : (
                <EventRow
                  key={row.key}
                  row={row}
                  expanded={expandedKeys.has(row.key)}
                  onToggleExpand={toggleExpand}
                />
              ),
            )}
          </ol>
        </>
      )}
        </Fragment>,
      )}
    </div>
  )
}
