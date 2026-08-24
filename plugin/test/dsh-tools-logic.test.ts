/**
 * Unit tests for the dsh deep-query tools logic
 * (src/client/dsh-tools/logic.ts). Pure functions only — no React, no
 * DOM, node environment (same posture as board-logic.test.ts). Rendering
 * correctness is validated by the integration task against a real dsh web.
 *
 * Coverage per task T5.4: lineage trace → tree (multi-level / single node
 * / broken chain), current-session highlight, provenance-jump resolution,
 * search normalization (full-text snippet highlighting / filter-only
 * degradation), the external-agent (non-dsh) degradation model, and the
 * per-reason presentation of `available:false` responses.
 */

import { describe, expect, it } from 'vitest'
import {
  abbreviateId,
  buildLineageTree,
  deriveLineageView,
  deriveSearchView,
  externalLineageFallback,
  formatTemplate,
  highlightSnippet,
  lineageDegradeCard,
  normalizeSearchItems,
  resolveJumpTarget,
  searchDegradeNotice,
  visibleLineageNodes,
  type LineageRecordVM,
  type LineageTraceVM,
  type SearchResponseVM,
} from '../src/client/dsh-tools/logic.ts'
import { DSH_TOOLS_STRINGS } from '../src/client/dsh-tools/strings.ts'

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

function record(
  id: string,
  parentSession?: string,
  over: Partial<Omit<LineageRecordVM, 'header'>> = {},
): LineageRecordVM {
  return {
    header: {
      id,
      createdAt: 1_000,
      cwd: '/tmp/proj',
      ...(parentSession !== undefined ? { parentSession } : {}),
    },
    live: false,
    persisted: true,
    ...over,
  }
}

/** root → p1 → target → {c1 → c1a, c2}; ancestors deliberately NOT root-first. */
function multiLevelTrace(): LineageTraceVM {
  return {
    target: record('target', 'p1'),
    ancestors: [record('p1', 'root'), record('root')],
    descendants: [
      {
        session: record('c1', 'target', { live: true }),
        descendants: [{ session: record('c1a', 'c1'), descendants: [] }],
      },
      { session: record('c2', 'target'), descendants: [] },
    ],
    complete: true,
    root: record('root'),
  }
}

// ---------------------------------------------------------------------------
// buildLineageTree.
// ---------------------------------------------------------------------------

