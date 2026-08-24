/**
 * Simplified-Chinese dictionary — the key-set source of truth for the
 * agent-sidecar client locale table (the en dictionary is checked complete
 * against this key union; see ./en.ts).
 *
 * Key-space convention (compile-enforced by the `satisfies` clause): every
 * key is `<domain>.<leaf>` where the domain is one of
 * {@link SidecarLocaleDomain}. `settings.*` is owned by the settings card
 * (T2.3); `inject.*` by the inject panel (T4.5, src/client/inject/);
 * `board.*` by the board-tab chrome (view switcher); `detail.*` /
 * `dshtools.*` / `project.*` mirror the M3 module tables (see below);
 * `analysis.*` is owned by the analysis panel (T5.10b); `command.*` by the
 * `/sidecar` slash command (T4.6, re-exported via ./command.ts);
 * `sidebar.*` by the optional better-sidebar mini tab (T6.3,
 * src/client/sidebar-tab.tsx) — keeping the prefixes in one flat namespace
 * avoids cross-task collisions.
 *
 * M3 unification (T5.10b): the component-local tables `detail/strings.ts`,
 * `dsh-tools/strings.ts` and `PROJECT_VIEW_STRINGS` stay the rendering
 * source for their components (decoupling kept), and this table REFERENCES
 * them entry-by-entry so `t()` covers every M3 string with zero copy drift
 * (parity is pinned by test/locales.test.ts). en translations for those
 * domains live in ./en.ts (the module tables are zh-only).
 *
 * Copy sources: the schemastery `.description()` strings in src/config.ts
 * (the authoritative zh copy for each field) and design doc §4.a/§5.3/§6/§8.
 */

import { DETAIL_STRINGS } from '../detail/strings.ts'
import { DSH_TOOLS_STRINGS } from '../dsh-tools/strings.ts'
import { PROJECT_VIEW_STRINGS } from '../board/project-view-logic.ts'

/** Locale key domains (one owner surface per domain, see module doc). */
export type SidecarLocaleDomain =
  | 'settings'
  | 'board'
  | 'inject'
  | 'detail'
  | 'dshtools'
  | 'project'
  | 'analysis'
  | 'command'
  | 'sidebar'

const D = DETAIL_STRINGS
const Q = DSH_TOOLS_STRINGS
const P = PROJECT_VIEW_STRINGS

