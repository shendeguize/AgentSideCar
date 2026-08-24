/**
 * dsh lineage tree (design §5.1 view 2 dsh 会话专属区: 谱系树 + 溯源跳转).
 *
 * Fully controlled and presentational — the component does NO data
 * fetching; the integration layer calls `GET <prefix>/lineage/<id>` (or
 * short-circuits with `externalLineageFallback` for non-dsh sessions) and
 * feeds the always-200 body straight into the props. `available:false` is
 * rendered as an honest degradation card per reason (§5.3), never as an
 * error and never faked into an empty tree.
 *
 * The only component-owned state is the collapse set of the tree rows; it
 * resets whenever the traced target changes. Every derivation lives in
 * ./logic.ts and is unit-tested there.
 */

import { useState } from 'react'
import type { ReactElement } from 'react'
import css from './dsh-tools.module.css'
import {
  deriveLineageView,
  formatTemplate,
  resolveJumpTarget,
  visibleLineageNodes,
  type LineageNodeVM,
  type LineageTraceVM,
  type LineageTreeVM,
} from './logic.ts'
import { DSH_TOOLS_STRINGS } from './strings.ts'

export interface LineageTreeProps {
  /** Wire trace of `GET lineage/<id>` (null while degraded/empty). */
  trace: LineageTraceVM | null
  /** Wire `available` bit; false renders a degradation card, not an error. */
  available: boolean
  /** Wire degradation reason, or the client-side `not_dsh_session`. */
  reason?: string | null
  /** Wire degradation detail (trace_failed carries the engine error). */
  detail?: string | null
  /** The session the user is inspecting — highlighted, click is a no-op. */
  currentSessionId: string | null
  /** Provenance jump: click on any other node navigates to that session. */
  onSelectSession: (sessionId: string) => void
  loading: boolean
  /** Transport/HTTP failure text from the integration, or null. */
  error: string | null
}

const S = DSH_TOOLS_STRINGS.lineage

function TreeRow(props: {
  node: LineageNodeVM
  collapsed: boolean
  currentSessionId: string | null
  onToggle: (id: string) => void
  onSelectSession: (sessionId: string) => void
}): ReactElement {
  const { node, collapsed } = props
  const jump = resolveJumpTarget(node.id, props.currentSessionId)
  return (
    <div
      className={css['treeRow']}
      style={{ paddingLeft: node.depth * 16 }}
      data-testid="agent-sidecar-lineage-row"
      data-role={node.role}
    >
      {node.hasChildren ? (
        <button
          type="button"
          className={css['toggle']}
          aria-expanded={!collapsed}
          aria-label={collapsed ? S.expand : S.collapse}
          onClick={() => props.onToggle(node.id)}
        >
          {collapsed ? '▸' : '▾'}
        </button>
      ) : (
        <span className={css['toggleSpacer']} aria-hidden />
      )}
      <button
        type="button"
        className={css['node']}
        data-current={node.isCurrent ? 'true' : 'false'}
        aria-current={node.isCurrent ? 'true' : undefined}
        title={jump.kind === 'current' ? S.currentTitle : S.jumpTitle}
        onClick={() => {
          if (jump.kind === 'select') props.onSelectSession(jump.sessionId)
        }}
      >
        <span className={css['nodeRole']}>{node.roleLabel}</span>
        <span className={css['nodeId']} title={node.id}>
          {node.shortId}
        </span>
        {node.isCurrent && (
          <span className={css['nodeBadge']} data-kind="current">
            {S.currentBadge}
          </span>
        )}
        {node.live && (
          <span className={css['nodeBadge']} data-kind="live">
            {S.liveBadge}
          </span>
        )}
        {!node.persisted && <span className={css['nodeBadge']}>{S.notPersistedBadge}</span>}
      </button>
    </div>
  )
}

function Tree(props: {
  tree: LineageTreeVM
  currentSessionId: string | null
  onSelectSession: (sessionId: string) => void
}): ReactElement {
  const { tree } = props
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())
  // Collapse state belongs to one trace; a new target starts expanded.
  const [seenTarget, setSeenTarget] = useState(tree.targetId)
  if (seenTarget !== tree.targetId) {
    setSeenTarget(tree.targetId)
    setCollapsed(new Set())
  }
  const toggle = (id: string): void => {
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  return (
    <>
      {tree.incompleteNotice !== null && (
        <div className={css['noticeBar']} role="status">
          {tree.incompleteNotice}
        </div>
      )}
      <div className={css['tree']} role="tree" data-testid="agent-sidecar-lineage-tree">
        {visibleLineageNodes(tree.nodes, collapsed).map((node) => (
          <TreeRow
            key={node.id}
            node={node}
            collapsed={collapsed.has(node.id)}
            currentSessionId={props.currentSessionId}
            onToggle={toggle}
            onSelectSession={props.onSelectSession}
          />
        ))}
      </div>
    </>
  )
}

/** The lineage panel. Pure render of `deriveLineageView` over the props. */
export function LineageTree(props: LineageTreeProps): ReactElement {
  const view = deriveLineageView({
    loading: props.loading,
    error: props.error,
    available: props.available,
    reason: props.reason ?? null,
    detail: props.detail ?? null,
    trace: props.trace,
    currentSessionId: props.currentSessionId,
  })

  return (
    <section className={css['panel']} data-testid="agent-sidecar-lineage">
      <div className={css['panelHead']}>
        <span className={css['panelTitle']}>{S.title}</span>
        {view.kind === 'tree' && (
          <span className={css['panelCount']}>
            {formatTemplate(S.nodeCount, { n: view.tree.nodeCount })}
          </span>
        )}
      </div>

      {view.kind === 'loading' && <div className={css['mutedLine']}>{view.text}</div>}

      {view.kind === 'error' && (
        <div className={css['errorCard']} role="alert">
          {view.text}
          {view.detail !== null && <span className={css['errorDetail']}>{view.detail}</span>}
        </div>
      )}

      {view.kind === 'degraded' && (
        <div
          className={css['degradeCard']}
          data-reason={view.card.reason}
          data-testid="agent-sidecar-lineage-degraded"
        >
          <span className={css['degradeTitle']}>{view.card.title}</span>
          <span className={css['degradeBody']}>{view.card.body}</span>
          {view.card.detail !== null && (
            <span className={css['degradeDetail']}>{view.card.detail}</span>
          )}
        </div>
      )}

      {view.kind === 'empty' && <div className={css['mutedLine']}>{view.text}</div>}

      {view.kind === 'tree' && (
        <Tree
          tree={view.tree}
          currentSessionId={props.currentSessionId}
          onSelectSession={props.onSelectSession}
        />
      )}
    </section>
  )
}
