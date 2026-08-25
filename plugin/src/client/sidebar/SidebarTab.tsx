/** Presentation-only compact view for the optional better-sidebar surface.
 * Integration owns discovery and view-model derivation; this root subscribes
 * once to locale changes for its complete presentation subtree. */

import { Button, StateDot, type StateDotState } from '@deepseek-ai/dsh-client-ui-primitives'
import { useState } from 'react'
import type { ReactElement } from 'react'
import {
  agentGlyph,
  formatRelativeTime,
  normalizeStatus,
  projectDisplayName,
  type SessionCardVM,
  type SessionStatusToken,
} from '../board/logic.ts'
import { t, type SidecarLocaleKey } from '../locales/index.ts'
import { useActiveLocale } from '../locales/react.ts'
import { StaticPill } from '../primitives/StaticPill.tsx'
import { surfaceProps } from '../theme/parts.ts'
import type { SidebarMiniVM } from './model.ts'
import css from './sidebar-tab.module.css'

const STATUS_LABEL_KEY: Record<SessionStatusToken, SidecarLocaleKey> = {
  working: 'detail.status.working',
  waiting: 'detail.status.waiting',
  idle: 'detail.status.idle',
  dead: 'detail.status.dead',
  unknown: 'detail.status.unknown',
}

const CONNECTION_DOT_STATE: Record<SidebarMiniVM['connection'], StateDotState> = {
  ok: 'done',
  degraded: 'warning',
  off: 'error',
}

export interface SidebarTabProps {
  vm: SidebarMiniVM
  visible: boolean
  /** Clock injection keeps SSR and focused view tests deterministic. */
  nowMs?: number
}

/** Icon renderer kept beside the view while preserving the descriptor callback API. */
export function SidebarTabIcon({ size }: { size: number }): ReactElement {
  return (
    <svg className={css['icon']} width={size} height={size} viewBox="0 0 24 24" aria-hidden>
      <path d="M12 2 22 12 12 22 2 12 12 2Zm0 3.4L5.4 12l6.6 6.6 6.6-6.6L12 5.4Z" />
      <circle cx="12" cy="12" r="2.2" />
    </svg>
  )
}

function SessionRow(props: {
  session: SessionCardVM
  nowMs: number
  expanded: boolean
  detailId: string
  onToggle: () => void
}): ReactElement {
  const { session, nowMs, expanded, detailId } = props
  const status = normalizeStatus(session.status)
  const title = session.title.trim() === '' ? t('sidebar.untitled') : session.title
  const lastEvent =
    session.lastEvent === null
      ? <span className={css['muted']}>{t('sidebar.noEvent')}</span>
      : `${session.lastEvent.kind}: ${session.lastEvent.text}`

  return (
    <li className={css['sessionItem']}>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        className={css['sessionButton']}
        onClick={props.onToggle}
        data-testid="agent-sidecar-sidebar-session"
        data-session-id={session.sessionId}
        data-status={status}
        aria-expanded={expanded}
        aria-controls={detailId}
      >
        <span className={css['glyph']} aria-hidden>{agentGlyph(session.agent)}</span>
        <span className={css['sessionTitle']} title={session.sessionId}>
          {title}
        </span>
        <span className={css['sessionMeta']}>
          {t(STATUS_LABEL_KEY[status])} · {formatRelativeTime(session.updatedAtMs, nowMs)}
        </span>
      </Button>
      {expanded && (
        <div
          id={detailId}
          className={css['detail']}
          role="region"
          aria-label={title}
          data-testid="agent-sidecar-sidebar-detail"
        >
          <div>{projectDisplayName(session.project)}</div>
          <div>{lastEvent}</div>
        </div>
      )}
    </li>
  )
}

/** Compact better-sidebar body. No controller, service, or transport imports. */
export function SidebarTab({ vm, visible, nowMs = Date.now() }: SidebarTabProps): ReactElement {
  useActiveLocale()
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const recentTitleId = 'agent-sidecar-sidebar-recent-title'

  let body: ReactElement
  if (!vm.hasSnapshot) {
    body = <p className={css['muted']}>{t('sidebar.connecting')}</p>
  } else if (vm.recent.length === 0) {
    body = <p className={css['muted']}>{t('sidebar.noSessions')}</p>
  } else {
    body = (
      <ul className={css['sessionList']} aria-labelledby={recentTitleId}>
        {vm.recent.map((session, index) => (
          <SessionRow
            key={session.sessionId}
            session={session}
            nowMs={nowMs}
            expanded={expandedId === session.sessionId}
            detailId={`agent-sidecar-sidebar-detail-${index}`}
            onToggle={() => {
              setExpandedId((previous) => previous === session.sessionId ? null : session.sessionId)
            }}
          />
        ))}
      </ul>
    )
  }

  return (
    <section
      {...surfaceProps('sidebar-tab', css['root'])}
      data-testid="agent-sidecar-sidebar-tab"
      data-visible={visible}
      aria-label={t('sidebar.tabTitle')}
      aria-hidden={!visible}
    >
      <header className={css['header']} title={vm.connectionTitle}>
        <StaticPill className={css['counts']} data-testid="agent-sidecar-sidebar-counts">
          <StateDot state={CONNECTION_DOT_STATE[vm.connection]} size={8} />
          {t('sidebar.countsRow', { working: vm.workingCount, waiting: vm.waitingCount })}
        </StaticPill>
      </header>
      <h2 id={recentTitleId} className={css['sectionTitle']}>{t('sidebar.recentTitle')}</h2>
      {body}
      <p className={css['hint']}>{t('sidebar.boardHint')}</p>
    </section>
  )
}