describe('buildLineageTree', () => {
  it('flattens a multi-level trace: ancestors root-first, target, descendants DFS', () => {
    const tree = buildLineageTree(multiLevelTrace(), 'target')
    expect(tree.nodes.map((n) => n.id)).toEqual(['root', 'p1', 'target', 'c1', 'c1a', 'c2'])
    expect(tree.nodes.map((n) => n.depth)).toEqual([0, 1, 2, 3, 4, 3])
    expect(tree.nodes.map((n) => n.role)).toEqual([
      'ancestor',
      'ancestor',
      'target',
      'descendant',
      'descendant',
      'descendant',
    ])
    expect(tree.nodeCount).toBe(6)
    expect(tree.maxDepth).toBe(4)
    expect(tree.targetId).toBe('target')
    expect(tree.complete).toBe(true)
    expect(tree.incompleteNotice).toBeNull()
  })

  it('links structural parent/children edges and marks rows with children', () => {
    const tree = buildLineageTree(multiLevelTrace(), 'target')
    const byId = new Map(tree.nodes.map((n) => [n.id, n]))
    expect(byId.get('root')!.parentId).toBeNull()
    expect(byId.get('p1')!.parentId).toBe('root')
    expect(byId.get('target')!.parentId).toBe('p1')
    expect(byId.get('c1')!.parentId).toBe('target')
    expect(byId.get('c1a')!.parentId).toBe('c1')
    expect(byId.get('c2')!.parentId).toBe('target')
    expect(tree.nodes.map((n) => n.hasChildren)).toEqual([true, true, true, true, false, false])
  })

  it('highlights exactly the current session (target here) and carries record flags', () => {
    const tree = buildLineageTree(multiLevelTrace(), 'target')
    expect(tree.nodes.filter((n) => n.isCurrent).map((n) => n.id)).toEqual(['target'])
    expect(tree.nodes.find((n) => n.id === 'target')!.isTarget).toBe(true)
    expect(tree.nodes.find((n) => n.id === 'c1')!.live).toBe(true)
    expect(tree.nodes.find((n) => n.id === 'root')!.live).toBe(false)
  })

  it('can highlight a non-target node (panel opened while inspecting a child)', () => {
    const tree = buildLineageTree(multiLevelTrace(), 'c1a')
    expect(tree.nodes.filter((n) => n.isCurrent).map((n) => n.id)).toEqual(['c1a'])
    expect(tree.nodes.find((n) => n.id === 'target')!.isCurrent).toBe(false)
  })

  it('highlights nothing when currentSessionId is null', () => {
    const tree = buildLineageTree(multiLevelTrace(), null)
    expect(tree.nodes.some((n) => n.isCurrent)).toBe(false)
  })

  it('renders a single-node trace (no ancestors, no descendants)', () => {
    const tree = buildLineageTree(
      { target: record('solo'), ancestors: [], descendants: [], complete: true },
      'solo',
    )
    expect(tree.nodes).toHaveLength(1)
    const only = tree.nodes[0]!
    expect(only.id).toBe('solo')
    expect(only.role).toBe('target')
    expect(only.depth).toBe(0)
    expect(only.parentId).toBeNull()
    expect(only.hasChildren).toBe(false)
    expect(only.isCurrent).toBe(true)
    expect(tree.maxDepth).toBe(0)
  })

  it('keeps unlinked ancestors above the resolved chain and raises the incomplete notice', () => {
    const tree = buildLineageTree(
      {
        target: record('target', 'missing'),
        // 'orphan' is reported as an ancestor but no parentSession link
        // reaches it from the target (the chain breaks at 'missing').
        ancestors: [record('orphan')],
        descendants: [],
        complete: false,
        unresolvedParentId: 'missing',
      },
      'target',
    )
    expect(tree.nodes.map((n) => n.id)).toEqual(['orphan', 'target'])
    expect(tree.nodes.map((n) => n.depth)).toEqual([0, 1])
    expect(tree.complete).toBe(false)
    expect(tree.unresolvedParentId).toBe('missing')
    expect(tree.incompleteNotice).toBe(
      formatTemplate(DSH_TOOLS_STRINGS.lineage.incompleteWithId, { id: 'missing' }),
    )
  })

  it('uses the generic incomplete notice when no unresolvedParentId is reported', () => {
    const tree = buildLineageTree(
      { target: record('t'), ancestors: [], descendants: [], complete: false },
      't',
    )
    expect(tree.incompleteNotice).toBe(DSH_TOOLS_STRINGS.lineage.incomplete)
  })

  it('abbreviates long ids in shortId but keeps the full id', () => {
    const longId = 'a'.repeat(30)
    const tree = buildLineageTree(
      { target: record(longId), ancestors: [], descendants: [], complete: true },
      null,
    )
    expect(tree.nodes[0]!.id).toBe(longId)
    expect(tree.nodes[0]!.shortId).toBe(abbreviateId(longId))
    expect(tree.nodes[0]!.shortId).toContain('…')
  })
})

// ---------------------------------------------------------------------------
// visibleLineageNodes (component-owned collapse over the flattened rows).
// ---------------------------------------------------------------------------

describe('visibleLineageNodes', () => {
  const tree = buildLineageTree(multiLevelTrace(), 'target')

  it('returns every row when nothing is collapsed', () => {
    expect(visibleLineageNodes(tree.nodes, new Set())).toHaveLength(6)
  })

  it('collapsing the target hides all descendants but keeps the spine', () => {
    const visible = visibleLineageNodes(tree.nodes, new Set(['target']))
    expect(visible.map((n) => n.id)).toEqual(['root', 'p1', 'target'])
  })

  it('collapsing a mid-branch node hides only its subtree', () => {
    const visible = visibleLineageNodes(tree.nodes, new Set(['c1']))
    expect(visible.map((n) => n.id)).toEqual(['root', 'p1', 'target', 'c1', 'c2'])
  })

  it('collapsing the root hides everything deeper', () => {
    const visible = visibleLineageNodes(tree.nodes, new Set(['root']))
    expect(visible.map((n) => n.id)).toEqual(['root'])
  })

  it('a collapsed id inside an already-hidden subtree changes nothing extra', () => {
    const visible = visibleLineageNodes(tree.nodes, new Set(['target', 'c1']))
    expect(visible.map((n) => n.id)).toEqual(['root', 'p1', 'target'])
  })

  it('collapsed ids on leaf rows are ignored (no children to hide)', () => {
    const visible = visibleLineageNodes(tree.nodes, new Set(['c2']))
    expect(visible).toHaveLength(6)
  })
})

