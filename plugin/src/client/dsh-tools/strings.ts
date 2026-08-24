/**
 * Centralized user-facing strings for the dsh deep-query tools (design
 * §5.1 view 2 dsh 会话专属区: lineage tree / provenance jump / full-text
 * search; §5.3 honest degradation wording).
 *
 * Deliberately a MODULE-LOCAL table (task T5.4 constraint): the main
 * locale table (client/locales) and test/locales.test.ts are untouched —
 * S7 merges this table into the locale registry in one move. Chinese is
 * the primary UI language, same posture as board/strings.ts. Templates
 * use `{name}` placeholders resolved by `formatTemplate` in logic.ts.
 *
 * Pure data module: no imports, no logic.
 *
 * @module
 */

export const DSH_TOOLS_STRINGS = {
  /** Lineage tree panel (dsh 谱系/溯源). */
  lineage: {
    title: '会话谱系',
    loading: '谱系加载中…',
    /** Transport/HTTP failure headline (the detail carries the cause). */
    error: '谱系加载失败',
    /** available:true but the trace body is missing — honest empty state. */
    empty: '暂无谱系数据',
    /** Badge on the session the user is currently inspecting. */
    currentBadge: '当前会话',
    /** Badge on records live in this dsh process. */
    liveBadge: '运行中',
    /** Badge on records the trace knows but dsh has not persisted. */
    notPersistedBadge: '未持久化',
    /** Role labels for tree rows. */
    role: {
      ancestor: '祖先',
      target: '目标',
      descendant: '子会话',
    },
    /** Hover title for a clickable node (provenance jump). */
    jumpTitle: '跳转到该会话',
    /** Hover title for the highlighted current node (no jump). */
    currentTitle: '正在查看该会话',
    expand: '展开',
    collapse: '折叠',
    nodeCount: '{n} 个会话',
    /** trace.complete === false with a known unresolved parent id. */
    incompleteWithId: '谱系不完整:父会话 {id} 无法解析',
    /** trace.complete === false without an unresolved parent id. */
    incomplete: '谱系不完整:部分父链无法解析',
    /** Degradation cards — available:false is data, not an error (§4.e.4). */
    degrade: {
      /** Client-side reason for sessions of non-dsh agents (task spec copy). */
      notDshTitle: '谱系/溯源为 dsh 会话专属',
      notDshBody: '当前会话来自外部 agent,dsh 的谱系与溯源能力不适用。',
      /** Backend reason `session_query_unavailable`. */
      queryUnavailableTitle: 'dsh 谱系服务不可用',
      queryUnavailableBody:
        '当前 dsh 组合未挂载 sessionQuery,谱系与溯源暂不可用。',
      /** Backend reason `trace_failed`. */
      traceFailedTitle: '谱系追溯失败',
      traceFailedBody: 'dsh 无法解析该会话的谱系。',
      /** Any reason outside the known vocabulary (never invent a state). */
      unknownTitle: '谱系不可用',
      unknownBody: '后端报告谱系不可用(原因: {reason})。',
    },
  },
  /** Cross-agent search panel (dsh 全文检索 + 降级过滤). */
  search: {
    title: '会话检索',
    placeholder: '检索会话(标题 / 项目 / 全文)',
    submit: '检索',
    loading: '检索中…',
    error: '检索失败',
    /** Query submitted, zero hits. */
    empty: '没有匹配的会话',
    /** mode: 'filter-only' degradation bar (task spec copy, verbatim). */
    filterOnlyNotice: 'dsh 全文检索不可用,已降级为标题/项目过滤',
    /** Active project-filter chip. */
    projectFilter: '项目过滤: {project}',
    /** matchedBy tags on result rows. */
    matchedBy: {
      'full-text': '全文',
      title: '标题',
      project: '项目',
      other: '其他',
    },
    untitled: '(无标题)',
  },
} as const

export type DshToolsStrings = typeof DSH_TOOLS_STRINGS
