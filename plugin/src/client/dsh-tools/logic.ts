/**
 * Pure view-model logic for the dsh deep-query tools (design §5.1 view 2
 * dsh 会话专属区 / M3): lineage trace → tree view model, provenance-jump
 * resolution, search-result normalization (snippet highlighting), and the
 * honest-degradation models (§5.3: degradation is presented, never faked).
 *
 * No React, no I/O, no imports from the data layer — everything arrives
 * and leaves as plain values, so this module is unit-testable in a bare
 * node environment (same posture as board/logic.ts and inject/logic.ts).
 *
 * Decoupling contract: the wire-facing types below are this module's OWN
 * hand-written mirrors of the host read contract (task constraint —
 * components never import host code):
 * - {@link LineageResponseVM} mirrors `GET <prefix>/lineage/<id>` — always
 *   200 `{available, trace, reason, detail?}` (routes.ts handleLineage over
 *   fusion.ts LineageResult); `available:false` is DATA, not an error.
 * - {@link LineageTraceVM} mirrors fusion.ts DshLineageTraceFace (the
 *   `SessionLineageTrace` subset from dsh-session-query traceSession).
 * - {@link SearchResponseVM} mirrors `GET <prefix>/search?q=&project=&limit=`
 *   (routes.ts handleSearch over fusion.ts searchSessions): `mode:
 *   'filter-only'` means the sessionQuery full-text engine is unavailable
 *   and the backend degraded to title/project filtering.
 *
 * @module
 */

import { DSH_TOOLS_STRINGS } from './strings.ts'

// ---------------------------------------------------------------------------
// Small shared helpers.
// ---------------------------------------------------------------------------

/** Resolve `{name}` placeholders in a message template (board convention). */
export function formatTemplate(
  template: string,
  params: Record<string, string | number>,
): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) => {
    const value = params[key]
    return value === undefined ? match : String(value)
  })
}

/** Head…tail abbreviation for long session ids (same rule as the board). */
export function abbreviateId(id: string, max = 20): string {
  if (id.length <= max) return id
  return `${id.slice(0, 12)}…${id.slice(-6)}`
}

// ---------------------------------------------------------------------------
// Lineage wire mirrors (host source of truth noted per type).
// ---------------------------------------------------------------------------

/** `SessionRecord` subset inside a trace (host: fusion.ts DshLineageRecordFace). */
export interface LineageRecordVM {
  header: {
    id: string
    createdAt: number
    cwd?: string
    parentSession?: string
  }
  /** True when the session is live in the dsh process. */
  live: boolean
  /** True when dsh has persisted the session log. */
  persisted: boolean
}

/** One descendant branch (host: fusion.ts DshLineageNodeFace). */
export interface LineageBranchVM {
  session: LineageRecordVM
  descendants: readonly LineageBranchVM[]
}

/** The trace body (host: fusion.ts DshLineageTraceFace ← traceSession). */
export interface LineageTraceVM {
  target: LineageRecordVM
  ancestors: readonly LineageRecordVM[]
  descendants: readonly LineageBranchVM[]
  complete: boolean
  root?: LineageRecordVM
  unresolvedParentId?: string
}

/** Backend degradation vocabulary (host: fusion.ts LineageResult.reason). */
export type LineageBackendReason = 'session_query_unavailable' | 'trace_failed'

/**
 * Client-side degradation vocabulary: the backend reasons plus
 * `not_dsh_session`, minted locally by {@link externalLineageFallback} for
 * sessions of non-dsh agents (lineage is a dsh-only capability, so the
 * integration never even calls the backend for them).
 */
export type LineageDegradeReason = LineageBackendReason | 'not_dsh_session'

/** Body of `GET lineage/<id>` — always HTTP 200 (host: routes.ts). */
export interface LineageResponseVM {
  available: boolean
  trace: LineageTraceVM | null
  reason: LineageBackendReason | null
  detail?: string
}

// ---------------------------------------------------------------------------
// Lineage trace → tree view model.
// ---------------------------------------------------------------------------

/** Position of a node relative to the traced session. */
export type LineageRole = 'ancestor' | 'target' | 'descendant'