// ---------------------------------------------------------------------------
// Provenance jump resolution.
// ---------------------------------------------------------------------------

describe('resolveJumpTarget', () => {
  it('clicking another node selects that session', () => {
    expect(resolveJumpTarget('parent', 'child')).toEqual({
      kind: 'select',
      sessionId: 'parent',
    })
  })

  it('clicking the current session is a no-op', () => {
    expect(resolveJumpTarget('same', 'same')).toEqual({ kind: 'current' })
  })

  it('selects when no current session is known', () => {
    expect(resolveJumpTarget('any', null)).toEqual({ kind: 'select', sessionId: 'any' })
  })
})

// ---------------------------------------------------------------------------
// External-agent (non-dsh) degradation model.
// ---------------------------------------------------------------------------

describe('externalLineageFallback', () => {
  it('returns null for dsh sessions (proceed to fetch), tolerating case/whitespace', () => {
    expect(externalLineageFallback('dsh')).toBeNull()
    expect(externalLineageFallback(' DSH ')).toBeNull()
  })

  it('synthesizes the not_dsh_session degradation for external agents', () => {
    for (const agent of ['claude', 'codex', 'cursor-cli', 'copilot', 'kimi', '']) {
      expect(externalLineageFallback(agent), agent).toEqual({
        available: false,
        trace: null,
        reason: 'not_dsh_session',
      })
    }
  })

  it('its output renders the dsh-exclusive card verbatim through the view resolver', () => {
    const fallback = externalLineageFallback('claude')!
    const view = deriveLineageView({
      loading: false,
      error: null,
      available: fallback.available,
      reason: fallback.reason,
      trace: fallback.trace,
      currentSessionId: 'x',
    })
    expect(view.kind).toBe('degraded')
    if (view.kind !== 'degraded') return
    expect(view.card.title).toBe('谱系/溯源为 dsh 会话专属')
  })
})

// ---------------------------------------------------------------------------
// available:false per-reason presentation.
// ---------------------------------------------------------------------------

describe('lineageDegradeCard', () => {
  it('maps each known reason to its own card copy', () => {
    const strings = DSH_TOOLS_STRINGS.lineage.degrade
    expect(lineageDegradeCard('not_dsh_session')).toMatchObject({
      reason: 'not_dsh_session',
      title: strings.notDshTitle,
      detail: null,
    })
    expect(lineageDegradeCard('session_query_unavailable')).toMatchObject({
      reason: 'session_query_unavailable',
      title: strings.queryUnavailableTitle,
    })
    expect(lineageDegradeCard('trace_failed', 'unknown session')).toMatchObject({
      reason: 'trace_failed',
      title: strings.traceFailedTitle,
      detail: 'unknown session',
    })
  })

  it('falls back to the generic card for unknown reasons, carrying the raw reason', () => {
    const card = lineageDegradeCard('future_reason')
    expect(card.reason).toBe('unknown')
    expect(card.title).toBe(DSH_TOOLS_STRINGS.lineage.degrade.unknownTitle)
    expect(card.body).toContain('future_reason')
    const nullCard = lineageDegradeCard(null)
    expect(nullCard.reason).toBe('unknown')
    expect(nullCard.body).toContain('?')
  })
})

describe('deriveLineageView', () => {
  const base = {
    loading: false,
    error: null,
    available: true,
    reason: null,
    detail: null,
    trace: null,
    currentSessionId: null,
  }

  it('loading outranks everything else', () => {
    const view = deriveLineageView({
      ...base,
      loading: true,
      error: 'boom',
      available: false,
      reason: 'trace_failed',
    })
    expect(view).toEqual({ kind: 'loading', text: DSH_TOOLS_STRINGS.lineage.loading })
  })

  it('a transport error outranks degradation and carries the detail', () => {
    const view = deriveLineageView({
      ...base,
      error: 'request_timeout',
      available: false,
      reason: 'trace_failed',
    })
    expect(view).toEqual({
      kind: 'error',
      text: DSH_TOOLS_STRINGS.lineage.error,
      detail: 'request_timeout',
    })
  })

  it('available:false is presented as degradation data, per reason', () => {
    for (const reason of ['session_query_unavailable', 'trace_failed', 'not_dsh_session']) {
      const view = deriveLineageView({ ...base, available: false, reason })
      expect(view.kind, reason).toBe('degraded')
      if (view.kind === 'degraded') expect(view.card.reason).toBe(reason)
    }
  })

  it('available:true without a trace body is the honest empty state', () => {
    expect(deriveLineageView(base)).toEqual({
      kind: 'empty',
      text: DSH_TOOLS_STRINGS.lineage.empty,
    })
  })

  it('a present trace resolves to the built tree with the highlight applied', () => {
    const view = deriveLineageView({
      ...base,
      trace: multiLevelTrace(),
      currentSessionId: 'c2',
    })
    expect(view.kind).toBe('tree')
    if (view.kind !== 'tree') return
    expect(view.tree.nodeCount).toBe(6)
    expect(view.tree.nodes.filter((n) => n.isCurrent).map((n) => n.id)).toEqual(['c2'])
  })
})

