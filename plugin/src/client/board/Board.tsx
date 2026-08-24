/**
 * Cross-agent session board (design §5.1 view 1).
 *
 * Presentation-only: no data fetching, no api/sse imports. Everything
 * arrives through props already shaped as the view models of `logic.ts`;
 * the integration layer (T2.4) owns state, transport, and the mapping
 * from the host wire types (epoch-seconds → epoch-ms conversion included).
 *
 * Interaction surface handed back to the owner:
 * - `onFiltersChange` — time-window select / show-dead checkbox (controlled);
 * - `onRefresh`      — manual snapshot pull button;
 * - `onSelectSession` — card click, pass-through for the M3 detail view.
 */

import type { ReactElement } from 'react'
import {
  buildBoardViewModel,
  timeWindowLabel,
  formatTemplate,
  type BoardFilterState,
  type DaemonStateToken,
  type DerivedSessionCardVM,
  type ProjectGroupVM,
  type SessionCardVM,
  type StreamHealthToken,
} from './logic.ts'
import { BOARD_STRINGS } from './strings.ts'
import styles from './board.module.css'

/** Time-window choices offered by the top bar (hours). */
export const TIME_WINDOW_OPTIONS: readonly number[] = [6, 12, 24, 48, 168]

export interface BoardProps {
  daemonState: DaemonStateToken
  /** Optional raw detail for the daemon badge hover (e.g. "pid 123 · v0.6.0"). */
  daemonDetail?: string
  streamHealth: StreamHealthToken
  /** Epoch ms of the last authoritative snapshot reconcile, or null. */
  lastReconcileAtMs: number | null
  sessions: SessionCardVM[]
  /** Controlled filter state (owner persists it to the settings namespace). */
  filters: BoardFilterState
  onFiltersChange: (next: BoardFilterState) => void
  onRefresh: () => void
  onSelectSession: (sessionId: string) => void
  /** Clock injection for deterministic rendering; defaults to Date.now(). */
  nowMs?: number
}

function SessionCard(props: {
  card: DerivedSessionCardVM
  onSelect: (sessionId: string) => void
}): ReactElement {
  const { card, onSelect } = props
  return (
    <button
      type="button"
      className={styles['card']}
      onClick={() => onSelect(card.sessionId)}
      data-testid="agent-sidecar-card"
    >
      <div className={styles['cardHead']}>
        <span className={styles['agent']}>
          <span className={styles['glyph']} aria-hidden>
            {card.glyph}
          </span>
          {card.agent}
        </span>
        <span className={styles['badge']} data-tone={card.badge.tone} title={card.hoverTitle}>
          <span className={styles['dot']} data-tone={card.badge.tone} />
          {card.badge.label}
          {card.badge.attention !== null && (
            <span className={styles['attention']} data-kind={card.badge.attention}>
              {card.badge.attentionLabel}
            </span>
          )}
        </span>
      </div>
      <div className={styles['cardTitle']} title={card.title}>
        {card.title.trim() === '' ? BOARD_STRINGS.card.untitled : card.title}
      </div>
      <div className={styles['cardId']} title={card.sessionId}>
        {card.shortId}
      </div>
      <div className={styles['cardEvent']}>
        {card.lastEvent === null
          ? BOARD_STRINGS.card.noEvent
          : `${card.lastEvent.kind} · ${card.lastEvent.text}`}
      </div>
      <div className={styles['cardTime']}>{card.relativeTime}</div>
    </button>
  )
}

function ProjectGroup(props: {
  group: ProjectGroupVM<DerivedSessionCardVM>
  onSelect: (sessionId: string) => void
}): ReactElement {
  const { group, onSelect } = props
  return (
    <section className={styles['group']}>
      <div className={styles['groupHead']}>
        <span
          className={styles['groupName']}
          title={group.fullPath === '' ? undefined : group.fullPath}
        >
          {group.label}
        </span>
        <span className={styles['groupCount']}>
          {formatTemplate(BOARD_STRINGS.groupCount, { n: group.cards.length })}
        </span>
      </div>
      <div className={styles['grid']}>
        {group.cards.map((card) => (
          <SessionCard key={`${card.agent}:${card.sessionId}`} card={card} onSelect={onSelect} />
        ))}
      </div>
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

  return (
    <div className={styles['root']} data-testid="agent-sidecar-board">
      <header className={styles['topbar']}>
        <span className={styles['title']}>{BOARD_STRINGS.topbar.title}</span>
        <span className={styles['badge']} data-tone={vm.daemonBadge.tone} title={props.daemonDetail}>
          <span className={styles['dot']} data-tone={vm.daemonBadge.tone} />
          {vm.daemonBadge.label}
        </span>
        <span className={styles['badge']} data-tone={vm.streamTone}>
          <span className={styles['dot']} data-tone={vm.streamTone} />
          {vm.streamLabel}
        </span>
        <span className={styles['spacer']} />
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
        <button
          type="button"
          className={styles['refresh']}
          title={BOARD_STRINGS.topbar.refreshTitle}
          onClick={props.onRefresh}
        >
          {BOARD_STRINGS.topbar.refresh}
        </button>
      </header>

      {vm.banner !== null && (
        <div className={styles['banner']} data-tone={vm.banner.tone} role="status">
          {vm.banner.text}
        </div>
      )}

      {vm.emptyState !== null ? (
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
          />
        ))
      )}
    </div>
  )
}
