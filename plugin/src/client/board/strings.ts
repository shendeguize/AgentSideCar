/**
 * Centralized user-facing strings for the board view and the footer widget
 * (design §5.1 view 1 / view 4, §5.3 honesty wording).
 *
 * Chinese is the primary UI language for now; every string the components
 * render lives in this single table so the i18n skeleton (T2.3,
 * `ctx.locale.register`) can take it over without hunting through JSX.
 * Templates use `{name}` placeholders resolved by `formatTemplate` in
 * `logic.ts` — plain message strings, locale-registry friendly.
 *
 * Pure data module: no imports, no logic.
 *
 * @module
 */

export const BOARD_STRINGS = {
  /** Session status badge labels (observed values, see hover disclaimer). */
  status: {
    working: '工作中',
    waiting: '等待中',
    idle: '空闲',
    dead: '已结束',
    unknown: '未知',
  },
  /** Badge attention markers; gap outranks stale (per-session > global). */
  attention: {
    gap: '事件缺口',
    stale: '可能滞后',
  },
  /** Daemon supervisor state labels for the top-bar badge. */
  daemon: {
    probe: '探测中',
    adopted: '已连接 · 领养',
    defer: '等待系统服务',
    reprobe: '重新探测中',
    hosting: '正在启动',
    hosted: '已连接 · 托管',
    backoff: '重启退避中',
    failed: '离线',
  },
  /** Stream health indicator labels. */
  stream: {
    ok: '实时流正常',
    degraded: '实时流重连中',
    unknown: '实时流未建立',
  },
  /** Top banner texts (design §5.3: degraded warning / FAILED red banner). */
  banner: {
    daemonFailed: 'sidecar 离线:看板显示最后一次快照,数据不再更新',
    streamDegraded: '实时流重连中,数据可能滞后',
  },
  /** Empty-state guidance blocks. */
  empty: {
    daemonFailedTitle: 'sidecar 已离线',
    daemonFailedHint:
      'daemon 连续启动失败已熔断。可在设置卡重试,或手动运行 agent-sidecar daemon start 后等待自动领养。',
    daemonDeferTitle: '等待系统服务拉起 daemon',
    daemonDeferHint:
      '检测到 LaunchAgent 托管,插件不会自行启动 daemon;服务拉起后看板会自动出数。',
    filteredTitle: '当前过滤条件下没有会话',
    filteredHint: '试试放宽时间窗,或打开「显示已结束」。',
    noSessionsTitle: '暂无被观测的会话',
    noSessionsHint:
      '本机 agent(claude / codex / cursor / dsh …)开始工作后会自动出现在这里。',
  },
  /** Top bar controls. */
  topbar: {
    title: 'Sidecar 多 agent 看板',
    refresh: '刷新',
    refreshTitle: '手动拉取最新快照',
    showDead: '显示已结束',
    timeWindow: '时间窗',
  },
  /** Session card texts. */
  card: {
    noEvent: '暂无事件',
    untitled: '(无标题)',
    /** Design §5.3 / SKILL.md wording: statuses are inferred observations. */
    observedDisclaimer: '状态为从持久化数据推断的观察值,可能滞后',
    observedValue: '观察值: {status}',
    lastReconcile: '最近对账: {time}',
    neverReconciled: '尚未对账',
  },
  /** Relative time templates. */
  time: {
    justNow: '刚刚',
    minutesAgo: '{n} 分钟前',
    hoursAgo: '{n} 小时前',
    daysAgo: '{n} 天前',
  },
  /** Time-window option templates. */
  timeWindow: {
    hours: '{n} 小时',
    days: '{n} 天',
  },
  /** Group header session count. */
  groupCount: '{n} 个会话',
  /** Sessions with an empty `project` fall into this group (task spec). */
  unknownProject: '未知项目',
  /** Footer widget. */
  widget: {
    label: 'Sidecar',
    connection: {
      ok: '已连接',
      degraded: '连接不稳定',
      off: '离线',
    },
    working: '{n} 个会话工作中',
  },
} as const

export type BoardStrings = typeof BOARD_STRINGS
