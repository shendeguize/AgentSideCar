/**
 * Centralized user-facing strings for the session-detail view (design §5.1
 * view 2, §5.3 honesty wording). Deliberately a module-local table — NOT
 * part of the main locale registry (locales/zh.ts / locales/en.ts): the
 * locale tables are owned by parallel tasks and the S7 integration wave
 * folds this table in centrally. Same posture as board/strings.ts.
 *
 * Chinese is the primary UI language. Templates use `{name}` placeholders
 * resolved by `formatTemplate` in `logic.ts`.
 *
 * Pure data module: no imports, no logic.
 *
 * @module
 */

export const DETAIL_STRINGS = {
  /** Header block (session meta + controls). */
  header: {
    close: '返回看板',
    listenOn: '监听中',
    listenOff: '监听',
    listenHint: '开启后新事件将实时追加并高亮',
    untitled: '(无标题)',
    unknownProject: '未知项目',
    /** Design §5.3 / SKILL.md wording: statuses are inferred observations. */
    observedDisclaimer: '状态为从持久化数据推断的观察值,可能滞后',
  },
  /** Session status badge labels (observed values, see disclaimer). */
  status: {
    working: '工作中',
    waiting: '等待中',
    idle: '空闲',
    dead: '已结束',
    unknown: '未知',
  },
  /** Timeline source badges (provenance of the merged page, honest labels). */
  sources: {
    title: '数据来源',
    dshLive: 'dsh 实时',
    dshCold: 'dsh 冷读',
    sidecarReplay: 'sidecar 重放',
    sidecarBuffer: 'sidecar 缓冲',
    none: '来源未知',
  },
  /** Normalized event-kind labels; unknown kinds keep their raw text. */
  kind: {
    user: '用户消息',
    assistant: '助手回复',
    thinking: '思考',
    toolCall: '工具调用',
    toolResult: '工具结果',
    turn: '回合',
    step: '步骤',
    error: '错误',
    other: '事件',
  },
  /** Seq-discontinuity marker row (design §4.b.3 honest presentation). */
  gap: {
    label: '缺口:可能有 {n} 条事件未捕获(256 队列上限或未持久化)',
  },
  /** Timeline list chrome. */
  timeline: {
    loadMore: '加载更多历史',
    loadingMore: '加载中…',
    noMore: '已到时间线起点',
    expand: '展开',
    collapse: '收起',
    newBadge: '新',
    seq: 'seq {n}',
    hiddenNotice: '为保持流畅,较早的 {n} 条已折叠',
    showAll: '全部显示',
  },
  /** Loading / empty / error body states. */
  states: {
    loadingTitle: '正在加载时间线…',
    emptyTitle: '暂无事件',
    emptyHint: '该会话还没有可展示的规范化事件。',
    errorTitle: '时间线加载失败',
    /** Fallback template when the reason code has no friendly mapping. */
    errorFallback: '错误码:{reason}',
    errors: {
      session_not_found: '会话不存在或已不可见',
      invalid_cursor: '分页游标无效,请重新打开详情',
      fusion_not_wired: '当前 host 未启用时间线能力',
      network_error: '网络错误,无法联系 dsh host',
      request_timeout: '请求超时',
    },
  },
  /** Relative time templates (coarse buckets, matches the board wording). */
  time: {
    justNow: '刚刚',
    minutesAgo: '{n} 分钟前',
    hoursAgo: '{n} 小时前',
    daysAgo: '{n} 天前',
  },
} as const

export type DetailStrings = typeof DETAIL_STRINGS