/** One flattened tree row, ready to render (indent by `depth`). */
export interface LineageNodeVM {
  id: string
  role: LineageRole
  roleLabel: string
  /** Indentation level; the outermost ancestor (or the target) is 0. */
  depth: number
  /** Structural tree parent (render edge), null for the outermost row. */
  parentId: string | null
  /** True when deeper rows directly under this one exist (collapse handle). */
  hasChildren: boolean
  /** True when this node IS the session the user is inspecting (highlight). */
  isCurrent: boolean
  isTarget: boolean
  live: boolean
  persisted: boolean
  shortId: string
  cwd: string | null
  createdAt: number
}

/** The whole lineage tree render model. */
export interface LineageTreeVM {
  /** Rows in render order: ancestors root-first, target, descendants DFS. */
  nodes: LineageNodeVM[]
  targetId: string
  complete: boolean
  unresolvedParentId: string | null
  /** Honest incompleteness banner; null when the trace is complete. */
  incompleteNotice: string | null
  nodeCount: number
  maxDepth: number
}

function toNode(
  record: LineageRecordVM,
  role: LineageRole,
  depth: number,
  parentId: string | null,
  currentSessionId: string | null,
): LineageNodeVM {
  return {
    id: record.header.id,
    role,
    roleLabel: DSH_TOOLS_STRINGS.lineage.role[role],
    depth,
    parentId,
    hasChildren: false,
    isCurrent: currentSessionId !== null && record.header.id === currentSessionId,
    isTarget: role === 'target',
    live: record.live,
    persisted: record.persisted,
    shortId: abbreviateId(record.header.id),
    cwd: record.header.cwd ?? null,
    createdAt: record.header.createdAt,
  }
}

/**
 * Order the ancestors root-first by walking `header.parentSession` links
 * up from the target. Ancestors the links cannot reach (broken chain —
 * the trace then also carries `complete:false`) are still shown: they sit
 * above the resolved chain in their reported order, since everything the
 * backend lists in `ancestors` IS an ancestor even when the intermediate
 * links are unresolved.
 */
function orderAncestors(trace: LineageTraceVM): LineageRecordVM[] {
  const byId = new Map<string, LineageRecordVM>()
  for (const record of trace.ancestors) byId.set(record.header.id, record)
  const chain: LineageRecordVM[] = [] // nearest-first
  const visited = new Set<string>()
  let cursor = trace.target.header.parentSession
  while (cursor !== undefined && !visited.has(cursor)) {
    const record = byId.get(cursor)
    if (record === undefined) break
    visited.add(cursor)
    chain.push(record)
    cursor = record.header.parentSession
  }
  const leftovers = trace.ancestors.filter((r) => !visited.has(r.header.id))
  return [...leftovers, ...chain.reverse()]
}

/**
 * Lineage trace → flattened tree view model. Ancestors form the spine
 * (root-most at depth 0), the target follows, and the descendant branches
 * are appended depth-first in reported order. `currentSessionId` marks the
 * highlight (usually the target, but a lineage panel opened from another
 * node keeps whatever the owner is inspecting).
 */
export function buildLineageTree(
  trace: LineageTraceVM,
  currentSessionId: string | null,
): LineageTreeVM {
  const nodes: LineageNodeVM[] = []

  const ancestors = orderAncestors(trace)
  let parentId: string | null = null
  let depth = 0
  for (const record of ancestors) {
    nodes.push(toNode(record, 'ancestor', depth, parentId, currentSessionId))
    parentId = record.header.id
    depth += 1
  }

  nodes.push(toNode(trace.target, 'target', depth, parentId, currentSessionId))
  const targetDepth = depth

  const walk = (branch: LineageBranchVM, branchDepth: number, branchParent: string): void => {
    nodes.push(
      toNode(branch.session, 'descendant', branchDepth, branchParent, currentSessionId),
    )
    for (const child of branch.descendants) {
      walk(child, branchDepth + 1, branch.session.header.id)
    }
  }
  for (const branch of trace.descendants) {
    walk(branch, targetDepth + 1, trace.target.header.id)
  }

  // hasChildren over the flattened DFS order: a row has children exactly
  // when the next row is one level deeper.
  let maxDepth = 0
  for (let i = 0; i < nodes.length; i += 1) {
    const node = nodes[i]
    if (node === undefined) continue
    const next = nodes[i + 1]
    node.hasChildren = next !== undefined && next.depth > node.depth
    if (node.depth > maxDepth) maxDepth = node.depth
  }

  const unresolvedParentId = trace.unresolvedParentId ?? null
  const incompleteNotice = trace.complete
    ? null
    : unresolvedParentId !== null
      ? formatTemplate(DSH_TOOLS_STRINGS.lineage.incompleteWithId, {
          id: abbreviateId(unresolvedParentId),
        })
      : DSH_TOOLS_STRINGS.lineage.incomplete

  return {
    nodes,
    targetId: trace.target.header.id,
    complete: trace.complete,
    unresolvedParentId,
    incompleteNotice,
    nodeCount: nodes.length,
    maxDepth,
  }
}