export const zh = {
  // ── card chrome ────────────────────────────────────────────────────────
  'settings.cardTitle': 'Agent Sidecar',
  'settings.cardDescription': '跨 agent 会话监控、注入与旁路分析的运行设置。',
  'settings.docsLink': '查看文档',
  'settings.readOnly': '当前设置文档为只读,修改不可保存。',
  'settings.unsaved': '未保存',
  'settings.save': '保存',
  'settings.saving': '保存中…',
  'settings.discard': '放弃修改',
  'settings.saveFailed': '保存失败,请重试。',
  'settings.expand': '展开',
  'settings.collapse': '收起',
  'settings.invalidNumber': '请输入不小于 {min} 的整数',

  // ── daemon lifecycle ───────────────────────────────────────────────────
  'settings.sectionDaemon': 'daemon 生命周期',
  'settings.daemonPolicyLabel': '托管策略',
  'settings.daemonPolicyHint':
    'adopt-or-host=探测并领养既有 daemon,否则自行拉起;adopt-only=只领养绝不拉起;off=不管理生命周期(仍只读对账既有 daemon 的数据)。',
  'settings.daemonPolicyAdoptOrHost': 'adopt-or-host(领养或拉起)',
  'settings.daemonPolicyAdoptOnly': 'adopt-only(只领养)',
  'settings.daemonPolicyOff': 'off(不管理)',
  'settings.daemonBackoffLimitLabel': '熔断阈值',
  'settings.daemonBackoffLimitHint': '连续托管失败达到该次数后停止重启并进入 failed。',
  'settings.daemonStatusLabel': 'daemon 状态',
  'settings.daemonStateProbe': '探测中',
  'settings.daemonStateAdopted': '已领养既有 daemon',
  'settings.daemonStateDefer': '等待系统服务拉活',
  'settings.daemonStateReprobe': '重新探测中',
  'settings.daemonStateHosting': '正在拉起',
  'settings.daemonStateHosted': '插件托管中',
  'settings.daemonStateBackoff': '退避重试中',
  'settings.daemonStateFailed': '已熔断',
  'settings.daemonPidVersion': 'pid {pid} · v{version}',
  'settings.daemonDeferNote':
    'daemon 由系统服务(LaunchAgent)托管;插件只探测等待,不重复拉起,也不会终止它。',
  'settings.daemonFailedNote':
    '连续托管失败已达熔断阈值;看板降级为最后快照。排查 sidecar 命令后可点击重试。',
  'settings.daemonRetry': '重试',

  // ── sidecar invocation ─────────────────────────────────────────────────
  'settings.sectionSidecar': 'sidecar 调用',
  'settings.sidecarCommandLabel': '可执行命令',
  'settings.sidecarCommandHint':
    'PATH 名、绝对路径或空格分隔的多段命令(如 python3 /path/agent-sidecar.pyz);插件绝不代装 sidecar。',
  'settings.sidecarRuntimeDirLabel': '运行时目录',
  'settings.sidecarRuntimeDirHint':
    '留空使用默认 ~/.agent_sidecar(尊重 AGENT_SIDECAR_RUNTIME_DIR 环境变量);非空时经环境变量传给受托管的 daemon。',

  // ── stream reconciliation ──────────────────────────────────────────────
  'settings.sectionStream': '数据流对账',
  'settings.streamActiveMsLabel': '活跃对账周期(毫秒)',
  'settings.streamActiveMsHint': '有会话工作中时的 status 快照周期。',
  'settings.streamIdleMsLabel': '空闲对账周期(毫秒)',
  'settings.streamIdleMsHint': '无会话工作时的 status 快照周期。',

  // ── injection ──────────────────────────────────────────────────────────
  'settings.sectionInject': '消息注入',
  'settings.injectEnabledLabel': '启用注入',
  'settings.injectEnabledHint':
    '关闭时看板隐藏全部注入入口,写接口在服务端同步拒绝。',
  'settings.injectDefaultModeLabel': '默认注入模式',
  'settings.injectDefaultModeHint': '注入面板打开时预选的模式。',
  'settings.injectModeQueue': 'queue(排队下一轮)',
  'settings.injectModeSteer': 'steer(中途注入)',
  'settings.injectSafetyNote':
    '安全须知:注入默认关闭;开启后每次注入仍须经确认对话框逐次放行,无任何批量或定时注入。多用户主机不建议开启。',

  // ── bypass analysis ────────────────────────────────────────────────────
  'settings.sectionAnalysis': '旁路分析',
  'settings.analysisEnabledLabel': '启用 AI 旁路分析',
  'settings.analysisEnabledHint':
    '按需拉起 dsh 分析会话解读被观测会话(消耗模型 token,默认关闭)。',

  // ── board UI ───────────────────────────────────────────────────────────
  'settings.sectionUi': '看板界面',
  'settings.uiTimeWindowHoursLabel': '会话时间窗(小时)',
  'settings.uiTimeWindowHoursHint': '看板只显示该时间窗内活动过的会话。',
  'settings.uiShowDeadLabel': '显示 dead 会话',
  'settings.uiShowDeadHint': '把已结束(dead)的会话也列入看板。',

  // ── skill mode ─────────────────────────────────────────────────────────
  'settings.sectionSkill': 'skill 模式',
  'settings.skillProvideLabel': '内嵌提供 skill',
  'settings.skillProvideHint':
    '经 registerProvider 向 dsh 提供 agent-sidecar skill(M4 启用;重启后生效)。',

  // ── inject panel: chrome & editor (T4.5, design §5.1 view 3) ───────────
  'inject.title': '注入消息',
  'inject.confirmTitle': '确认注入',
  'inject.close': '关闭',
  'inject.done': '完成',
  'inject.capabilityOff': '注入功能未开启;请在设置中开启注入。',
  'inject.noTarget': '未选择注入目标;请从看板卡片或会话详情发起注入。',
  'inject.targetLabel': '注入目标',
  'inject.messageLabel': '消息内容',
  'inject.messagePlaceholder': '输入要注入的消息(上限 16 KiB;请勿粘贴密钥等敏感内容)',
  'inject.byteCount': '{bytes} / {limit} 字节',
  'inject.msgEmpty': '消息不能为空。',
  'inject.msgNul': '消息包含非法 NUL 字符。',
  'inject.msgTooLarge': '消息 {bytes} 字节,超出 {limit} 字节上限。',
  'inject.modeLabel': '注入模式',
  'inject.modeQueue': 'queue(排队下一轮)',
  'inject.modeQueueHint': '消息排队等待,目标会话下一轮开始时处理。',
  'inject.modeSteer': 'steer(中途注入)',
  'inject.modeSteerHint': '消息在目标会话当前轮次中途注入,立即介入其工作。',
  'inject.argvWarning':
    '目标为 cursor-cli:注入经其原生子进程执行,消息在该进程存续期间对本机进程列表可见;请勿包含密钥等敏感内容。',
  'inject.auditNote':
    '本次注入会被记入 sidecar 审计日志(含字节数与内容指纹,不含消息明文)。',
  'inject.prepare': '准备注入',
  'inject.preparing': '校验中…',

  // ── inject panel: confirm phase ──────────────────────────────────────
  'inject.planTargetLabel': '目标现状',
  'inject.planStatus': '当前状态:{status}',
  'inject.statusObservedNote': '状态为从持久化数据推断的观察值,可能滞后。',
  'inject.planModeLabel': '模式',
  'inject.planPreviewLabel': '消息摘要({bytes} 字节)',
  'inject.countdown': '确认令牌 {seconds} 秒后过期',
  'inject.confirmExecute': '确认注入',
  'inject.executing': '注入中…',
  'inject.cancel': '取消',
  'inject.tokenExpired': '确认已超时,令牌失效;请重新准备注入。',

  // ── inject panel: result phase ───────────────────────────────────────
  'inject.resultDelivered': '已投递:消息已注入目标会话。',
  'inject.resultFailed': '注入失败。',
  'inject.resultUnknown':
    '结果未知:消息可能已投递。请勿重试;请前往目标会话核对后再决定下一步。',
  'inject.resultReplayed': '幂等重放:返回的是此前同一请求的结果,未发生二次注入。',
  'inject.reprepare': '重新准备',

  // ── inject panel: error vocabulary (gateway + transport) ─────────────
  'inject.errInjectDisabled': '注入功能已在服务端关闭;请在设置中开启注入。',
  'inject.errInvalidMessage': '消息未通过服务端校验。',
  'inject.errTargetNotFound': '目标会话不存在或已离开观测范围。',
  'inject.errTargetDead': '目标会话已结束(dead),无法注入。',
  'inject.errTooManyPending': '待确认的注入请求过多,请稍后再试。',
  'inject.errTokenMissing': '确认令牌缺失或未被签发;请重新准备。',
  'inject.errTokenExpired': '确认令牌已过期;请重新准备。',
  'inject.errTokenReused': '确认令牌已被消费;请重新准备。',
  'inject.errTokenMismatch': '确认内容与准备时不一致;请重新准备。',
  'inject.errUnsupportedAgent': '该 agent 没有可用的注入通道。',
  'inject.errExecutorError': '注入通路执行出错。',
  'inject.errTimeout': '请求超时,未收到服务端回执。',
  'inject.errAborted': '请求已取消。',
  'inject.errNetwork': '网络错误,请求未能送达。',
  'inject.errParse': '服务端响应无法解析。',
  'inject.errGeneric': '请求失败({code})。',

  // ── board tab chrome: main-view switcher (T5.10b, design §5.1) ─────────
  'board.viewBoard': '会话看板',
  'board.viewProjects': '项目视图',

  // ── session detail (T5.3 detail/strings.ts, referenced verbatim) ───────
  'detail.header.close': D.header.close,
  'detail.header.listenOn': D.header.listenOn,
  'detail.header.listenOff': D.header.listenOff,
  'detail.header.listenHint': D.header.listenHint,
  'detail.header.untitled': D.header.untitled,
  'detail.header.unknownProject': D.header.unknownProject,
  'detail.header.observedDisclaimer': D.header.observedDisclaimer,
  'detail.status.working': D.status.working,
  'detail.status.waiting': D.status.waiting,
  'detail.status.idle': D.status.idle,
  'detail.status.dead': D.status.dead,
  'detail.status.unknown': D.status.unknown,
  'detail.sources.title': D.sources.title,
  'detail.sources.dshLive': D.sources.dshLive,
  'detail.sources.dshCold': D.sources.dshCold,
  'detail.sources.sidecarReplay': D.sources.sidecarReplay,
  'detail.sources.sidecarBuffer': D.sources.sidecarBuffer,
  'detail.sources.none': D.sources.none,
  'detail.kind.user': D.kind.user,
  'detail.kind.assistant': D.kind.assistant,
  'detail.kind.thinking': D.kind.thinking,
  'detail.kind.toolCall': D.kind.toolCall,
  'detail.kind.toolResult': D.kind.toolResult,
  'detail.kind.turn': D.kind.turn,
  'detail.kind.step': D.kind.step,
  'detail.kind.error': D.kind.error,
  'detail.kind.other': D.kind.other,
  'detail.gap.label': D.gap.label,
  'detail.timeline.loadMore': D.timeline.loadMore,
  'detail.timeline.loadingMore': D.timeline.loadingMore,
  'detail.timeline.noMore': D.timeline.noMore,
  'detail.timeline.expand': D.timeline.expand,
  'detail.timeline.collapse': D.timeline.collapse,
  'detail.timeline.newBadge': D.timeline.newBadge,
  'detail.timeline.seq': D.timeline.seq,
  'detail.timeline.hiddenNotice': D.timeline.hiddenNotice,
  'detail.timeline.showAll': D.timeline.showAll,
  'detail.states.loadingTitle': D.states.loadingTitle,
  'detail.states.emptyTitle': D.states.emptyTitle,
  'detail.states.emptyHint': D.states.emptyHint,
  'detail.states.errorTitle': D.states.errorTitle,
  'detail.states.errorFallback': D.states.errorFallback,
  'detail.states.errors.session_not_found': D.states.errors.session_not_found,
  'detail.states.errors.invalid_cursor': D.states.errors.invalid_cursor,
  'detail.states.errors.fusion_not_wired': D.states.errors.fusion_not_wired,
  'detail.states.errors.network_error': D.states.errors.network_error,
  'detail.states.errors.request_timeout': D.states.errors.request_timeout,
  'detail.time.justNow': D.time.justNow,
  'detail.time.minutesAgo': D.time.minutesAgo,
  'detail.time.hoursAgo': D.time.hoursAgo,
  'detail.time.daysAgo': D.time.daysAgo,

  // ── session detail: integration chrome (T5.10b literals) ───────────────
  'detail.actions.inject': '注入',
  'detail.actions.analyze': 'AI 分析',
  'detail.actions.analyzeDisabledHint': '在设置中开启「启用 AI 旁路分析」后可用',

  // ── dsh deep-query tools (T5.4 dsh-tools/strings.ts, referenced) ───────
  'dshtools.lineage.title': Q.lineage.title,
  'dshtools.lineage.loading': Q.lineage.loading,
  'dshtools.lineage.error': Q.lineage.error,
  'dshtools.lineage.empty': Q.lineage.empty,
  'dshtools.lineage.currentBadge': Q.lineage.currentBadge,
  'dshtools.lineage.liveBadge': Q.lineage.liveBadge,
  'dshtools.lineage.notPersistedBadge': Q.lineage.notPersistedBadge,
  'dshtools.lineage.role.ancestor': Q.lineage.role.ancestor,
  'dshtools.lineage.role.target': Q.lineage.role.target,
  'dshtools.lineage.role.descendant': Q.lineage.role.descendant,
  'dshtools.lineage.jumpTitle': Q.lineage.jumpTitle,
  'dshtools.lineage.currentTitle': Q.lineage.currentTitle,
  'dshtools.lineage.expand': Q.lineage.expand,
  'dshtools.lineage.collapse': Q.lineage.collapse,
  'dshtools.lineage.nodeCount': Q.lineage.nodeCount,
  'dshtools.lineage.incompleteWithId': Q.lineage.incompleteWithId,
  'dshtools.lineage.incomplete': Q.lineage.incomplete,
  'dshtools.lineage.degrade.notDshTitle': Q.lineage.degrade.notDshTitle,
  'dshtools.lineage.degrade.notDshBody': Q.lineage.degrade.notDshBody,
  'dshtools.lineage.degrade.queryUnavailableTitle': Q.lineage.degrade.queryUnavailableTitle,
  'dshtools.lineage.degrade.queryUnavailableBody': Q.lineage.degrade.queryUnavailableBody,
  'dshtools.lineage.degrade.traceFailedTitle': Q.lineage.degrade.traceFailedTitle,
  'dshtools.lineage.degrade.traceFailedBody': Q.lineage.degrade.traceFailedBody,
  'dshtools.lineage.degrade.unknownTitle': Q.lineage.degrade.unknownTitle,
  'dshtools.lineage.degrade.unknownBody': Q.lineage.degrade.unknownBody,
  'dshtools.search.title': Q.search.title,
  'dshtools.search.placeholder': Q.search.placeholder,
  'dshtools.search.submit': Q.search.submit,
  'dshtools.search.loading': Q.search.loading,
  'dshtools.search.error': Q.search.error,
  'dshtools.search.empty': Q.search.empty,
  'dshtools.search.filterOnlyNotice': Q.search.filterOnlyNotice,
  'dshtools.search.projectFilter': Q.search.projectFilter,
  'dshtools.search.matchedBy.full-text': Q.search.matchedBy['full-text'],
  'dshtools.search.matchedBy.title': Q.search.matchedBy.title,
  'dshtools.search.matchedBy.project': Q.search.matchedBy.project,
  'dshtools.search.matchedBy.other': Q.search.matchedBy.other,
  'dshtools.search.untitled': Q.search.untitled,

  // ── project correlation view (T5.5 PROJECT_VIEW_STRINGS, referenced) ───
  'project.title': P.title,
  'project.summary': P.summary,
  'project.crossAgent': P.crossAgent,
  'project.sessionCount': P.sessionCount,
  'project.lastActive': P.lastActive,
  'project.liveChip': P.liveChip,
  'project.untitled': P.untitled,
  'project.empty.title': P.empty.title,
  'project.empty.hint': P.empty.hint,
  'project.loading': P.loading,
  'project.errorTitle': P.errorTitle,

  // ── AI bypass analysis panel (T5.10b, design §4.e.3 / §5.1) ────────────
  'analysis.title': 'AI 分析',
  'analysis.close': '关闭',
  'analysis.disabledNote': 'AI 旁路分析未开启;请在设置中开启「启用 AI 旁路分析」。',
  'analysis.idleHint':
    '按需拉起一次 dsh 旁路分析,解读该会话的当前状态与走向(消耗模型 token)。',
  'analysis.start': '开始分析',
  'analysis.requesting': '分析中…(最长约 60 秒)',
  'analysis.exchangeInitial': '分析摘要',
  'analysis.followupLabel': '追问',
  'analysis.truncatedNotice': '输入超出预算已截断,分析基于部分上下文。',
  'analysis.emptySummary': '(分析会话未返回摘要)',
  'analysis.disclaimerFallback': 'AI 分析仅供参考,结论以实际会话为准。',
  'analysis.followupPlaceholder': '继续追问这次分析…',
  'analysis.followupSubmit': '追问',
  'analysis.answering': '回答中…',
  'analysis.stop': '停止分析',
  'analysis.stopped': '分析已停止,分析会话已释放。',
  'analysis.restart': '重新分析',
  'analysis.noticeTimeout': '本次追问超时,可稍后重试;分析会话仍保留。',
  'analysis.noticeNetwork': '请求未能送达,可重试;分析会话仍保留。',
  'analysis.noticeCancelFailed': '停止请求未送达,请重试。',
  'analysis.errDisabled': 'AI 分析已在服务端关闭;请在设置中开启后重试。',
  'analysis.errUnavailable': '当前 host 未接入 AI 分析能力(agents 服务不可用)。',
  'analysis.errTargetNotFound': '分析目标不存在或已离开观测范围。',
  'analysis.errTooManyActive': '并发分析会话已达上限,请稍后再试。',
  'analysis.errTimeout': '分析超时,分析会话已释放;可重新发起。',
  'analysis.errCreateFailed': '分析会话创建失败。',
  'analysis.errCancelled': '分析已取消。',
  'analysis.errNetwork': '网络错误,分析请求未能完成。',
  'analysis.errGeneric': '分析失败({code})。',

  // ── /sidecar slash command (T4.6, folded from locales/command.ts) ──────
  'command.description': '查看 Sidecar 状态速览(daemon、连接与会话)',
  'command.daemon.probe': '探测中',
  'command.daemon.adopted': '已连接 · 领养',
  'command.daemon.defer': '等待系统服务',
  'command.daemon.reprobe': '重新探测中',
  'command.daemon.hosting': '正在启动',
  'command.daemon.hosted': '已连接 · 托管',
  'command.daemon.backoff': '重启退避中',
  'command.daemon.failed': '离线',
  'command.daemon.unknown': '状态未知',
  'command.connection.ok': '已连接',
  'command.connection.degraded': '连接不稳定',
  'command.connection.off': '离线',
  'command.status.working': '工作中',
  'command.status.waiting': '等待中',
  'command.status.idle': '空闲',
  'command.status.dead': '已结束',
  'command.status.unknown': '未知',
  'command.daemonRow': 'Sidecar · {state}',
  'command.countsRow': '{working} 个工作中 · {waiting} 个等待中',
  'command.countsDetail': '共 {total} 个会话',
  'command.sessionDetail': '{project} · {status} · {time}',
  'command.noSessions': '暂无被观测的会话',
  'command.unknownProject': '未知项目',
  'command.untitled': '(无标题)',
  'command.truncated': '还有 {n} 个活跃会话未列出',
  'command.boardHint': '打开会话视图的「Sidecar」Tab 查看完整看板',
  'command.unreachable': 'sidecar 未连接',
  'command.unreachableHint':
    '无法获取状态快照;请确认 agent-sidecar 插件已启用、daemon 可用后重试。',
  'command.offlineFailed': 'sidecar 已离线(daemon 连续启动失败已熔断)',
  'command.offlineFailedHint':
    '以下为最后一次快照。可在设置卡重试,或手动运行 agent-sidecar daemon start 后等待自动领养。',
  'command.offlineDefer': '等待系统服务拉起 daemon',
  'command.offlineDeferHint':
    '检测到 LaunchAgent 托管,插件只探测等待;服务拉起后速览会自动恢复。',
  'command.time.justNow': '刚刚',
  'command.time.minutesAgo': '{n} 分钟前',
  'command.time.hoursAgo': '{n} 小时前',
  'command.time.daysAgo': '{n} 天前',

  // ── better-sidebar mini tab (T6.3, design §5.2 optional soft dep) ──────
  'sidebar.tabTitle': 'Sidecar',
  'sidebar.countsRow': '{working} 工作中 · {waiting} 等待中',
  'sidebar.recentTitle': '最近活跃',
  'sidebar.connecting': '等待 sidecar 快照…',
  'sidebar.noSessions': '暂无活跃会话',
  'sidebar.noEvent': '暂无事件记录',
  'sidebar.untitled': '(无标题)',
  'sidebar.boardHint': '完整看板见会话视图的「Sidecar」Tab',
} satisfies Record<`${SidecarLocaleDomain}.${string}`, string>

/** Key union of the locale table (zh is the source of truth). */
export type SidecarLocaleKey = keyof typeof zh
