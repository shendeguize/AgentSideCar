/**
 * Session-detail view: header + merged event timeline (design §5.1 view 2).
 *
 * Presentation-only and fully controlled: no data fetching, no api/sse
 * imports. The integration layer (S7) owns transport and accumulation —
 * it feeds the {@link TimelineVM} built via logic.ts (`applyTimelinePage`
 * for history pages, `applyListenPage` for listen-mode refetches) and
 * handles `onLoadMore` / `onToggleListen`.
 *
 * Long-list posture (task report): no full virtualization — history only
 * grows page-by-page on explicit 加载更多, and rendering is additionally
 * capped at {@link DEFAULT_MAX_RENDER_ROWS} newest rows behind a collapse
 * notice with a 全部显示 escape hatch. View-local concerns (expanded
 * bodies, the lift-cap flag, auto-scroll) are component state; everything
 * else comes through props.
 */

import { useEffect, useRef, useState, type ReactElement } from 'react'
import {
  buildTimelineRows,
  deriveDetailBodyState,
  deriveDetailStatus,
  deriveSourceBadges,
  agentGlyph,
  limitTimelineRows,
  DEFAULT_MAX_RENDER_ROWS,
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
  onLoadMore: () => void
  onToggleListen: () => void
  onClose?: () => void
  /** Clock injection for deterministic rendering; defaults to Date.now(). */
  nowMs?: number
  /** Render cap override (segmented rendering); mostly for tests/tuning. */
  maxRenderRows?: number
}

function EventRow(props: {
  row: Extract<TimelineRowVM, { type: 'event' }>
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
        <span className={styles['eventTime']}>{row.relativeTime}</span>
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

/** The session-detail view. Pure render of the logic.ts pipelines over props. */
export function SessionDetail(props: SessionDetailProps): ReactElement {
  const nowMs = props.nowMs ?? Date.now()
  const [expandedKeys, setExpandedKeys] = useState<ReadonlySet<string>>(new Set())
  const [renderAll, setRenderAll] = useState(false)
  const listRef = useRef<HTMLOListElement | null>(null)

  const status = deriveDetailStatus(props.header.status)
  const sourceBadges = deriveSourceBadges(props.timeline.sources)
  const bodyState = deriveDetailBodyState({
    loading: props.loading,
    error: props.error,
    entryCount: props.timeline.entries.length,
  })

  const allRows = buildTimelineRows(props.timeline, nowMs)
  const limited = renderAll
    ? { rows: allRows, hiddenCount: 0, notice: null }
    : limitTimelineRows(allRows, props.maxRenderRows ?? DEFAULT_MAX_RENDER_ROWS)

  const entryCount = props.timeline.entries.length
  const listening = props.listening
  useEffect(() => {
    // Listen mode appends at the tail: keep the newest events in view.
    if (!listening) return
    const list = listRef.current
    if (list !== null) list.scrollTop = list.scrollHeight
  }, [listening, entryCount])

  const toggleExpand = (key: string): void => {
    setExpandedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
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
          <span className={styles['sessionId']} title={props.sessionId}>
            {props.sessionId}
          </span>
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