// ---------------------------------------------------------------------------
// Snippet highlighting.
// ---------------------------------------------------------------------------

describe('highlightSnippet', () => {
  it('splits case-insensitively around every occurrence, preserving original case', () => {
    expect(highlightSnippet('…the hit… and HIT again', 'Hit')).toEqual([
      { text: '…the ', highlight: false },
      { text: 'hit', highlight: true },
      { text: '… and ', highlight: false },
      { text: 'HIT', highlight: true },
      { text: ' again', highlight: false },
    ])
  })

  it('handles matches at the start, the end, and the whole string', () => {
    expect(highlightSnippet('abc tail', 'abc')).toEqual([
      { text: 'abc', highlight: true },
      { text: ' tail', highlight: false },
    ])
    expect(highlightSnippet('head abc', 'abc')).toEqual([
      { text: 'head ', highlight: false },
      { text: 'abc', highlight: true },
    ])
    expect(highlightSnippet('abc', 'abc')).toEqual([{ text: 'abc', highlight: true }])
  })

  it('yields one plain segment when the query is blank or never matches', () => {
    expect(highlightSnippet('some text', '')).toEqual([{ text: 'some text', highlight: false }])
    expect(highlightSnippet('some text', '   ')).toEqual([
      { text: 'some text', highlight: false },
    ])
    expect(highlightSnippet('some text', 'zzz')).toEqual([
      { text: 'some text', highlight: false },
    ])
  })

  it('yields nothing for an empty snippet', () => {
    expect(highlightSnippet('', 'x')).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Search normalization.
// ---------------------------------------------------------------------------

function searchResponse(over: Partial<SearchResponseVM> = {}): SearchResponseVM {
  return {
    mode: 'full-text',
    query: 'hit',
    project: null,
    items: [],
    ...over,
  }
}

describe('normalizeSearchItems', () => {
  it('splits full-text snippets against the echoed query and labels the match', () => {
    const items = normalizeSearchItems(
      searchResponse({
        items: [
          {
            session: {
              agent: 'dsh',
              sessionId: 'dsh1',
              title: 'dsh run',
              project: '/tmp/proj',
              status: 'working',
            },
            matchedBy: 'full-text',
            snippet: '…the hit…',
          },
        ],
      }),
    )
    expect(items).toHaveLength(1)
    const item = items[0]!
    expect(item.sessionId).toBe('dsh1')
    expect(item.matchedBy).toBe('full-text')
    expect(item.matchedByLabel).toBe(DSH_TOOLS_STRINGS.search.matchedBy['full-text'])
    expect(item.snippet).toEqual([
      { text: '…the ', highlight: false },
      { text: 'hit', highlight: true },
      { text: '…', highlight: false },
    ])
  })

  it('keeps filter matches (title/project) snippet-less with their own labels', () => {
    const items = normalizeSearchItems(
      searchResponse({
        mode: 'filter-only',
        query: 'fix',
        items: [
          {
            session: {
              agent: 'claude',
              sessionId: 'a1',
              title: 'fix the parser',
              project: '/tmp/proj',
              status: 'waiting',
            },
            matchedBy: 'title',
            snippet: null,
          },
          {
            session: {
              agent: 'codex',
              sessionId: 'b1',
              title: 'other',
              project: '/work/fix-it',
              status: 'idle',
            },
            matchedBy: 'project',
            snippet: null,
          },
        ],
      }),
    )
    expect(items.map((i) => i.matchedBy)).toEqual(['title', 'project'])
    expect(items.map((i) => i.matchedByLabel)).toEqual([
      DSH_TOOLS_STRINGS.search.matchedBy.title,
      DSH_TOOLS_STRINGS.search.matchedBy.project,
    ])
    expect(items.every((i) => i.snippet === null)).toBe(true)
  })

  it('maps unknown matchedBy values to the honest other tag', () => {
    const items = normalizeSearchItems(
      searchResponse({
        items: [
          {
            session: {
              agent: 'dsh',
              sessionId: 's',
              title: 't',
              project: '/p',
              status: 'idle',
            },
            matchedBy: 'fuzzy-v2',
            snippet: null,
          },
        ],
      }),
    )
    expect(items[0]!.matchedBy).toBe('other')
    expect(items[0]!.matchedByLabel).toBe(DSH_TOOLS_STRINGS.search.matchedBy.other)
  })

  it('applies the untitled fallback and abbreviates long ids', () => {
    const longId = 'x'.repeat(40)
    const items = normalizeSearchItems(
      searchResponse({
        items: [
          {
            session: {
              agent: 'dsh',
              sessionId: longId,
              title: '   ',
              project: '/p',
              status: 'idle',
            },
            matchedBy: 'title',
            snippet: null,
          },
        ],
      }),
    )
    expect(items[0]!.titleLabel).toBe(DSH_TOOLS_STRINGS.search.untitled)
    expect(items[0]!.title).toBe('   ')
    expect(items[0]!.shortId).toBe(abbreviateId(longId))
  })

  it('treats an empty-string snippet like a missing one', () => {
    const items = normalizeSearchItems(
      searchResponse({
        items: [
          {
            session: {
              agent: 'dsh',
              sessionId: 's',
              title: 't',
              project: '/p',
              status: 'idle',
            },
            matchedBy: 'full-text',
            snippet: '',
          },
        ],
      }),
    )
    expect(items[0]!.snippet).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// filter-only degradation bar + search panel view resolver.
// ---------------------------------------------------------------------------

describe('searchDegradeNotice', () => {
  it('emits the task-spec degradation copy verbatim in filter-only mode', () => {
    expect(searchDegradeNotice('filter-only')).toBe(
      'dsh 全文检索不可用,已降级为标题/项目过滤',
    )
  })

  it('emits nothing when full-text search is up', () => {
    expect(searchDegradeNotice('full-text')).toBeNull()
  })
})

describe('deriveSearchView', () => {
  const base = {
    loading: false,
    error: null,
    mode: 'full-text' as const,
    itemCount: 0,
    query: '',
  }

  it('resolves loading > error > results > empty > idle in that order', () => {
    expect(deriveSearchView({ ...base, loading: true, error: 'x', itemCount: 3 }).body).toBe(
      'loading',
    )
    expect(deriveSearchView({ ...base, error: 'boom', itemCount: 3 }).body).toBe('error')
    expect(deriveSearchView({ ...base, itemCount: 3 }).body).toBe('results')
    expect(deriveSearchView({ ...base, query: 'needle' }).body).toBe('empty')
    expect(deriveSearchView(base).body).toBe('idle')
    expect(deriveSearchView({ ...base, query: '   ' }).body).toBe('idle')
  })

  it('carries the localized texts and the error detail', () => {
    expect(deriveSearchView({ ...base, loading: true }).text).toBe(
      DSH_TOOLS_STRINGS.search.loading,
    )
    const errorView = deriveSearchView({ ...base, error: 'network_error' })
    expect(errorView.text).toBe(DSH_TOOLS_STRINGS.search.error)
    expect(errorView.detail).toBe('network_error')
    expect(deriveSearchView({ ...base, query: 'q' }).text).toBe(DSH_TOOLS_STRINGS.search.empty)
  })

  it('shows the degradation notice in every filter-only body state', () => {
    const notice = DSH_TOOLS_STRINGS.search.filterOnlyNotice
    for (const input of [
      { ...base, mode: 'filter-only' as const, loading: true },
      { ...base, mode: 'filter-only' as const, error: 'x' },
      { ...base, mode: 'filter-only' as const, itemCount: 2 },
      { ...base, mode: 'filter-only' as const, query: 'q' },
      { ...base, mode: 'filter-only' as const },
    ]) {
      expect(deriveSearchView(input).notice).toBe(notice)
    }
    expect(deriveSearchView({ ...base, itemCount: 2 }).notice).toBeNull()
  })
})