/**
 * Apply component-owned collapse state over the flattened rows: a
 * collapsed row keeps itself but hides every deeper row until the DFS
 * order returns to its level.
 */
export function visibleLineageNodes(
  nodes: readonly LineageNodeVM[],
  collapsedIds: ReadonlySet<string>,
): LineageNodeVM[] {
  const out: LineageNodeVM[] = []
  let hideDeeperThan: number | null = null
  for (const node of nodes) {
    if (hideDeeperThan !== null) {
      if (node.depth > hideDeeperThan) continue
      hideDeeperThan = null
    }
    out.push(node)
    if (node.hasChildren && collapsedIds.has(node.id)) {
      hideDeeperThan = node.depth
    }
  }
  return out
}

// ---------------------------------------------------------------------------
// Provenance jump (点击谱系节点 → onSelectSession(id)).
// ---------------------------------------------------------------------------

/** What a click on a lineage node should do. */
export type LineageJump =
  | { kind: 'select'; sessionId: string }
  /** The node is the session already on screen — highlight, no navigation. */
  | { kind: 'current' }

/** Resolve the jump target of a lineage-node click. */
export function resolveJumpTarget(
  nodeId: string,
  currentSessionId: string | null,
): LineageJump {
  if (currentSessionId !== null && nodeId === currentSessionId) {
    return { kind: 'current' }
  }
  return { kind: 'select', sessionId: nodeId }
}

// ---------------------------------------------------------------------------
// External-agent degradation (lineage is dsh-exclusive).
// ---------------------------------------------------------------------------

const DSH_AGENT = 'dsh'

/**
 * Client-side pre-check for the lineage panel: sessions of non-dsh agents
 * get a synthetic degraded response (reason `not_dsh_session`) and the
 * integration never calls the backend for them; dsh sessions return null
 * (proceed to fetch). Honest presentation, not an error (§5.3).
 */
export function externalLineageFallback(
  agent: string,
): { available: false; trace: null; reason: 'not_dsh_session' } | null {
  if (agent.trim().toLowerCase() === DSH_AGENT) return null
  return { available: false, trace: null, reason: 'not_dsh_session' }
}

// ---------------------------------------------------------------------------
// Degradation cards and the lineage view resolver.
// ---------------------------------------------------------------------------

/** Render model of one degradation card (available:false presentations). */
export interface LineageDegradeCardVM {
  reason: LineageDegradeReason | 'unknown'
  title: string
  body: string
  /** Backend `detail` passthrough (trace_failed carries the engine error). */
  detail: string | null
}

/**
 * Degradation reason → card copy. Reasons outside the known vocabulary
 * (a newer backend) fall back to the generic card carrying the raw reason
 * — the panel never invents a state.
 */
export function lineageDegradeCard(
  reason: string | null | undefined,
  detail?: string | null,
): LineageDegradeCardVM {
  const strings = DSH_TOOLS_STRINGS.lineage.degrade
  const detailOut = detail ?? null
  switch (reason) {
    case 'not_dsh_session':
      return { reason, title: strings.notDshTitle, body: strings.notDshBody, detail: detailOut }
    case 'session_query_unavailable':
      return {
        reason,
        title: strings.queryUnavailableTitle,
        body: strings.queryUnavailableBody,
        detail: detailOut,
      }
    case 'trace_failed':
      return {
        reason,
        title: strings.traceFailedTitle,
        body: strings.traceFailedBody,
        detail: detailOut,
      }
    default:
      return {
        reason: 'unknown',
        title: strings.unknownTitle,
        body: formatTemplate(strings.unknownBody, { reason: reason ?? '?' }),
        detail: detailOut,
      }
  }
}

