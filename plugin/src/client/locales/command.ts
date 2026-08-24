/**
 * `command.*` locale segment for the `/sidecar` slash command (T4.6).
 *
 * WHY A SEPARATE SEGMENT FILE (not new keys in zh.ts/en.ts): the main
 * dictionaries are being extended concurrently by the inject-panel task
 * (`inject.*`), and the shipped locale contract test pins the main table's
 * domain set to `settings|board|inject` — so the command surface keeps its
 * keys in this self-contained segment instead. It reuses the shared
 * translator engine and the module-level active-locale switch from
 * `./index.ts`, so `setLocale` drives both tables consistently. The
 * integration wave may fold these dictionaries into the main table (they
 * are flat `Record<string, string>` — the exact
 * `ctx.locale.register(ns, locale, dict)` currency) without touching this
 * module's consumers.
 *
 * Key-space convention: every key is `command.<leaf>`, compile-enforced by
 * the `satisfies` clause; en is compile-checked complete against the zh
 * key union (same posture as ./en.ts). Copy for daemon/connection/status
 * labels mirrors the board's wording so the quick overview and the board
 * never disagree.
 *
 * @module
 */

import { createTranslator, getLocale } from './index.ts'

export const commandZh = {
  // ── slash-menu row ──────────────────────────────────────────────────────
  'command.description': '查看 Sidecar 状态速览(daemon、连接与会话)',

  // ── daemon supervisor state labels (mirrors board wording) ─────────────
  'command.daemon.probe': '探测中',
  'command.daemon.adopted': '已连接 · 领养',
  'command.daemon.defer': '等待系统服务',
  'command.daemon.reprobe': '重新探测中',
  'command.daemon.hosting': '正在启动',
  'command.daemon.hosted': '已连接 · 托管',
  'command.daemon.backoff': '重启退避中',
  'command.daemon.failed': '离线',
  'command.daemon.unknown': '状态未知',

  // ── connection health (mirrors footer widget wording) ──────────────────
  'command.connection.ok': '已连接',
  'command.connection.degraded': '连接不稳定',
  'command.connection.off': '离线',

  // ── session status labels (mirrors board badge wording) ────────────────
  'command.status.working': '工作中',
  'command.status.waiting': '等待中',
  'command.status.idle': '空闲',
  'command.status.dead': '已结束',
  'command.status.unknown': '未知',

  // ── overview rows ───────────────────────────────────────────────────────
  'command.daemonRow': 'Sidecar · {state}',
  'command.countsRow': '{working} 个工作中 · {waiting} 个等待中',
  'command.countsDetail': '共 {total} 个会话',
  'command.sessionDetail': '{project} · {status} · {time}',
  'command.noSessions': '暂无被观测的会话',
  'command.unknownProject': '未知项目',
  'command.untitled': '(无标题)',
  'command.truncated': '还有 {n} 个活跃会话未列出',
  'command.boardHint': '打开会话视图的「Sidecar」Tab 查看完整看板',

  // ── offline / unreachable guidance ──────────────────────────────────────
  'command.unreachable': 'sidecar 未连接',
  'command.unreachableHint':
    '无法获取状态快照;请确认 agent-sidecar 插件已启用、daemon 可用后重试。',
  'command.offlineFailed': 'sidecar 已离线(daemon 连续启动失败已熔断)',
  'command.offlineFailedHint':
    '以下为最后一次快照。可在设置卡重试,或手动运行 agent-sidecar daemon start 后等待自动领养。',
  'command.offlineDefer': '等待系统服务拉起 daemon',
  'command.offlineDeferHint':
    '检测到 LaunchAgent 托管,插件只探测等待;服务拉起后速览会自动恢复。',

  // ── relative time (mirrors board thresholds) ────────────────────────────
  'command.time.justNow': '刚刚',
  'command.time.minutesAgo': '{n} 分钟前',
  'command.time.hoursAgo': '{n} 小时前',
  'command.time.daysAgo': '{n} 天前',
} satisfies Record<`command.${string}`, string>

/** Key union of the command locale segment (zh is the source of truth). */
export type CommandLocaleKey = keyof typeof commandZh

export const commandEn = {
  // ── slash-menu row ──────────────────────────────────────────────────────
  'command.description': 'Sidecar status at a glance (daemon, connection, sessions)',

  // ── daemon supervisor state labels ──────────────────────────────────────
  'command.daemon.probe': 'Probing',
  'command.daemon.adopted': 'Connected · adopted',
  'command.daemon.defer': 'Waiting for the system service',
  'command.daemon.reprobe': 'Re-probing',
  'command.daemon.hosting': 'Starting',
  'command.daemon.hosted': 'Connected · hosted',
  'command.daemon.backoff': 'Restart backoff',
  'command.daemon.failed': 'Offline',
  'command.daemon.unknown': 'State unknown',

  // ── connection health ────────────────────────────────────────────────────
  'command.connection.ok': 'Connected',
  'command.connection.degraded': 'Connection unstable',
  'command.connection.off': 'Offline',

  // ── session status labels ────────────────────────────────────────────────
  'command.status.working': 'Working',
  'command.status.waiting': 'Waiting',
  'command.status.idle': 'Idle',
  'command.status.dead': 'Finished',
  'command.status.unknown': 'Unknown',

  // ── overview rows ────────────────────────────────────────────────────────
  'command.daemonRow': 'Sidecar · {state}',
  'command.countsRow': '{working} working · {waiting} waiting',
  'command.countsDetail': '{total} sessions total',
  'command.sessionDetail': '{project} · {status} · {time}',
  'command.noSessions': 'No observed sessions yet',
  'command.unknownProject': 'Unknown project',
  'command.untitled': '(untitled)',
  'command.truncated': '{n} more active sessions not listed',
  'command.boardHint': 'Open the "Sidecar" tab in the conversation view for the full board',

  // ── offline / unreachable guidance ───────────────────────────────────────
  'command.unreachable': 'sidecar is not connected',
  'command.unreachableHint':
    'The state snapshot could not be fetched; check that the agent-sidecar plugin is enabled and the daemon is available, then retry.',
  'command.offlineFailed': 'sidecar is offline (daemon start failures tripped the breaker)',
  'command.offlineFailedHint':
    'Showing the last snapshot. Retry from the settings card, or run agent-sidecar daemon start manually and wait for adoption.',
  'command.offlineDefer': 'Waiting for the system service to start the daemon',
  'command.offlineDeferHint':
    'A LaunchAgent manages the daemon; the plugin only probes and waits. The overview recovers automatically once the service brings it up.',

  // ── relative time ────────────────────────────────────────────────────────
  'command.time.justNow': 'just now',
  'command.time.minutesAgo': '{n} min ago',
  'command.time.hoursAgo': '{n} h ago',
  'command.time.daysAgo': '{n} d ago',
} satisfies Record<CommandLocaleKey, string>

/** Shipped dictionaries of this segment, keyed by locale id. */
export const commandDictionaries = { zh: commandZh, en: commandEn } as const

const translate = createTranslator(commandDictionaries)

/**
 * Translate a command-segment key in the shared active locale (the same
 * `setLocale` switch that drives the main table). Lookup chain: active
 * locale → zh → the key itself.
 * @param key - a key of the command segment.
 * @param params - optional `{name}` template params.
 * @returns the translated string.
 */
export function tCommand(key: CommandLocaleKey, params?: Record<string, unknown>): string {
  return translate(getLocale(), key, params)
}
