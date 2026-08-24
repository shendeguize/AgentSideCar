/**
 * Cross-agent session search panel (design §4.e.4 / §5.1: dsh 全文检索,
 * 外部会话标题/项目过滤降级).
 *
 * Fully controlled and presentational — the component does NO data
 * fetching; the integration layer owns the query state, calls
 * `GET <prefix>/search?q=&project=&limit=`, maps the response through
 * `normalizeSearchItems` (./logic.ts) and feeds the rows plus the wire
 * `mode` back in. `mode: 'filter-only'` renders the honest degradation
 * bar (§5.3) — the panel keeps working as a title/project filter and
 * never pretends full-text ran.
 *
 * Snippets arrive pre-split into highlight segments (React-safe, no HTML
 * injection); full-text hits carry them, filter hits render without.
 */

import type { ReactElement } from 'react'
import css from './dsh-tools.module.css'
import {
  deriveSearchView,
  formatTemplate,
  type SearchItemVM,
  type SearchMode,
} from './logic.ts'
import { DSH_TOOLS_STRINGS } from './strings.ts'

export interface SearchPanelProps {
  /** Controlled query text (owner persists/submits it). */
  query: string
  /** Active project filter echoed by the backend, if any. */
  project?: string | null
  /** Wire search mode; 'filter-only' shows the degradation bar. */
  mode: SearchMode
  /** Normalized result rows (owner maps the wire body via logic.ts). */
  items: SearchItemVM[]
  loading: boolean
  /** Transport/HTTP failure text from the integration, or null. */
  error: string | null
  onQueryChange: (query: string) => void
  onSubmit: () => void
  /** Result click → open that session (same seam as the board cards). */
  onSelectSession: (sessionId: string) => void
}

const S = DSH_TOOLS_STRINGS.search

function ResultItem(props: {
  item: SearchItemVM
  onSelectSession: (sessionId: string) => void
}): ReactElement {
  const { item } = props
  return (
    <button
      type="button"
      className={css['resultItem']}
      onClick={() => props.onSelectSession(item.sessionId)}
      data-testid="agent-sidecar-search-item"
    >
      <span className={css['resultHead']}>
        <span className={css['resultAgent']}>{item.agent}</span>
        <span className={css['resultTitle']} title={item.title}>
          {item.titleLabel}
        </span>
        <span className={css['matchTag']} data-kind={item.matchedBy}>
          {item.matchedByLabel}
        </span>
      </span>
      <span className={css['resultMeta']}>
        <span className={css['resultProject']} title={item.project}>
          {item.project}
        </span>
        <span className={css['resultId']} title={item.sessionId}>
          {item.shortId}
        </span>
      </span>
      {item.snippet !== null && (
        <span className={css['snippet']}>
          {item.snippet.map((segment, index) =>
            segment.highlight ? (
              <mark key={index} className={css['snippetMark']}>
                {segment.text}
              </mark>
            ) : (
              <span key={index}>{segment.text}</span>
            ),
          )}
        </span>
      )}
    </button>
  )
}

/** The search panel. Pure render of `deriveSearchView` over the props. */
export function SearchPanel(props: SearchPanelProps): ReactElement {
  const view = deriveSearchView({
    loading: props.loading,
    error: props.error,
    mode: props.mode,
    itemCount: props.items.length,
    query: props.query,
  })
  const project = props.project ?? null

  return (
    <section className={css['panel']} data-testid="agent-sidecar-search">
      <div className={css['panelHead']}>
        <span className={css['panelTitle']}>{S.title}</span>
      </div>

      <form
        className={css['searchForm']}
        onSubmit={(ev) => {
          ev.preventDefault()
          props.onSubmit()
        }}
      >
        <input
          type="search"
          className={css['searchInput']}
          value={props.query}
          placeholder={S.placeholder}
          onChange={(ev) => props.onQueryChange(ev.target.value)}
        />
        <button type="submit" className={css['searchSubmit']}>
          {S.submit}
        </button>
      </form>

      {project !== null && project !== '' && (
        <span className={css['projectChip']} title={project}>
          {formatTemplate(S.projectFilter, { project })}
        </span>
      )}

      {view.notice !== null && (
        <div
          className={css['noticeBar']}
          role="status"
          data-testid="agent-sidecar-search-degraded"
        >
          {view.notice}
        </div>
      )}

      {view.body === 'loading' && <div className={css['mutedLine']}>{view.text}</div>}

      {view.body === 'error' && (
        <div className={css['errorCard']} role="alert">
          {view.text}
          {view.detail !== null && <span className={css['errorDetail']}>{view.detail}</span>}
        </div>
      )}

      {view.body === 'empty' && <div className={css['mutedLine']}>{view.text}</div>}

      {view.body === 'results' && (
        <div className={css['resultList']}>
          {props.items.map((item) => (
            <ResultItem
              key={`${item.agent}:${item.sessionId}`}
              item={item}
              onSelectSession={props.onSelectSession}
            />
          ))}
        </div>
      )}
    </section>
  )
}