/** Discriminated render state of the lineage panel. */
export type LineageViewState =
  | { kind: 'loading'; text: string }
  /** Transport/HTTP failure (unlike available:false, this IS an error). */
  | { kind: 'error'; text: string; detail: string | null }
  | { kind: 'degraded'; card: LineageDegradeCardVM }
  /** available:true but no trace body — honest empty state. */
  | { kind: 'empty'; text: string }
  | { kind: 'tree'; tree: LineageTreeVM }

/** Everything the lineage panel receives (mirrors LineageTree props). */
export interface LineageViewInput {
  loading: boolean
  /** Transport-level failure text from the integration, or null. */
  error: string | null
  available: boolean
  /** Open vocabulary on purpose: newer backend reasons degrade gracefully. */
  reason?: string | null
  detail?: string | null
  trace: LineageTraceVM | null
  currentSessionId: string | null
}

/**
 * Resolve what the lineage panel shows, in priority order:
 * loading > transport error > degraded (available:false, per-reason card)
 * > empty (no trace body) > the tree.
 */
export function deriveLineageView(input: LineageViewInput): LineageViewState {
  if (input.loading) {
    return { kind: 'loading', text: DSH_TOOLS_STRINGS.lineage.loading }
  }
  if (input.error !== null) {
    return { kind: 'error', text: DSH_TOOLS_STRINGS.lineage.error, detail: input.error }
  }
  if (!input.available) {
    return { kind: 'degraded', card: lineageDegradeCard(input.reason, input.detail) }
  }
  if (input.trace === null) {
    return { kind: 'empty', text: DSH_TOOLS_STRINGS.lineage.empty }
  }
  return { kind: 'tree', tree: buildLineageTree(input.trace, input.currentSessionId) }
}

// ---------------------------------------------------------------------------
// Search wire mirrors.
// ---------------------------------------------------------------------------

/** Search mode: 'filter-only' is the sessionQuery-absent degradation. */
export type SearchMode = 'full-text' | 'filter-only'

/** UnifiedSession subset the search rows render (host: fusion.ts). */
export interface SearchSessionVM {
  agent: string
  sessionId: string
  title: string
  project: string
  status: string
}

/** One raw search hit (host: routes.ts handleSearch item). */
export interface SearchHitVM {
  session: SearchSessionVM
  /** 'full-text' | 'title' | 'project' today; open for newer backends. */
  matchedBy: string
  /** Best-match excerpt; full-text hits only, null otherwise. */
  snippet: string | null
}

/** Body of `GET search?q=&project=&limit=` (host: routes.ts). */
export interface SearchResponseVM {
  mode: SearchMode
  query: string
  project: string | null
  items: readonly SearchHitVM[]
}

// ---------------------------------------------------------------------------
// Snippet highlighting (React-safe segments, no HTML injection).
// ---------------------------------------------------------------------------

/** One run of snippet text; `highlight` runs matched the query. */
export interface SnippetSegment {
  text: string
  highlight: boolean
}

/**
 * Split a snippet into plain/highlight segments around case-insensitive
 * occurrences of the (trimmed) query. Best-effort: the engine does not
 * mark its match, so a query the snippet spells differently simply yields
 * one plain segment. Empty snippet → no segments.
 */
export function highlightSnippet(snippet: string, query: string): SnippetSegment[] {
  if (snippet === '') return []
  const needle = query.trim().toLowerCase()
  if (needle === '') return [{ text: snippet, highlight: false }]
  const haystack = snippet.toLowerCase()
  const segments: SnippetSegment[] = []
  let pos = 0
  for (;;) {
    const hit = haystack.indexOf(needle, pos)
    if (hit === -1) break
    if (hit > pos) segments.push({ text: snippet.slice(pos, hit), highlight: false })
    segments.push({ text: snippet.slice(hit, hit + needle.length), highlight: true })
    pos = hit + needle.length
  }
  if (pos < snippet.length) segments.push({ text: snippet.slice(pos), highlight: false })
  return segments
}

