/**
 * Simplified-Chinese dictionary — the key-set source of truth for the
 * agent-sidecar client locale table (the en dictionary is checked complete
 * against this key union; see ./en.ts).
 *
 * Key-space convention (compile-enforced by the `satisfies` clause): every
 * key is `<domain>.<leaf>` where the domain is one of
 * {@link SidecarLocaleDomain}. `settings.*` is owned by the settings card
 * (T2.3); `inject.*` is owned by the inject panel (T4.5,
 * src/client/inject/); `board.*` stays RESERVED for the board surface —
 * keeping the prefixes in one flat namespace avoids cross-task collisions.
 *
 * Copy sources: the schemastery `.description()` strings in src/config.ts
 * (the authoritative zh copy for each field) and design doc §4.a/§5.3/§6/§8.
 */

/** Locale key domains: settings (card), inject (panel), board (reserved). */
export type SidecarLocaleDomain = 'settings' | 'board' | 'inject'

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
} satisfies Record<`${SidecarLocaleDomain}.${string}`, string>

/** Key union of the locale table (zh is the source of truth). */
export type SidecarLocaleKey = keyof typeof zh
