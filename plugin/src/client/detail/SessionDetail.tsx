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

import { Fragment, useEffect, useRef, useState, type ReactElement } from 'react'
import {
  aggregateChunkRows,
  buildTimelineRows,
  deriveDetailBodyState,
  deriveDetailStatus,
  deriveSourceBadges,
  agentGlyph,
  filterTimelineRows,
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
import styles from './detail.module.css'

export interface SessionDetailHeaderVM {
  agent: string
  title: string
  project: string
  /** Raw observed status string (open vocabulary, normalized in logic.ts). */
  status: string
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
  /** Clock injection for deterministic rendering; defaults to Date.now(). */
  nowMs?: number
  /** Render cap override (segmented rendering); mostly for tests/tuning. */
  maxRenderRows?: number
}

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
        {row.isNew && <span className={styles['eventNew']}>{DETAIL_STRINGS.timeline.newBadge}</span>}
        <span className={styles['eventSpacer']} />
        <span className={styles['eventTime']}>{row.timeLabel}</span>
      </div>
      {entry.summary !== '' && <div className={styles['eventSummary']}>{entry.summary}</div>}
      {entry.expandable && (
        <button
          type="button"
          className={styles['expandButton']}
          onClick={() => onToggleExpand(entry.key)}
        >
          {expanded ? DETAIL_STRINGS.timeline.collapse : DETAIL_STRINGS.timeline.expand}
        </button>
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
        <button
          type="button"
          className={styles['expandButton']}
          onClick={() => props.onToggleRun(row.key)}
        >
          {props.expanded ? DETAIL_STRINGS.timeline.collapse : DETAIL_STRINGS.timeline.expand}
        </button>
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
  const [copied, setCopied] = useState(false)
  const listRef = useRef<HTMLOListElement | null>(null)
  const positionedRef = useRef(false)
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

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

  useEffect(
    () => () => {
      if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current)
    },
    [],
  )

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

  const copySessionId = (): void => {
    const clipboard = typeof navigator === 'undefined' ? undefined : navigator.clipboard
    if (clipboard === undefined) return
    clipboard.writeText(props.sessionId).then(
      () => {
        setCopied(true)
        if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current)
        copyTimerRef.current = setTimeout(() => {
          setCopied(false)
        }, 2000)
      },
      () => {
        // Clipboard permission denied: silently keep the plain id display.
      },
    )
  }

  return (
    <div className={styles['root']} data-testid="agent-sidecar-detail">
      <header className={styles['header']}>
        <div className={styles['headerTop']}>
          {props.onClose !== undefined && (
            <button type="button" className={styles['closeButton']} onClick={props.onClose}>
              {DETAIL_STRINGS.header.close}
            </button>
          )}
          <span className={styles['agent']}>
            <span className={styles['agentGlyph']} aria-hidden>
              {agentGlyph(props.header.agent)}
            </span>
            {props.header.agent}
          </span>
          <span className={styles['badge']} data-tone={status.tone} title={DETAIL_STRINGS.header.observedDisclaimer}>
            <span className={styles['dot']} data-tone={status.tone} />
            {status.label}
          </span>
          <span className={styles['spacer']} />
          {props.onRefresh !== undefined && (
            <button
              type="button"
              className={styles['refreshButton']}
              disabled={props.refreshing === true}
              title={DETAIL_STRINGS.header.refreshHint}
              onClick={props.onRefresh}
              data-testid="agent-sidecar-detail-refresh"
            >
              {props.refreshing === true
                ? DETAIL_STRINGS.header.refreshing
                : DETAIL_STRINGS.header.refresh}
            </button>
          )}
          <button
            type="button"
            className={styles['listenButton']}
            aria-pressed={props.listening}
            data-active={props.listening || undefined}
            title={DETAIL_STRINGS.header.listenHint}
            onClick={props.onToggleListen}
          >
            {props.listening ? DETAIL_STRINGS.header.listenOn : DETAIL_STRINGS.header.listenOff}
          </button>
        </div>
        <div className={styles['title']} title={props.header.title}>
          {props.header.title.trim() === '' ? DETAIL_STRINGS.header.untitled : props.header.title}
        </div>
        <div className={styles['meta']}>
          <span className={styles['project']} title={props.header.project}>
            {props.header.project.trim() === ''
              ? DETAIL_STRINGS.header.unknownProject
              : props.header.project}
          </span>
          <button
            type="button"
            className={styles['sessionId']}
            title={`${props.sessionId} · ${DETAIL_STRINGS.header.copyIdTitle}`}
            onClick={copySessionId}
            data-testid="agent-sidecar-detail-copy-id"
          >
            {props.sessionId}
          </button>
          {copied && (
            <span className={styles['copiedBubble']} role="status">
              {DETAIL_STRINGS.header.copied}
            </span>
          )}
        </div>
        <div className={styles['metaRow']}>
          <span className={styles['disclaimer']}>{DETAIL_STRINGS.header.observedDisclaimer}</span>
          {sourceBadges.length > 0 && (
            <span className={styles['sourceList']} title={DETAIL_STRINGS.sources.title}>
              {sourceBadges.map((badge) => (
                <span key={badge.id} className={styles['sourceBadge']} data-tone={badge.tone}>
                  {badge.label}
                </span>
              ))}
            </span>
          )}
        </div>
      </header>

      {bodyState.errorBanner !== null && (
        <div className={styles['banner']} role="status">
          {bodyState.errorBanner}
        </div>
      )}

      {bodyState.kind !== 'list' ? (
        <div className={styles['bodyState']} data-kind={bodyState.kind}>
          <div className={styles['bodyStateTitle']}>{bodyState.title}</div>
          {bodyState.hint !== null && <div className={styles['bodyStateHint']}>{bodyState.hint}</div>}
        </div>
      ) : (
        <>
          <div className={styles['filterRow']} data-testid="agent-sidecar-detail-filter">
            {(['conversation', 'all'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                className={styles['filterChip']}
                data-active={filterMode === mode || undefined}
                aria-pressed={filterMode === mode}
                onClick={() => {
                  setFilterMode(mode)
                }}
              >
                {DETAIL_STRINGS.filter[mode]}
              </button>
            ))}
            {filtered.hiddenCount > 0 && (
              <span className={styles['filterHiddenNote']}>
                {formatTemplate(DETAIL_STRINGS.filter.hiddenNotice, { n: filtered.hiddenCount })}
              </span>
            )}
          </div>
          <div className={styles['pager']}>
            {props.hasMore ? (
              <button
                type="button"
                className={styles['loadMoreButton']}
                disabled={props.loading}
                onClick={props.onLoadMore}
              >
                {props.loading
                  ? DETAIL_STRINGS.timeline.loadingMore
                  : DETAIL_STRINGS.timeline.loadMore}
              </button>
            ) : (
              <span className={styles['pagerNote']}>{DETAIL_STRINGS.timeline.noMore}</span>
            )}
          </div>
          {limited.notice !== null && (
            <div className={styles['hiddenNotice']}>
              {limited.notice}
              <button
                type="button"
                className={styles['showAllButton']}
                onClick={() => setRenderAll(true)}
              >
                {DETAIL_STRINGS.timeline.showAll}
              </button>
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
    </div>
  )
}