// ---------------------------------------------------------------------------
// Search-result normalization.
// ---------------------------------------------------------------------------

/** Known matchedBy vocabulary plus the honest catch-all. */
export type MatchedByToken = 'full-text' | 'title' | 'project' | 'other'

const MATCHED_BY_TOKENS: readonly MatchedByToken[] = ['full-text', 'title', 'project']

/** One render-ready search result row. */
export interface SearchItemVM {
  sessionId: string
  agent: string
  /** Raw title (may be empty). */
  title: string
  /** Title with the untitled fallback applied. */
  titleLabel: string
  project: string
  status: string
  matchedBy: MatchedByToken
  matchedByLabel: string
  /** Pre-split highlight segments; null for non-full-text matches. */
  snippet: SnippetSegment[] | null
  shortId: string
}

/**
 * Wire hits → render rows. Snippets are split against the response's own
 * echoed query; matchedBy values outside the known vocabulary map to the
 * 'other' tag (label keeps a stable word, never invents a match kind).
 */
export function normalizeSearchItems(response: SearchResponseVM): SearchItemVM[] {
  return response.items.map((hit) => {
    const matchedBy = (MATCHED_BY_TOKENS as readonly string[]).includes(hit.matchedBy)
      ? (hit.matchedBy as MatchedByToken)
      : 'other'
    const title = hit.session.title
    return {
      sessionId: hit.session.sessionId,
      agent: hit.session.agent,
      title,
      titleLabel: title.trim() === '' ? DSH_TOOLS_STRINGS.search.untitled : title,
      project: hit.session.project,
      status: hit.session.status,
      matchedBy,
      matchedByLabel: DSH_TOOLS_STRINGS.search.matchedBy[matchedBy],
      snippet:
        hit.snippet === null || hit.snippet === ''
          ? null
          : highlightSnippet(hit.snippet, response.query),
      shortId: abbreviateId(hit.session.sessionId),
    }
  })
}

/**
 * The filter-only degradation bar text; null when full-text search is up.
 * Shown whenever the mode says so — the degradation is a capability fact,
 * independent of whether the current query has results.
 */
export function searchDegradeNotice(mode: SearchMode): string | null {
  return mode === 'filter-only' ? DSH_TOOLS_STRINGS.search.filterOnlyNotice : null
}

// ---------------------------------------------------------------------------
// Search panel view resolver.
// ---------------------------------------------------------------------------

/** Discriminated body state of the search panel. */
export type SearchBodyKind = 'loading' | 'error' | 'results' | 'empty' | 'idle'

export interface SearchViewState {
  body: SearchBodyKind
  /** Degradation bar text (independent of the body state), or null. */
  notice: string | null
  /** Localized text for loading/error/empty bodies, null otherwise. */
  text: string | null
  /** Transport error detail for the error body, null otherwise. */
  detail: string | null
}

/**
 * Resolve the search panel body: loading > error > results > empty (a
 * submitted non-blank query with zero hits) > idle (nothing asked yet).
 */
export function deriveSearchView(input: {
  loading: boolean
  error: string | null
  mode: SearchMode
  itemCount: number
  query: string
}): SearchViewState {
  const notice = searchDegradeNotice(input.mode)
  if (input.loading) {
    return { body: 'loading', notice, text: DSH_TOOLS_STRINGS.search.loading, detail: null }
  }
  if (input.error !== null) {
    return {
      body: 'error',
      notice,
      text: DSH_TOOLS_STRINGS.search.error,
      detail: input.error,
    }
  }
  if (input.itemCount > 0) return { body: 'results', notice, text: null, detail: null }
  if (input.query.trim() !== '') {
    return { body: 'empty', notice, text: DSH_TOOLS_STRINGS.search.empty, detail: null }
  }
  return { body: 'idle', notice, text: null, detail: null }
}
