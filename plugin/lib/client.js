window.__ModuleLoader__.load({
	id: "@shendeguize/dsh-agent-sidecar",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")] = {};
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")] = /* @__PURE__ */ new Map();
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		let react = require("react");
		let _deepseek_ai_dsh_client_ui_primitives = require("@deepseek-ai/dsh-client-ui-primitives");
		let react_jsx_runtime = require("react/jsx-runtime");
		//#region src/client/api.ts
		/**
		* Browser-half data layer: same-origin fetch wrappers for the plugin's
		* self-registered route namespace (design §5.3; server: src/routes.ts).
		*
		* Wire types below are hand-mirrored from the host half (routes.ts /
		* session-store.ts / supervisor.ts / bridge.ts response shapes): the
		* client TS program cannot import host modules because they live in the
		* node-typed host program. The API_PREFIX mirror is pinned against the
		* routes.ts constant by test/client-data.test.ts.
		*
		* Calls are same-origin relative paths with no auth headers (ADR-8 trust
		* posture). Browser primitives — fetch, AbortController, timers — are
		* injectable so everything runs under plain node in tests; defaults
		* resolve from globalThis at call time. Pure data layer: no React, no
		* slots SDK; the UI half imports its types from here.
		*
		* @module
		*/
		/** Route namespace; must mirror routes.ts `API_PREFIX` (pinned by test). */
		const API_PREFIX = "/plugins/agent-sidecar/api";
		/**
		* Single normalized failure shape for every request path, so the UI has
		* one catch surface. `reason` carries the server `{reason}` envelope for
		* kind 'http' and a stable local code otherwise.
		*/
		var ApiError = class extends Error {
			kind;
			reason;
			/** HTTP status when a response was received, else null. */
			status;
			constructor(kind, reason, status = null, cause) {
				super(status === null ? `api ${kind}: ${reason}` : `api ${kind}: ${reason} (http ${status})`, cause === void 0 ? void 0 : { cause });
				this.name = "ApiError";
				this.kind = kind;
				this.reason = reason;
				this.status = status;
			}
		};
		function isApiError(value) {
			return value instanceof ApiError;
		}
		const defaultSetTimeout$3 = (fn, ms) => globalThis.setTimeout(fn, ms);
		const defaultClearTimeout$3 = (handle) => {
			globalThis.clearTimeout(handle);
		};
		const defaultCreateAbortController$3 = () => new AbortController();
		function resolveFetch$2(opts) {
			if (opts.fetch !== void 0) return opts.fetch;
			return globalThis.fetch;
		}
		async function request(path, init, opts) {
			const doFetch = resolveFetch$2(opts);
			const controller = (opts.createAbortController ?? defaultCreateAbortController$3)();
			const setT = opts.setTimeout ?? defaultSetTimeout$3;
			const clearT = opts.clearTimeout ?? defaultClearTimeout$3;
			const timeoutMs = opts.timeoutMs ?? 15e3;
			let timedOut = false;
			let externallyAborted = false;
			const timer = setT(() => {
				timedOut = true;
				controller.abort();
			}, timeoutMs);
			const external = opts.signal;
			const onExternalAbort = () => {
				externallyAborted = true;
				controller.abort();
			};
			if (external !== void 0) if (external.aborted) onExternalAbort();
			else external.addEventListener("abort", onExternalAbort);
			try {
				let res;
				try {
					res = await doFetch(path, {
						...init,
						signal: controller.signal
					});
				} catch (err) {
					if (timedOut) throw new ApiError("timeout", "request_timeout", null, err);
					if (externallyAborted) throw new ApiError("aborted", "request_aborted", null, err);
					throw new ApiError("network", "network_error", null, err);
				}
				if (!res.ok) {
					let reason = `http_${res.status}`;
					try {
						const body = await res.json();
						if (typeof body === "object" && body !== null) {
							const record = body;
							const value = record["reason"];
							const code = record["errorCode"];
							if (typeof value === "string" && value !== "") reason = value;
							else if (typeof code === "string" && code !== "") reason = code;
						}
					} catch {}
					throw new ApiError("http", reason, res.status);
				}
				try {
					return await res.json();
				} catch (err) {
					if (timedOut) throw new ApiError("timeout", "request_timeout", null, err);
					throw new ApiError("parse", "invalid_json", res.status, err);
				}
			} finally {
				clearT(timer);
				if (external !== void 0) external.removeEventListener("abort", onExternalAbort);
			}
		}
		/** `GET <prefix>/state` — full board snapshot. */
		async function fetchState(opts = {}) {
			return await request(`${API_PREFIX}/state`, { method: "GET" }, opts);
		}
		/**
		* `POST <prefix>/action` — transport layer only: the envelope is passed
		* through verbatim, failures are normalized, and there is deliberately NO
		* retry here — requestId idempotency and the `delivery:unknown` no-retry
		* rule (S6) are gateway/UI policy, not transport policy.
		*/
		async function postAction(body, opts = {}) {
			return request(`${API_PREFIX}/action`, {
				method: "POST",
				headers: { "content-type": "application/json" },
				body: JSON.stringify(body)
			}, opts);
		}
		//#endregion
		//#region src/client/locales/zh.ts
		const zh = {
			"settings.cardTitle": "Agent Sidecar",
			"settings.cardDescription": "跨 agent 会话监控、注入与旁路分析的运行设置。",
			"settings.docsLink": "查看文档",
			"settings.readOnly": "当前设置文档为只读,修改不可保存。",
			"settings.unsaved": "未保存",
			"settings.save": "保存",
			"settings.saving": "保存中…",
			"settings.discard": "放弃修改",
			"settings.saveFailed": "保存失败,请重试。",
			"settings.expand": "展开",
			"settings.collapse": "收起",
			"settings.invalidNumber": "请输入不小于 {min} 的整数",
			"settings.sectionDaemon": "daemon 生命周期",
			"settings.daemonPolicyLabel": "托管策略",
			"settings.daemonPolicyHint": "adopt-or-host=探测并领养既有 daemon,否则自行拉起;adopt-only=只领养绝不拉起;off=不管理生命周期(仍只读对账既有 daemon 的数据)。",
			"settings.daemonPolicyAdoptOrHost": "adopt-or-host(领养或拉起)",
			"settings.daemonPolicyAdoptOnly": "adopt-only(只领养)",
			"settings.daemonPolicyOff": "off(不管理)",
			"settings.daemonBackoffLimitLabel": "熔断阈值",
			"settings.daemonBackoffLimitHint": "连续托管失败达到该次数后停止重启并进入 failed。",
			"settings.daemonStatusLabel": "daemon 状态",
			"settings.daemonStateProbe": "探测中",
			"settings.daemonStateAdopted": "已领养既有 daemon",
			"settings.daemonStateDefer": "等待系统服务拉活",
			"settings.daemonStateReprobe": "重新探测中",
			"settings.daemonStateHosting": "正在拉起",
			"settings.daemonStateHosted": "插件托管中",
			"settings.daemonStateBackoff": "退避重试中",
			"settings.daemonStateFailed": "已熔断",
			"settings.daemonPidVersion": "pid {pid} · v{version}",
			"settings.daemonDeferNote": "daemon 由系统服务(LaunchAgent)托管;插件只探测等待,不重复拉起,也不会终止它。",
			"settings.daemonFailedNote": "连续托管失败已达熔断阈值;看板降级为最后快照。排查 sidecar 命令后可点击重试。",
			"settings.daemonRetry": "重试",
			"settings.sectionSidecar": "sidecar 调用",
			"settings.sidecarCommandLabel": "可执行命令",
			"settings.sidecarCommandHint": "PATH 名、绝对路径或空格分隔的多段命令(如 python3 /path/agent-sidecar.pyz);插件绝不代装 sidecar。",
			"settings.sidecarRuntimeDirLabel": "运行时目录",
			"settings.sidecarRuntimeDirHint": "留空使用默认 ~/.agent_sidecar(尊重 AGENT_SIDECAR_RUNTIME_DIR 环境变量);非空时经环境变量传给受托管的 daemon。",
			"settings.sectionStream": "数据流对账",
			"settings.streamActiveMsLabel": "活跃对账周期(毫秒)",
			"settings.streamActiveMsHint": "有会话工作中时的 status 快照周期。",
			"settings.streamIdleMsLabel": "空闲对账周期(毫秒)",
			"settings.streamIdleMsHint": "无会话工作时的 status 快照周期。",
			"settings.sectionInject": "消息注入",
			"settings.injectEnabledLabel": "启用注入",
			"settings.injectEnabledHint": "关闭后注入面板为只读禁用态,写接口在服务端同步拒绝。",
			"settings.injectDefaultModeLabel": "默认注入模式",
			"settings.injectDefaultModeHint": "注入面板打开时预选的模式。",
			"settings.injectModeQueue": "queue(排队下一轮)",
			"settings.injectModeSteer": "steer(中途注入)",
			"settings.injectSafetyNote": "安全须知:注入默认关闭;开启后每次注入仍须经确认对话框逐次放行,无任何批量或定时注入。多用户主机不建议开启。",
			"settings.sectionAnalysis": "旁路分析",
			"settings.analysisEnabledLabel": "启用 AI 旁路分析",
			"settings.analysisEnabledHint": "按需拉起 dsh 分析会话解读被观测会话(消耗模型 token,默认关闭)。",
			"settings.sectionUi": "看板界面",
			"settings.uiTimeWindowHoursLabel": "会话时间窗(小时)",
			"settings.uiTimeWindowHoursHint": "看板只显示该时间窗内活动过的会话。",
			"settings.uiShowDeadLabel": "显示 dead 会话",
			"settings.uiShowDeadHint": "把已结束(dead)的会话也列入看板。",
			"settings.sectionSkill": "skill 模式",
			"settings.skillProvideLabel": "内嵌提供 skill",
			"settings.skillProvideHint": "经 registerProvider 向 dsh 提供 agent-sidecar skill(M4 启用;重启后生效)。",
			"inject.title": "注入消息",
			"inject.confirmTitle": "确认注入",
			"inject.close": "关闭",
			"inject.done": "完成",
			"inject.capabilityOff": "注入功能未开启;请在设置中开启注入。",
			"inject.noTarget": "未选择注入目标;请从看板卡片或会话详情发起注入。",
			"inject.targetLabel": "注入目标",
			"inject.messageLabel": "消息内容",
			"inject.messagePlaceholder": "输入要注入的消息(上限 16 KiB;请勿粘贴密钥等敏感内容)",
			"inject.byteCount": "{bytes} / {limit} 字节",
			"inject.msgEmpty": "消息不能为空。",
			"inject.msgNul": "消息包含非法 NUL 字符。",
			"inject.msgTooLarge": "消息 {bytes} 字节,超出 {limit} 字节上限。",
			"inject.modeLabel": "注入模式",
			"inject.modeQueue": "queue(排队下一轮)",
			"inject.modeQueueHint": "消息排队等待,目标会话下一轮开始时处理。",
			"inject.modeSteer": "steer(中途注入)",
			"inject.modeSteerHint": "消息在目标会话当前轮次中途注入,立即介入其工作。",
			"inject.argvWarning": "目标为 cursor-cli:注入经其原生子进程执行,消息在该进程存续期间对本机进程列表可见;请勿包含密钥等敏感内容。",
			"inject.auditNote": "本次注入会被记入 sidecar 审计日志(含字节数与内容指纹,不含消息明文)。",
			"inject.prepare": "准备注入",
			"inject.preparing": "校验中…",
			"inject.planTargetLabel": "目标现状",
			"inject.planStatus": "当前状态:{status}",
			"inject.statusObservedNote": "状态为从持久化数据推断的观察值,可能滞后。",
			"inject.planModeLabel": "模式",
			"inject.planPreviewLabel": "消息摘要({bytes} 字节)",
			"inject.countdown": "确认令牌 {seconds} 秒后过期",
			"inject.confirmExecute": "确认注入",
			"inject.executing": "注入中…",
			"inject.cancel": "取消",
			"inject.tokenExpired": "确认已超时,令牌失效;请重新准备注入。",
			"inject.resultDelivered": "已投递:消息已注入目标会话。",
			"inject.resultFailed": "注入失败。",
			"inject.resultUnknown": "结果未知:消息可能已投递。请勿重试;请前往目标会话核对后再决定下一步。",
			"inject.resultReplayed": "幂等重放:返回的是此前同一请求的结果,未发生二次注入。",
			"inject.reprepare": "重新准备",
			"inject.observeListen": "开启监听观察反应",
			"inject.errInjectDisabled": "注入功能已在服务端关闭;请在设置中开启注入。",
			"inject.errInvalidMessage": "消息未通过服务端校验。",
			"inject.errTargetNotFound": "目标会话不存在或已离开观测范围。",
			"inject.errTargetDead": "目标会话已结束(dead),无法注入。",
			"inject.errTooManyPending": "待确认的注入请求过多,请稍后再试。",
			"inject.errTokenMissing": "确认令牌缺失或未被签发;请重新准备。",
			"inject.errTokenExpired": "确认令牌已过期;请重新准备。",
			"inject.errTokenReused": "确认令牌已被消费;请重新准备。",
			"inject.errTokenMismatch": "确认内容与准备时不一致;请重新准备。",
			"inject.errUnsupportedAgent": "该 agent 没有可用的注入通道。",
			"inject.errExecutorError": "注入通路执行出错。",
			"inject.errTimeout": "请求超时,未收到服务端回执。",
			"inject.errAborted": "请求已取消。",
			"inject.errNetwork": "网络错误,请求未能送达。",
			"inject.errParse": "服务端响应无法解析。",
			"inject.errGeneric": "请求失败({code})。",
			"board.viewBoard": "会话看板",
			"board.viewProjects": "项目视图",
			"board.status.working": "工作中",
			"board.status.waiting": "等待中",
			"board.status.idle": "空闲",
			"board.status.dead": "已结束",
			"board.status.unknown": "未知",
			"board.attention.gap": "事件缺口",
			"board.daemon.probe": "探测中",
			"board.daemon.adopted": "已连接 · 领养",
			"board.daemon.defer": "等待系统服务",
			"board.daemon.reprobe": "重新探测中",
			"board.daemon.hosting": "正在启动",
			"board.daemon.hosted": "已连接 · 托管",
			"board.daemon.backoff": "重启退避中",
			"board.daemon.failed": "离线",
			"board.stream.ok": "实时流正常",
			"board.stream.degraded": "实时流重连中",
			"board.stream.unknown": "实时流未建立",
			"board.banner.daemonFailed": "sidecar 离线:看板显示最后一次快照,数据不再更新",
			"board.banner.streamDegraded": "实时流重连中,数据可能滞后",
			"board.empty.daemonFailedTitle": "sidecar 已离线",
			"board.empty.daemonFailedHint": "daemon 连续启动失败已熔断。可在设置卡重试,或手动运行 agent-sidecar daemon start 后等待自动领养。",
			"board.empty.daemonDeferTitle": "等待系统服务拉起 daemon",
			"board.empty.daemonDeferHint": "检测到 LaunchAgent 托管,插件不会自行启动 daemon;服务拉起后看板会自动出数。",
			"board.empty.filteredTitle": "当前过滤条件下没有会话",
			"board.empty.filteredHint": "试试放宽时间窗、取消状态过滤,或打开「显示已结束」。",
			"board.empty.noSessionsTitle": "暂无被观测的会话",
			"board.empty.noSessionsHint": "本机 agent(claude / codex / cursor / dsh …)开始工作后会自动出现在这里。",
			"board.topbar.title": "Sidecar 多 agent 看板",
			"board.topbar.refresh": "刷新",
			"board.topbar.refreshing": "刷新中…",
			"board.topbar.refreshTitle": "手动拉取最新快照",
			"board.topbar.refreshFailed": "手动刷新失败,看板仍显示原有快照",
			"board.topbar.dismiss": "知道了",
			"board.topbar.showDead": "显示已结束",
			"board.topbar.timeWindow": "时间窗",
			"board.topbar.countWorking": "{n} 工作中",
			"board.topbar.countWaiting": "{n} 等待中",
			"board.topbar.countTotal": "共 {n} 个会话",
			"board.topbar.filterByStatusTitle": "只看「{label}」的会话",
			"board.topbar.clearStatusFilterTitle": "取消状态过滤,显示全部会话",
			"board.group.collapseTitle": "收起该分组",
			"board.group.expandTitle": "展开该分组",
			"board.group.showAll": "展开全部 {n} 个会话",
			"board.group.showLess": "只看前 {n} 个",
			"board.card.noEvent": "暂无事件",
			"board.card.untitled": "(无标题)",
			"board.card.observedDisclaimer": "状态为从持久化数据推断的观察值,可能滞后",
			"board.card.observedValue": "观察值: {status}",
			"board.card.lastReconcile": "最近对账: {time}",
			"board.card.neverReconciled": "尚未对账",
			"board.card.copyId": "点击复制完整会话 id",
			"board.card.copied": "已复制",
			"board.time.justNow": "刚刚",
			"board.time.minutesAgo": "{n} 分钟前",
			"board.time.hoursAgo": "{n} 小时前",
			"board.timeWindow.hours": "{n} 小时",
			"board.timeWindow.days": "{n} 天",
			"board.groupCount": "{n} 个会话",
			"board.unknownProject": "未知项目",
			"board.widget.label": "Sidecar",
			"board.widget.connection.ok": "已连接",
			"board.widget.connection.degraded": "连接不稳定",
			"board.widget.connection.off": "离线",
			"board.widget.working": "{n} 个会话工作中",
			"detail.header.close": "返回看板",
			"detail.header.listenOn": "监听中",
			"detail.header.listenOff": "监听",
			"detail.header.listenHint": "开启后新事件将实时追加并高亮",
			"detail.header.refresh": "刷新",
			"detail.header.refreshing": "刷新中…",
			"detail.header.refreshHint": "手动拉取最新时间线窗口",
			"detail.header.copyIdTitle": "点击复制会话 ID",
			"detail.header.copied": "已复制",
			"detail.header.untitled": "(无标题)",
			"detail.header.unknownProject": "未知项目",
			"detail.header.observedDisclaimer": "状态为从持久化数据推断的观察值,可能滞后",
			"detail.status.working": "工作中",
			"detail.status.waiting": "等待中",
			"detail.status.idle": "空闲",
			"detail.status.dead": "已结束",
			"detail.status.unknown": "未知",
			"detail.sources.title": "数据来源",
			"detail.sources.dshLive": "dsh 实时",
			"detail.sources.dshCold": "dsh 冷读",
			"detail.sources.sidecarReplay": "sidecar 重放",
			"detail.sources.sidecarBuffer": "sidecar 缓冲",
			"detail.sources.none": "来源未知",
			"detail.kind.user": "用户消息",
			"detail.kind.assistant": "助手回复",
			"detail.kind.thinking": "思考",
			"detail.kind.toolCall": "工具调用",
			"detail.kind.toolResult": "工具结果",
			"detail.kind.turn": "回合",
			"detail.kind.step": "步骤",
			"detail.kind.error": "错误",
			"detail.kind.other": "事件",
			"detail.gap.label": "缺口:可能有 {n} 条事件未捕获(256 队列上限或未持久化)",
			"detail.filter.conversation": "只看对话",
			"detail.filter.all": "全部事件",
			"detail.filter.hiddenNotice": "已隐藏 {n} 条协议事件",
			"detail.timeline.loadMore": "加载更多历史",
			"detail.timeline.loadingMore": "加载中…",
			"detail.timeline.noMore": "已到时间线起点",
			"detail.timeline.expand": "展开",
			"detail.timeline.collapse": "收起",
			"detail.timeline.newBadge": "新",
			"detail.timeline.seq": "seq {n}",
			"detail.timeline.hiddenNotice": "为保持流畅,较早的 {n} 条已折叠",
			"detail.timeline.showAll": "全部显示",
			"detail.timeline.chunkRun": "{n} 个流式分块",
			"detail.states.loadingTitle": "正在加载时间线…",
			"detail.states.emptyTitle": "暂无事件",
			"detail.states.emptyHint": "该会话还没有可展示的规范化事件。",
			"detail.states.errorTitle": "时间线加载失败",
			"detail.states.errorFallback": "错误码:{reason}",
			"detail.states.errors.session_not_found": "会话不存在或已不可见",
			"detail.states.errors.invalid_cursor": "分页游标无效,请重新打开详情",
			"detail.states.errors.fusion_not_wired": "当前 host 未启用时间线能力",
			"detail.states.errors.network_error": "网络错误,无法联系 dsh host",
			"detail.states.errors.request_timeout": "请求超时",
			"detail.time.justNow": "刚刚",
			"detail.time.minutesAgo": "{n} 分钟前",
			"detail.time.hoursAgo": "{n} 小时前",
			"detail.time.daysAgo": "{n} 天前",
			"detail.actions.inject": "注入",
			"detail.actions.analyze": "AI 分析",
			"detail.actions.analyzeDisabledHint": "在设置中开启「启用 AI 旁路分析」后可用",
			"detail.tools.title": "谱系与检索",
			"detail.tools.show": "展开",
			"detail.tools.hide": "收起",
			"dshtools.lineage.title": "会话谱系",
			"dshtools.lineage.loading": "谱系加载中…",
			"dshtools.lineage.error": "谱系加载失败",
			"dshtools.lineage.empty": "暂无谱系数据",
			"dshtools.lineage.currentBadge": "当前会话",
			"dshtools.lineage.liveBadge": "运行中",
			"dshtools.lineage.notPersistedBadge": "未持久化",
			"dshtools.lineage.role.ancestor": "祖先",
			"dshtools.lineage.role.target": "目标",
			"dshtools.lineage.role.descendant": "子会话",
			"dshtools.lineage.jumpTitle": "跳转到该会话",
			"dshtools.lineage.currentTitle": "正在查看该会话",
			"dshtools.lineage.expand": "展开",
			"dshtools.lineage.collapse": "折叠",
			"dshtools.lineage.nodeCount": "{n} 个会话",
			"dshtools.lineage.incompleteWithId": "谱系不完整:父会话 {id} 无法解析",
			"dshtools.lineage.incomplete": "谱系不完整:部分父链无法解析",
			"dshtools.lineage.degrade.notDshTitle": "谱系/溯源为 dsh 会话专属",
			"dshtools.lineage.degrade.notDshBody": "当前会话来自外部 agent,dsh 的谱系与溯源能力不适用。",
			"dshtools.lineage.degrade.queryUnavailableTitle": "dsh 谱系服务不可用",
			"dshtools.lineage.degrade.queryUnavailableBody": "当前 dsh 组合未挂载 sessionQuery,谱系与溯源暂不可用。",
			"dshtools.lineage.degrade.traceFailedTitle": "谱系追溯失败",
			"dshtools.lineage.degrade.traceFailedBody": "dsh 无法解析该会话的谱系。",
			"dshtools.lineage.degrade.unknownTitle": "谱系不可用",
			"dshtools.lineage.degrade.unknownBody": "后端报告谱系不可用(原因: {reason})。",
			"dshtools.search.title": "会话检索",
			"dshtools.search.placeholder": "检索会话(标题 / 项目 / 全文)",
			"dshtools.search.submit": "检索",
			"dshtools.search.loading": "检索中…",
			"dshtools.search.error": "检索失败",
			"dshtools.search.empty": "没有匹配的会话",
			"dshtools.search.filterOnlyNotice": "dsh 全文检索不可用,已降级为标题/项目过滤",
			"dshtools.search.projectFilter": "项目过滤: {project}",
			"dshtools.search.matchedBy.full-text": "全文",
			"dshtools.search.matchedBy.title": "标题",
			"dshtools.search.matchedBy.project": "项目",
			"dshtools.search.matchedBy.other": "其他",
			"dshtools.search.untitled": "(无标题)",
			"project.title": "项目关联",
			"project.summary": "{projects} 个项目 · {sessions} 个会话",
			"project.crossAgent": "{n} 种 agent",
			"project.sessionCount": "{n} 个会话",
			"project.lastActive": "最近活跃 {time}",
			"project.liveChip": "实时",
			"project.untitled": "(无标题)",
			"project.showAllSessions": "展开全部 {n} 个会话",
			"project.showLessSessions": "只看前 {n} 个",
			"project.empty.title": "暂无项目关联",
			"project.empty.hint": "时间窗内没有跨 agent 的项目活动;agent 在某个项目目录下开始工作后会出现在这里。",
			"project.loading": "正在加载项目关联…",
			"project.errorTitle": "项目关联加载失败",
			"analysis.title": "AI 分析",
			"analysis.close": "关闭",
			"analysis.disabledNote": "AI 旁路分析未开启;请在设置中开启「启用 AI 旁路分析」。",
			"analysis.idleHint": "按需拉起一次 dsh 旁路分析,解读该会话的当前状态与走向(消耗模型 token)。",
			"analysis.start": "开始分析",
			"analysis.requesting": "分析中…(最长约 60 秒)",
			"analysis.exchangeInitial": "分析摘要",
			"analysis.followupLabel": "追问",
			"analysis.truncatedNotice": "输入超出预算已截断,分析基于部分上下文。",
			"analysis.emptySummary": "(分析会话未返回摘要)",
			"analysis.disclaimerFallback": "AI 分析仅供参考,结论以实际会话为准。",
			"analysis.followupPlaceholder": "继续追问这次分析…",
			"analysis.followupSubmit": "追问",
			"analysis.answering": "回答中…",
			"analysis.stop": "停止分析",
			"analysis.stopped": "分析已停止,分析会话已释放。",
			"analysis.restart": "重新分析",
			"analysis.noticeTimeout": "本次追问超时,可稍后重试;分析会话仍保留。",
			"analysis.noticeNetwork": "请求未能送达,可重试;分析会话仍保留。",
			"analysis.noticeCancelFailed": "停止请求未送达,请重试。",
			"analysis.errDisabled": "AI 分析已在服务端关闭;请在设置中开启后重试。",
			"analysis.errUnavailable": "当前 host 未接入 AI 分析能力(agents 服务不可用)。",
			"analysis.errTargetNotFound": "分析目标不存在或已离开观测范围。",
			"analysis.errTooManyActive": "并发分析会话已达上限,请稍后再试。",
			"analysis.errTimeout": "分析超时,分析会话已释放;可重新发起。",
			"analysis.errCreateFailed": "分析会话创建失败。",
			"analysis.errCancelled": "分析已取消。",
			"analysis.errNetwork": "网络错误,分析请求未能完成。",
			"analysis.errGeneric": "分析失败({code})。",
			"command.description": "查看 Sidecar 状态速览(daemon、连接与会话)",
			"command.daemon.probe": "探测中",
			"command.daemon.adopted": "已连接 · 领养",
			"command.daemon.defer": "等待系统服务",
			"command.daemon.reprobe": "重新探测中",
			"command.daemon.hosting": "正在启动",
			"command.daemon.hosted": "已连接 · 托管",
			"command.daemon.backoff": "重启退避中",
			"command.daemon.failed": "离线",
			"command.daemon.unknown": "状态未知",
			"command.connection.ok": "已连接",
			"command.connection.degraded": "连接不稳定",
			"command.connection.off": "离线",
			"command.status.working": "工作中",
			"command.status.waiting": "等待中",
			"command.status.idle": "空闲",
			"command.status.dead": "已结束",
			"command.status.unknown": "未知",
			"command.daemonRow": "Sidecar · {state}",
			"command.countsRow": "{working} 个工作中 · {waiting} 个等待中",
			"command.countsDetail": "共 {total} 个会话",
			"command.sessionDetail": "{project} · {status} · {time}",
			"command.noSessions": "暂无被观测的会话",
			"command.unknownProject": "未知项目",
			"command.untitled": "(无标题)",
			"command.truncated": "还有 {n} 个会话未列出",
			"command.boardHint": "打开会话视图的「Sidecar」Tab 查看完整看板",
			"command.unreachable": "sidecar 未连接",
			"command.unreachableHint": "无法获取状态快照;请确认 agent-sidecar 插件已启用、daemon 可用后重试。",
			"command.offlineFailed": "sidecar 已离线(daemon 连续启动失败已熔断)",
			"command.offlineFailedHint": "以下为最后一次快照。可在设置卡重试,或手动运行 agent-sidecar daemon start 后等待自动领养。",
			"command.offlineDefer": "等待系统服务拉起 daemon",
			"command.offlineDeferHint": "检测到 LaunchAgent 托管,插件只探测等待;服务拉起后速览会自动恢复。",
			"command.time.justNow": "刚刚",
			"command.time.minutesAgo": "{n} 分钟前",
			"command.time.hoursAgo": "{n} 小时前",
			"command.time.daysAgo": "{n} 天前",
			"sidebar.centerEntryLabel": "Agent 中心",
			"sidebar.centerEntryAria": "打开 Agent 中心",
			"sidebar.tabTitle": "Sidecar",
			"sidebar.countsRow": "{working} 工作中 · {waiting} 等待中",
			"sidebar.recentTitle": "最近活跃",
			"sidebar.connecting": "等待 sidecar 快照…",
			"sidebar.noSessions": "暂无活跃会话",
			"sidebar.noEvent": "暂无事件记录",
			"sidebar.untitled": "(无标题)",
			"sidebar.boardHint": "完整看板见会话视图的「Sidecar」Tab"
		};
		//#endregion
		//#region src/client/locales/en.ts
		const en = {
			"settings.cardTitle": "Agent Sidecar",
			"settings.cardDescription": "Runtime settings for cross-agent session monitoring, injection and bypass analysis.",
			"settings.docsLink": "Documentation",
			"settings.readOnly": "The settings document is read-only; edits cannot be saved.",
			"settings.unsaved": "Unsaved",
			"settings.save": "Save",
			"settings.saving": "Saving…",
			"settings.discard": "Discard",
			"settings.saveFailed": "Save failed; please retry.",
			"settings.expand": "Expand",
			"settings.collapse": "Collapse",
			"settings.invalidNumber": "Enter an integer no less than {min}",
			"settings.sectionDaemon": "Daemon lifecycle",
			"settings.daemonPolicyLabel": "Management policy",
			"settings.daemonPolicyHint": "adopt-or-host probes and adopts an existing daemon, else spawns one; adopt-only never spawns; off leaves the lifecycle alone (read-only reconcile still runs).",
			"settings.daemonPolicyAdoptOrHost": "adopt-or-host (adopt, else spawn)",
			"settings.daemonPolicyAdoptOnly": "adopt-only (never spawn)",
			"settings.daemonPolicyOff": "off (unmanaged)",
			"settings.daemonBackoffLimitLabel": "Backoff limit",
			"settings.daemonBackoffLimitHint": "After this many consecutive hosting failures the supervisor stops restarting and trips failed.",
			"settings.daemonStatusLabel": "Daemon status",
			"settings.daemonStateProbe": "Probing",
			"settings.daemonStateAdopted": "Adopted an existing daemon",
			"settings.daemonStateDefer": "Waiting for the system service",
			"settings.daemonStateReprobe": "Re-probing",
			"settings.daemonStateHosting": "Spawning",
			"settings.daemonStateHosted": "Hosted by the plugin",
			"settings.daemonStateBackoff": "Backing off before retry",
			"settings.daemonStateFailed": "Tripped (circuit open)",
			"settings.daemonPidVersion": "pid {pid} · v{version}",
			"settings.daemonDeferNote": "The daemon is managed by a system service (LaunchAgent); the plugin only probes and waits — it never spawns a duplicate or terminates it.",
			"settings.daemonFailedNote": "Consecutive hosting failures reached the backoff limit; the board degraded to the last snapshot. Check the sidecar command, then retry.",
			"settings.daemonRetry": "Retry",
			"settings.sectionSidecar": "Sidecar invocation",
			"settings.sidecarCommandLabel": "Executable command",
			"settings.sidecarCommandHint": "A PATH name, an absolute path, or a space-separated multi-part command (e.g. python3 /path/agent-sidecar.pyz); the plugin never installs the sidecar for you.",
			"settings.sidecarRuntimeDirLabel": "Runtime directory",
			"settings.sidecarRuntimeDirHint": "Empty uses the default ~/.agent_sidecar (honoring AGENT_SIDECAR_RUNTIME_DIR); a non-empty value is passed to spawned daemons via the environment.",
			"settings.sectionStream": "Stream reconciliation",
			"settings.streamActiveMsLabel": "Active cadence (ms)",
			"settings.streamActiveMsHint": "Status snapshot cadence while any session is working.",
			"settings.streamIdleMsLabel": "Idle cadence (ms)",
			"settings.streamIdleMsHint": "Status snapshot cadence while no session is working.",
			"settings.sectionInject": "Message injection",
			"settings.injectEnabledLabel": "Enable injection",
			"settings.injectEnabledHint": "When off, the inject panel renders read-only and disabled, and the server rejects write actions.",
			"settings.injectDefaultModeLabel": "Default injection mode",
			"settings.injectDefaultModeHint": "The mode preselected when the inject panel opens.",
			"settings.injectModeQueue": "queue (next turn)",
			"settings.injectModeSteer": "steer (mid-turn)",
			"settings.injectSafetyNote": "Safety: injection is off by default; even when enabled, every injection still passes a per-request confirmation dialog — there is no batch or scheduled injection. Not recommended on multi-user hosts.",
			"settings.sectionAnalysis": "Bypass analysis",
			"settings.analysisEnabledLabel": "Enable AI bypass analysis",
			"settings.analysisEnabledHint": "Spins up a dsh analysis session over an observed session on demand (consumes model tokens; off by default).",
			"settings.sectionUi": "Board UI",
			"settings.uiTimeWindowHoursLabel": "Session time window (hours)",
			"settings.uiTimeWindowHoursHint": "The board lists only sessions active within this window.",
			"settings.uiShowDeadLabel": "Show dead sessions",
			"settings.uiShowDeadHint": "Also list finished (dead) sessions on the board.",
			"settings.sectionSkill": "Skill mode",
			"settings.skillProvideLabel": "Provide the skill in-process",
			"settings.skillProvideHint": "Provide the agent-sidecar skill to dsh via registerProvider (enabled in M4; applies after restart).",
			"inject.title": "Inject message",
			"inject.confirmTitle": "Confirm injection",
			"inject.close": "Close",
			"inject.done": "Done",
			"inject.capabilityOff": "Injection is not enabled; turn it on in Settings first.",
			"inject.noTarget": "No injection target selected; start from a board card or a session detail page.",
			"inject.targetLabel": "Target",
			"inject.messageLabel": "Message",
			"inject.messagePlaceholder": "Message to inject (16 KiB max; never paste secrets)",
			"inject.byteCount": "{bytes} / {limit} bytes",
			"inject.msgEmpty": "The message must not be empty.",
			"inject.msgNul": "The message contains an illegal NUL character.",
			"inject.msgTooLarge": "The message is {bytes} bytes, over the {limit}-byte limit.",
			"inject.modeLabel": "Injection mode",
			"inject.modeQueue": "queue (next turn)",
			"inject.modeQueueHint": "The message queues and is handled when the target session starts its next turn.",
			"inject.modeSteer": "steer (mid-turn)",
			"inject.modeSteerHint": "The message is injected mid-turn and steers the target session immediately.",
			"inject.argvWarning": "cursor-cli target: injection runs through its native subprocess, and the message is visible in this machine's process list while that process lives; never include secrets.",
			"inject.auditNote": "This injection is recorded in the sidecar audit log (byte size and content fingerprint; never the message plaintext).",
			"inject.prepare": "Prepare injection",
			"inject.preparing": "Validating…",
			"inject.planTargetLabel": "Target snapshot",
			"inject.planStatus": "Current status: {status}",
			"inject.statusObservedNote": "Status is an observed value inferred from persisted data and may lag.",
			"inject.planModeLabel": "Mode",
			"inject.planPreviewLabel": "Message digest ({bytes} bytes)",
			"inject.countdown": "The confirm token expires in {seconds}s",
			"inject.confirmExecute": "Confirm injection",
			"inject.executing": "Injecting…",
			"inject.cancel": "Cancel",
			"inject.tokenExpired": "Confirmation timed out and the token is void; prepare the injection again.",
			"inject.resultDelivered": "Delivered: the message was injected into the target session.",
			"inject.resultFailed": "Injection failed.",
			"inject.resultUnknown": "Outcome unknown: the message may have been delivered. Do NOT retry; check the target session before deciding anything.",
			"inject.resultReplayed": "Idempotent replay: this is the earlier result of the same request — no second injection happened.",
			"inject.reprepare": "Prepare again",
			"inject.observeListen": "Listen for the reaction",
			"inject.errInjectDisabled": "Injection is disabled on the server; enable it in Settings.",
			"inject.errInvalidMessage": "The message failed server-side validation.",
			"inject.errTargetNotFound": "The target session does not exist or left the observation window.",
			"inject.errTargetDead": "The target session has ended (dead); it cannot be injected.",
			"inject.errTooManyPending": "Too many injections are pending confirmation; try again later.",
			"inject.errTokenMissing": "The confirm token is missing or was never issued; prepare again.",
			"inject.errTokenExpired": "The confirm token expired; prepare again.",
			"inject.errTokenReused": "The confirm token was already consumed; prepare again.",
			"inject.errTokenMismatch": "The confirmation no longer matches what was prepared; prepare again.",
			"inject.errUnsupportedAgent": "This agent has no injection path.",
			"inject.errExecutorError": "The injection path failed while executing.",
			"inject.errTimeout": "The request timed out without a server receipt.",
			"inject.errAborted": "The request was cancelled.",
			"inject.errNetwork": "Network error; the request could not be sent.",
			"inject.errParse": "The server response could not be parsed.",
			"inject.errGeneric": "Request failed ({code}).",
			"board.viewBoard": "Session board",
			"board.viewProjects": "Projects",
			"board.status.working": "Working",
			"board.status.waiting": "Waiting",
			"board.status.idle": "Idle",
			"board.status.dead": "Finished",
			"board.status.unknown": "Unknown",
			"board.attention.gap": "Event gap",
			"board.daemon.probe": "Probing",
			"board.daemon.adopted": "Connected · adopted",
			"board.daemon.defer": "Waiting for the system service",
			"board.daemon.reprobe": "Re-probing",
			"board.daemon.hosting": "Starting",
			"board.daemon.hosted": "Connected · hosted",
			"board.daemon.backoff": "Restart backoff",
			"board.daemon.failed": "Offline",
			"board.stream.ok": "Live stream healthy",
			"board.stream.degraded": "Live stream reconnecting",
			"board.stream.unknown": "Live stream not connected",
			"board.banner.daemonFailed": "sidecar is offline: the board shows the last snapshot and will not update",
			"board.banner.streamDegraded": "The live stream is reconnecting; data may be stale",
			"board.empty.daemonFailedTitle": "sidecar is offline",
			"board.empty.daemonFailedHint": "Repeated daemon start failures tripped the circuit breaker. Retry from Settings, or run agent-sidecar daemon start and wait for automatic adoption.",
			"board.empty.daemonDeferTitle": "Waiting for the system service to start the daemon",
			"board.empty.daemonDeferHint": "A LaunchAgent manages the daemon, so the plugin will not start it. The board populates automatically once the service starts it.",
			"board.empty.filteredTitle": "No sessions match these filters",
			"board.empty.filteredHint": "Try a wider time window, clear the status filter, or enable \"Show finished\".",
			"board.empty.noSessionsTitle": "No observed sessions yet",
			"board.empty.noSessionsHint": "Local agents (claude / codex / cursor / dsh …) appear here automatically once they start working.",
			"board.topbar.title": "Sidecar multi-agent board",
			"board.topbar.refresh": "Refresh",
			"board.topbar.refreshing": "Refreshing…",
			"board.topbar.refreshTitle": "Fetch the latest snapshot",
			"board.topbar.refreshFailed": "Refresh failed; the board is still showing the previous snapshot",
			"board.topbar.dismiss": "Dismiss",
			"board.topbar.showDead": "Show finished",
			"board.topbar.timeWindow": "Time window",
			"board.topbar.countWorking": "{n} working",
			"board.topbar.countWaiting": "{n} waiting",
			"board.topbar.countTotal": "{n} sessions total",
			"board.topbar.filterByStatusTitle": "Show only sessions marked \"{label}\"",
			"board.topbar.clearStatusFilterTitle": "Clear the status filter and show all sessions",
			"board.group.collapseTitle": "Collapse this group",
			"board.group.expandTitle": "Expand this group",
			"board.group.showAll": "Show all {n} sessions",
			"board.group.showLess": "Show first {n} only",
			"board.card.noEvent": "No events yet",
			"board.card.untitled": "(untitled)",
			"board.card.observedDisclaimer": "Status is an observed value inferred from persisted data and may lag",
			"board.card.observedValue": "Observed value: {status}",
			"board.card.lastReconcile": "Last reconciled: {time}",
			"board.card.neverReconciled": "Not reconciled yet",
			"board.card.copyId": "Click to copy the full session ID",
			"board.card.copied": "Copied",
			"board.time.justNow": "just now",
			"board.time.minutesAgo": "{n} min ago",
			"board.time.hoursAgo": "{n} h ago",
			"board.timeWindow.hours": "{n} hours",
			"board.timeWindow.days": "{n} days",
			"board.groupCount": "{n} sessions",
			"board.unknownProject": "Unknown project",
			"board.widget.label": "Sidecar",
			"board.widget.connection.ok": "Connected",
			"board.widget.connection.degraded": "Connection unstable",
			"board.widget.connection.off": "Offline",
			"board.widget.working": "{n} sessions working",
			"detail.header.close": "Back to board",
			"detail.header.listenOn": "Listening",
			"detail.header.listenOff": "Listen",
			"detail.header.listenHint": "New events append live and get highlighted while on",
			"detail.header.refresh": "Refresh",
			"detail.header.refreshing": "Refreshing…",
			"detail.header.refreshHint": "Pull the newest timeline window",
			"detail.header.copyIdTitle": "Click to copy the session ID",
			"detail.header.copied": "Copied",
			"detail.header.untitled": "(untitled)",
			"detail.header.unknownProject": "Unknown project",
			"detail.header.observedDisclaimer": "Status is an observed value inferred from persisted data and may lag",
			"detail.status.working": "Working",
			"detail.status.waiting": "Waiting",
			"detail.status.idle": "Idle",
			"detail.status.dead": "Finished",
			"detail.status.unknown": "Unknown",
			"detail.sources.title": "Data sources",
			"detail.sources.dshLive": "dsh live",
			"detail.sources.dshCold": "dsh cold read",
			"detail.sources.sidecarReplay": "sidecar replay",
			"detail.sources.sidecarBuffer": "sidecar buffer",
			"detail.sources.none": "Unknown source",
			"detail.kind.user": "User message",
			"detail.kind.assistant": "Assistant reply",
			"detail.kind.thinking": "Thinking",
			"detail.kind.toolCall": "Tool call",
			"detail.kind.toolResult": "Tool result",
			"detail.kind.turn": "Turn",
			"detail.kind.step": "Step",
			"detail.kind.error": "Error",
			"detail.kind.other": "Event",
			"detail.gap.label": "Gap: about {n} events may be uncaptured (256-slot queue cap or not persisted)",
			"detail.filter.conversation": "Conversation only",
			"detail.filter.all": "All events",
			"detail.filter.hiddenNotice": "{n} protocol events hidden",
			"detail.timeline.loadMore": "Load older history",
			"detail.timeline.loadingMore": "Loading…",
			"detail.timeline.noMore": "Start of the timeline",
			"detail.timeline.expand": "Expand",
			"detail.timeline.collapse": "Collapse",
			"detail.timeline.newBadge": "New",
			"detail.timeline.seq": "seq {n}",
			"detail.timeline.hiddenNotice": "{n} earlier entries collapsed to stay smooth",
			"detail.timeline.showAll": "Show all",
			"detail.timeline.chunkRun": "{n} streaming chunks",
			"detail.states.loadingTitle": "Loading the timeline…",
			"detail.states.emptyTitle": "No events yet",
			"detail.states.emptyHint": "This session has no normalized events to show yet.",
			"detail.states.errorTitle": "Timeline failed to load",
			"detail.states.errorFallback": "Error code: {reason}",
			"detail.states.errors.session_not_found": "The session does not exist or is no longer visible",
			"detail.states.errors.invalid_cursor": "The paging cursor is invalid; reopen the detail view",
			"detail.states.errors.fusion_not_wired": "This host has no timeline capability enabled",
			"detail.states.errors.network_error": "Network error; the dsh host is unreachable",
			"detail.states.errors.request_timeout": "The request timed out",
			"detail.time.justNow": "just now",
			"detail.time.minutesAgo": "{n} min ago",
			"detail.time.hoursAgo": "{n} h ago",
			"detail.time.daysAgo": "{n} d ago",
			"detail.actions.inject": "Inject",
			"detail.actions.analyze": "AI analysis",
			"detail.actions.analyzeDisabledHint": "Enable \"AI bypass analysis\" in Settings to use this",
			"detail.tools.title": "Lineage & search",
			"detail.tools.show": "Show",
			"detail.tools.hide": "Hide",
			"dshtools.lineage.title": "Session lineage",
			"dshtools.lineage.loading": "Loading lineage…",
			"dshtools.lineage.error": "Lineage failed to load",
			"dshtools.lineage.empty": "No lineage data",
			"dshtools.lineage.currentBadge": "Current session",
			"dshtools.lineage.liveBadge": "Live",
			"dshtools.lineage.notPersistedBadge": "Not persisted",
			"dshtools.lineage.role.ancestor": "Ancestor",
			"dshtools.lineage.role.target": "Target",
			"dshtools.lineage.role.descendant": "Child session",
			"dshtools.lineage.jumpTitle": "Jump to this session",
			"dshtools.lineage.currentTitle": "Currently viewing this session",
			"dshtools.lineage.expand": "Expand",
			"dshtools.lineage.collapse": "Collapse",
			"dshtools.lineage.nodeCount": "{n} sessions",
			"dshtools.lineage.incompleteWithId": "Lineage incomplete: parent session {id} could not be resolved",
			"dshtools.lineage.incomplete": "Lineage incomplete: part of the parent chain is unresolved",
			"dshtools.lineage.degrade.notDshTitle": "Lineage/provenance is dsh-session only",
			"dshtools.lineage.degrade.notDshBody": "This session comes from an external agent; dsh's lineage and provenance do not apply.",
			"dshtools.lineage.degrade.queryUnavailableTitle": "dsh lineage service unavailable",
			"dshtools.lineage.degrade.queryUnavailableBody": "This dsh composition has no sessionQuery mounted; lineage and provenance are unavailable.",
			"dshtools.lineage.degrade.traceFailedTitle": "Lineage trace failed",
			"dshtools.lineage.degrade.traceFailedBody": "dsh could not resolve this session's lineage.",
			"dshtools.lineage.degrade.unknownTitle": "Lineage unavailable",
			"dshtools.lineage.degrade.unknownBody": "The backend reports lineage unavailable (reason: {reason}).",
			"dshtools.search.title": "Session search",
			"dshtools.search.placeholder": "Search sessions (title / project / full text)",
			"dshtools.search.submit": "Search",
			"dshtools.search.loading": "Searching…",
			"dshtools.search.error": "Search failed",
			"dshtools.search.empty": "No matching sessions",
			"dshtools.search.filterOnlyNotice": "dsh full-text search is unavailable; degraded to title/project filtering",
			"dshtools.search.projectFilter": "Project filter: {project}",
			"dshtools.search.matchedBy.full-text": "Full text",
			"dshtools.search.matchedBy.title": "Title",
			"dshtools.search.matchedBy.project": "Project",
			"dshtools.search.matchedBy.other": "Other",
			"dshtools.search.untitled": "(untitled)",
			"project.title": "Project correlation",
			"project.summary": "{projects} projects · {sessions} sessions",
			"project.crossAgent": "{n} agent kinds",
			"project.sessionCount": "{n} sessions",
			"project.lastActive": "Last active {time}",
			"project.liveChip": "Live",
			"project.untitled": "(untitled)",
			"project.showAllSessions": "Show all {n} sessions",
			"project.showLessSessions": "Show first {n} only",
			"project.empty.title": "No project correlation yet",
			"project.empty.hint": "No cross-agent project activity within the time window; projects appear here once an agent works inside a project directory.",
			"project.loading": "Loading project correlation…",
			"project.errorTitle": "Project correlation failed to load",
			"analysis.title": "AI analysis",
			"analysis.close": "Close",
			"analysis.disabledNote": "AI bypass analysis is off; enable \"AI bypass analysis\" in Settings first.",
			"analysis.idleHint": "Spin up one dsh bypass-analysis pass over this session on demand (consumes model tokens).",
			"analysis.start": "Start analysis",
			"analysis.requesting": "Analyzing… (up to ~60 s)",
			"analysis.exchangeInitial": "Analysis summary",
			"analysis.followupLabel": "Follow-up",
			"analysis.truncatedNotice": "The input exceeded the budget and was truncated; the analysis covers partial context.",
			"analysis.emptySummary": "(the analysis session returned no summary)",
			"analysis.disclaimerFallback": "AI analysis is for reference only; trust the actual session over its conclusions.",
			"analysis.followupPlaceholder": "Ask a follow-up about this analysis…",
			"analysis.followupSubmit": "Ask",
			"analysis.answering": "Answering…",
			"analysis.stop": "Stop analysis",
			"analysis.stopped": "Analysis stopped; the analysis session was released.",
			"analysis.restart": "Analyze again",
			"analysis.noticeTimeout": "This follow-up timed out; retry later — the analysis session is kept.",
			"analysis.noticeNetwork": "The request could not be sent; retry — the analysis session is kept.",
			"analysis.noticeCancelFailed": "The stop request did not go through; try again.",
			"analysis.errDisabled": "AI analysis is disabled on the server; enable it in Settings and retry.",
			"analysis.errUnavailable": "This host has no AI analysis capability (agents service unavailable).",
			"analysis.errTargetNotFound": "The analysis target does not exist or left the observation window.",
			"analysis.errTooManyActive": "Too many concurrent analysis sessions; try again later.",
			"analysis.errTimeout": "The analysis timed out and its session was released; start again.",
			"analysis.errCreateFailed": "Failed to create the analysis session.",
			"analysis.errCancelled": "The analysis was cancelled.",
			"analysis.errNetwork": "Network error; the analysis request could not complete.",
			"analysis.errGeneric": "Analysis failed ({code}).",
			"command.description": "Sidecar status at a glance (daemon, connection, sessions)",
			"command.daemon.probe": "Probing",
			"command.daemon.adopted": "Connected · adopted",
			"command.daemon.defer": "Waiting for the system service",
			"command.daemon.reprobe": "Re-probing",
			"command.daemon.hosting": "Starting",
			"command.daemon.hosted": "Connected · hosted",
			"command.daemon.backoff": "Restart backoff",
			"command.daemon.failed": "Offline",
			"command.daemon.unknown": "State unknown",
			"command.connection.ok": "Connected",
			"command.connection.degraded": "Connection unstable",
			"command.connection.off": "Offline",
			"command.status.working": "Working",
			"command.status.waiting": "Waiting",
			"command.status.idle": "Idle",
			"command.status.dead": "Finished",
			"command.status.unknown": "Unknown",
			"command.daemonRow": "Sidecar · {state}",
			"command.countsRow": "{working} working · {waiting} waiting",
			"command.countsDetail": "{total} sessions total",
			"command.sessionDetail": "{project} · {status} · {time}",
			"command.noSessions": "No observed sessions yet",
			"command.unknownProject": "Unknown project",
			"command.untitled": "(untitled)",
			"command.truncated": "{n} more sessions not listed",
			"command.boardHint": "Open the \"Sidecar\" tab in the conversation view for the full board",
			"command.unreachable": "sidecar is not connected",
			"command.unreachableHint": "The state snapshot could not be fetched; check that the agent-sidecar plugin is enabled and the daemon is available, then retry.",
			"command.offlineFailed": "sidecar is offline (daemon start failures tripped the breaker)",
			"command.offlineFailedHint": "Showing the last snapshot. Retry from the settings card, or run agent-sidecar daemon start manually and wait for adoption.",
			"command.offlineDefer": "Waiting for the system service to start the daemon",
			"command.offlineDeferHint": "A LaunchAgent manages the daemon; the plugin only probes and waits. The overview recovers automatically once the service brings it up.",
			"command.time.justNow": "just now",
			"command.time.minutesAgo": "{n} min ago",
			"command.time.hoursAgo": "{n} h ago",
			"command.time.daysAgo": "{n} d ago",
			"sidebar.centerEntryLabel": "Agent Center",
			"sidebar.centerEntryAria": "Open Agent Center",
			"sidebar.tabTitle": "Sidecar",
			"sidebar.countsRow": "{working} working · {waiting} waiting",
			"sidebar.recentTitle": "Recently active",
			"sidebar.connecting": "Waiting for the sidecar snapshot…",
			"sidebar.noSessions": "No active sessions",
			"sidebar.noEvent": "No events recorded yet",
			"sidebar.untitled": "(untitled)",
			"sidebar.boardHint": "Full board: the \"Sidecar\" tab in the conversation view"
		};
		//#endregion
		//#region src/client/locales/host.ts
		function localeTag(value) {
			if (typeof value === "string") return value;
			if (value === null || value === void 0) return void 0;
			return value.active ?? value.preference ?? value.locale ?? value.value?.preference;
		}
		/** Map every Chinese locale variant to zh; all other values use en. */
		function mapHostLocale(value) {
			return localeTag(value)?.trim().toLowerCase().startsWith("zh") === true ? "zh" : "en";
		}
		/**
		* Register dictionaries, adopt the current Host locale, and follow changes.
		* Every capability is optional and isolated: absent or throwing Host methods
		* leave the module-owned locale fallback intact.
		*/
		function bridgeHostLocale(host, options) {
			const disposers = [];
			const service = host?.locale;
			for (const locale of ["zh", "en"]) try {
				const dispose = service?.register?.(options.namespace, locale, options.dictionaries[locale]);
				if (typeof dispose === "function") disposers.push(dispose);
			} catch {}
			const read = () => {
				try {
					return service?.getLocale?.();
				} catch {
					return;
				}
			};
			const apply = (value) => {
				if (localeTag(value) === void 0) return;
				try {
					options.onLocale(mapHostLocale(value));
				} catch {}
			};
			try {
				const dispose = host?.on?.("locale/change", (value) => {
					apply(value === void 0 ? read() : value);
				});
				if (typeof dispose === "function") disposers.push(dispose);
			} catch {}
			apply(read());
			let disposed = false;
			return () => {
				if (disposed) return;
				disposed = true;
				for (const dispose of disposers.reverse()) try {
					dispose();
				} catch {}
			};
		}
		/** Host namespace owned by Agent Sidecar dictionaries. */
		const SIDECAR_LOCALE_NAMESPACE = "agent-sidecar";
		/** Complete shipped dictionaries keyed by locale id. */
		const dictionaries = {
			zh,
			en
		};
		/**
		* Substitute `{name}` template params; unknown placeholders stay verbatim
		* (same semantics as the ecosystem LocaleRuntime.translate).
		*/
		function interpolate(template, params) {
			if (!params) return template;
			return template.replace(/\{(\w+)\}/g, (match, name) => name in params ? String(params[name]) : match);
		}
		/**
		* Build a translate engine over an arbitrary (possibly partial) dictionary
		* set. Lookup chain per key: requested locale → {@link BASE_LOCALE} (zh) →
		* the key itself. Exposed for tests and for compositions that need a
		* non-shipped dictionary set; the shipped {@link t} is this engine bound to
		* {@link dictionaries} and the module's active locale.
		* @param dicts - dictionaries keyed by locale id (missing locales allowed).
		* @returns pure translate function addressed by explicit locale.
		*/
		function createTranslator(dicts) {
			return (locale, key, params) => {
				return interpolate(dicts[locale]?.[key] ?? dicts["zh"]?.[key] ?? key, params);
			};
		}
		const shippedTranslate = createTranslator(dictionaries);
		let activeLocale = "zh";
		const localeListeners = /* @__PURE__ */ new Set();
		/** @returns the active locale id. */
		function getLocale() {
			return activeLocale;
		}
		/**
		* Switch the active locale and notify subscribers (no-op when unchanged).
		* @param locale - a shipped locale id.
		*/
		function setLocale(locale) {
			if (activeLocale === locale) return;
			activeLocale = locale;
			for (const fn of [...localeListeners]) fn();
		}
		/**
		* Observe active-locale switches (React consumers pair this with
		* {@link getLocale} as a uSES source).
		* @param fn - change callback.
		* @returns unsubscribe.
		*/
		function subscribeLocale(fn) {
			localeListeners.add(fn);
			return () => {
				localeListeners.delete(fn);
			};
		}
		const HOST_LOCALE_LEASES_SYMBOL = Symbol.for("@shendeguize/dsh-agent-sidecar/host-locale-leases");
		const globalSymbols = globalThis;
		function hasMethod(value, key) {
			return typeof value === "object" && value !== null && typeof value[key] === "function";
		}
		function isLeaseRegistry(value) {
			return hasMethod(value, "get") && hasMethod(value, "set") && hasMethod(value, "delete");
		}
		function isOwnerSet(value) {
			return hasMethod(value, "add") && hasMethod(value, "delete") && hasMethod(value, Symbol.iterator) && typeof value.size === "number";
		}
		function isHostLocaleArbiter(value) {
			if (typeof value !== "object" || value === null) return false;
			const candidate = value;
			return isLeaseRegistry(candidate.leases) && isOwnerSet(candidate.owners) && (candidate.activeOwner === null || typeof candidate.activeOwner === "object") && typeof candidate.detachEvents === "function";
		}
		const sharedLocaleState = globalSymbols[HOST_LOCALE_LEASES_SYMBOL];
		const hostLocaleArbiter = isHostLocaleArbiter(sharedLocaleState) ? sharedLocaleState : {
			leases: isLeaseRegistry(sharedLocaleState) ? sharedLocaleState : /* @__PURE__ */ new WeakMap(),
			owners: /* @__PURE__ */ new Set(),
			activeOwner: null,
			detachEvents: () => {}
		};
		globalSymbols[HOST_LOCALE_LEASES_SYMBOL] = hostLocaleArbiter;
		function bridge(host, onLocale) {
			return bridgeHostLocale(host, {
				namespace: SIDECAR_LOCALE_NAMESPACE,
				dictionaries,
				onLocale
			});
		}
		function followOwner(owner) {
			return bridge({
				locale: { getLocale: () => owner.service.getLocale?.() },
				on: (event, listener) => owner.host.on?.(event, listener)
			}, (locale) => {
				if (hostLocaleArbiter.activeOwner === owner) owner.onLocale(locale);
			});
		}
		function latestOwner(owners) {
			let latest;
			for (const owner of owners) latest = owner;
			return latest;
		}
		function detachActiveFollower() {
			const detach = hostLocaleArbiter.detachEvents;
			hostLocaleArbiter.detachEvents = () => {};
			try {
				detach();
			} catch {}
		}
		function activateOwner(owner) {
			detachActiveFollower();
			hostLocaleArbiter.activeOwner = owner;
			hostLocaleArbiter.detachEvents = followOwner(owner);
		}
		/**
		* Lease the shipped dictionaries and locale following for one Host service.
		* Dictionary ownership is keyed by service identity. Event ownership is
		* global across services and bundles so only the latest attached owner can
		* drive its module-local locale.
		*/
		function attachHostLocale(host) {
			if (host?.locale === void 0) return () => {};
			const service = host.locale;
			const owner = {
				host,
				service,
				onLocale: setLocale
			};
			let lease = hostLocaleArbiter.leases.get(service);
			if (lease === void 0) {
				lease = {
					owners: /* @__PURE__ */ new Set(),
					detachDictionaries: bridge({ locale: service }, () => {})
				};
				hostLocaleArbiter.leases.set(service, lease);
			}
			lease.owners.add(owner);
			hostLocaleArbiter.owners.add(owner);
			activateOwner(owner);
			let disposed = false;
			return () => {
				if (disposed) return;
				disposed = true;
				const wasActive = hostLocaleArbiter.activeOwner === owner;
				lease.owners.delete(owner);
				hostLocaleArbiter.owners.delete(owner);
				if (wasActive) {
					detachActiveFollower();
					hostLocaleArbiter.activeOwner = null;
				}
				if (lease.owners.size === 0) {
					lease.detachDictionaries();
					hostLocaleArbiter.leases.delete(service);
				}
				if (!wasActive) return;
				if (hostLocaleArbiter.owners.size > 0) {
					activateOwner(latestOwner(hostLocaleArbiter.owners));
					return;
				}
				owner.onLocale("zh");
			};
		}
		/**
		* Translate a typed key in the active locale. Missing entries fall back to
		* zh, then to the key itself (see module doc for why the chain ends visible).
		* @param key - a key of the shipped table.
		* @param params - optional `{name}` template params.
		* @returns the translated string.
		*/
		function t(key, params) {
			return shippedTranslate(activeLocale, key, params);
		}
		//#endregion
		//#region src/client/locales/view.ts
		function buildNode(descriptor) {
			const view = {};
			for (const [name, value] of Object.entries(descriptor)) {
				const property = typeof value === "string" ? {
					enumerable: true,
					get: () => t(value)
				} : {
					enumerable: true,
					value: buildNode(value)
				};
				Object.defineProperty(view, name, property);
			}
			return view;
		}
		/**
		* Build one stable, enumerable locale facade. Leaf getters call {@link t} at
		* read time, so the same facade follows subsequent active-locale changes.
		*/
		function createLocaleView(descriptor) {
			return buildNode(descriptor);
		}
		//#endregion
		//#region src/client/board/strings.ts
		/** Dynamic board vocabulary; template leaves are interpolated by `formatTemplate`. */
		const BOARD_STRINGS = createLocaleView({
			status: {
				working: "board.status.working",
				waiting: "board.status.waiting",
				idle: "board.status.idle",
				dead: "board.status.dead",
				unknown: "board.status.unknown"
			},
			attention: { gap: "board.attention.gap" },
			daemon: {
				probe: "board.daemon.probe",
				adopted: "board.daemon.adopted",
				defer: "board.daemon.defer",
				reprobe: "board.daemon.reprobe",
				hosting: "board.daemon.hosting",
				hosted: "board.daemon.hosted",
				backoff: "board.daemon.backoff",
				failed: "board.daemon.failed"
			},
			stream: {
				ok: "board.stream.ok",
				degraded: "board.stream.degraded",
				unknown: "board.stream.unknown"
			},
			banner: {
				daemonFailed: "board.banner.daemonFailed",
				streamDegraded: "board.banner.streamDegraded"
			},
			empty: {
				daemonFailedTitle: "board.empty.daemonFailedTitle",
				daemonFailedHint: "board.empty.daemonFailedHint",
				daemonDeferTitle: "board.empty.daemonDeferTitle",
				daemonDeferHint: "board.empty.daemonDeferHint",
				filteredTitle: "board.empty.filteredTitle",
				filteredHint: "board.empty.filteredHint",
				noSessionsTitle: "board.empty.noSessionsTitle",
				noSessionsHint: "board.empty.noSessionsHint"
			},
			topbar: {
				title: "board.topbar.title",
				refresh: "board.topbar.refresh",
				refreshing: "board.topbar.refreshing",
				refreshTitle: "board.topbar.refreshTitle",
				refreshFailed: "board.topbar.refreshFailed",
				dismiss: "board.topbar.dismiss",
				showDead: "board.topbar.showDead",
				timeWindow: "board.topbar.timeWindow",
				countWorking: "board.topbar.countWorking",
				countWaiting: "board.topbar.countWaiting",
				countTotal: "board.topbar.countTotal",
				filterByStatusTitle: "board.topbar.filterByStatusTitle",
				clearStatusFilterTitle: "board.topbar.clearStatusFilterTitle"
			},
			group: {
				collapseTitle: "board.group.collapseTitle",
				expandTitle: "board.group.expandTitle",
				showAll: "board.group.showAll",
				showLess: "board.group.showLess"
			},
			card: {
				noEvent: "board.card.noEvent",
				untitled: "board.card.untitled",
				observedDisclaimer: "board.card.observedDisclaimer",
				observedValue: "board.card.observedValue",
				lastReconcile: "board.card.lastReconcile",
				neverReconciled: "board.card.neverReconciled",
				copyId: "board.card.copyId",
				copied: "board.card.copied"
			},
			time: {
				justNow: "board.time.justNow",
				minutesAgo: "board.time.minutesAgo",
				hoursAgo: "board.time.hoursAgo"
			},
			timeWindow: {
				hours: "board.timeWindow.hours",
				days: "board.timeWindow.days"
			},
			groupCount: "board.groupCount",
			unknownProject: "board.unknownProject",
			widget: {
				label: "board.widget.label",
				connection: {
					ok: "board.widget.connection.ok",
					degraded: "board.widget.connection.degraded",
					off: "board.widget.connection.off"
				},
				working: "board.widget.working"
			}
		});
		//#endregion
		//#region src/client/board/logic.ts
		/**
		* Pure view-model logic for the cross-agent session board (design §5.1
		* view 1) and the footer status widget (view 4). No React, no I/O, no
		* imports from the data layer (api/sse) — everything arrives as plain
		* values and leaves as plain values, so this whole module is unit-testable
		* in a bare node environment.
		*
		* Decoupling contract (T2.2 ↔ T2.4): the types below are this module's OWN
		* view models. The integration task maps the host wire shapes
		* (`SessionView` / `StateSnapshot` from the state endpoint) onto them.
		* Notably every timestamp here is **epoch milliseconds** — the sidecar
		* snapshot carries epoch seconds (`updated_at`), so the mapping layer
		* multiplies by 1000 once at the boundary.
		*
		* @module
		*/
		const MINUTE_MS$2 = 6e4;
		const HOUR_MS$2 = 36e5;
		const DAY_MS$2 = 864e5;
		/** Resolve `{name}` placeholders in a message template. */
		function formatTemplate$2(template, params) {
			return template.replace(/\{(\w+)\}/g, (match, key) => {
				const value = params[key];
				return value === void 0 ? match : String(value);
			});
		}
		const KNOWN_STATUSES$1 = [
			"working",
			"waiting",
			"idle",
			"dead"
		];
		/** Map a raw observed status onto the badge vocabulary ('unknown' fallback). */
		function normalizeStatus(raw) {
			const cleaned = raw.trim().toLowerCase();
			return KNOWN_STATUSES$1.includes(cleaned) ? cleaned : "unknown";
		}
		const STATUS_RANK = {
			working: 0,
			waiting: 1,
			idle: 2,
			unknown: 3,
			dead: 4
		};
		/** Sort rank: working > waiting > idle > unknown > dead (lower sorts first). */
		function statusRank(status) {
			return STATUS_RANK[status];
		}
		const STATUS_TONE$1 = {
			working: "success",
			waiting: "warn",
			idle: "neutral",
			unknown: "neutral",
			dead: "muted"
		};
		/**
		* Visibility rules (task spec):
		* - an active `statusFilter` (UX-01) overrides everything: only sessions
		*   of that status are visible, regardless of window or showDead — the
		*   count badge and the filtered board therefore always agree;
		* - dead sessions are hidden unless `showDead`;
		* - working sessions are always visible (even outside the window);
		* - everything else hides once `updatedAtMs` falls strictly beyond the
		*   window (age === window is still visible);
		* - a non-finite or non-positive window disables the age filter entirely.
		*/
		function isSessionVisible(session, filters, nowMs) {
			const status = normalizeStatus(session.status);
			if (filters.statusFilter !== void 0) return status === filters.statusFilter;
			if (status === "dead" && !filters.showDead) return false;
			if (status === "working") return true;
			const windowMs = filters.timeWindowHours * HOUR_MS$2;
			if (!Number.isFinite(windowMs) || windowMs <= 0) return true;
			return nowMs - session.updatedAtMs <= windowMs;
		}
		/** Apply {@link isSessionVisible} across a session list (order preserved). */
		function filterSessions(sessions, filters, nowMs) {
			return sessions.filter((s) => isSessionVisible(s, filters, nowMs));
		}
		/** Human display name for a project path ('' → 未知项目). */
		function projectDisplayName(project) {
			const trimmed = project.trim().replace(/[/\\]+$/, "");
			if (trimmed === "") return BOARD_STRINGS.unknownProject;
			const segments = trimmed.split(/[/\\]/);
			const base = segments[segments.length - 1] ?? "";
			return base === "" ? trimmed : base;
		}
		/** Card ordering inside a group: status rank, then recency, then id. */
		function compareCards(a, b) {
			const rankDelta = statusRank(normalizeStatus(a.status)) - statusRank(normalizeStatus(b.status));
			if (rankDelta !== 0) return rankDelta;
			if (a.updatedAtMs !== b.updatedAtMs) return b.updatedAtMs - a.updatedAtMs;
			return a.sessionId.localeCompare(b.sessionId);
		}
		/**
		* Group sessions by project. Empty/whitespace projects share the
		* unknown-project bucket (key ''). Groups are ordered by their most recent
		* `updatedAtMs` descending, except the unknown bucket which always sorts
		* last (named projects are more actionable than the catch-all). Cards
		* inside each group follow {@link compareCards}.
		*/
		function groupSessions(sessions) {
			const buckets = /* @__PURE__ */ new Map();
			for (const session of sessions) {
				const key = session.project.trim() === "" ? "" : session.project;
				const bucket = buckets.get(key);
				if (bucket === void 0) buckets.set(key, [session]);
				else bucket.push(session);
			}
			const groups = [];
			for (const [key, cards] of buckets) {
				cards.sort(compareCards);
				groups.push({
					key,
					label: key === "" ? BOARD_STRINGS.unknownProject : projectDisplayName(key),
					fullPath: key,
					cards
				});
			}
			const newest = (group) => group.cards.reduce((max, card) => Math.max(max, card.updatedAtMs), Number.NEGATIVE_INFINITY);
			groups.sort((a, b) => {
				if (a.key === "") return b.key === "" ? 0 : 1;
				if (b.key === "") return -1;
				return newest(b) - newest(a) || a.key.localeCompare(b.key);
			});
			return groups;
		}
		/**
		* status + gap → badge tone/label/attention.
		*
		* Card-level attention carries ONLY the per-session `gap` marker (a known
		* data hole for THIS session). The global stream-health state is a
		* board-wide fact and lives in the top banner alone — repeating it on
		* every card was noise, not signal (UX-18). Unknown raw statuses keep
		* their raw text as the label — the board never invents a state.
		*/
		function deriveBadge(rawStatus, gap) {
			const status = normalizeStatus(rawStatus);
			const trimmed = rawStatus.trim();
			const label = status === "unknown" ? trimmed === "" ? BOARD_STRINGS.status.unknown : trimmed : BOARD_STRINGS.status[status];
			const attention = gap ? "gap" : null;
			return {
				status,
				tone: STATUS_TONE$1[status],
				label,
				attention,
				attentionLabel: attention === null ? null : BOARD_STRINGS.attention[attention]
			};
		}
		/**
		* Hover title for a status badge: the observed-value disclaimer (design
		* §5.3 wording), the raw observed status, and the last reconcile time.
		*/
		function badgeHoverTitle(rawStatus, lastReconcileAtMs, nowMs) {
			const observed = rawStatus.trim() === "" ? BOARD_STRINGS.status.unknown : rawStatus.trim();
			const reconcile = lastReconcileAtMs === null ? BOARD_STRINGS.card.neverReconciled : formatTemplate$2(BOARD_STRINGS.card.lastReconcile, { time: formatRelativeTime$1(lastReconcileAtMs, nowMs) });
			return [
				BOARD_STRINGS.card.observedDisclaimer,
				formatTemplate$2(BOARD_STRINGS.card.observedValue, { status: observed }),
				reconcile
			].join("\n");
		}
		/**
		* Absolute short timestamp in local time: `MM-DD HH:mm`. Used for ages
		* beyond 24h where relative buckets stop discriminating (UX-13).
		*/
		function formatAbsoluteShort(thenMs) {
			const date = new Date(thenMs);
			const pad = (n) => String(n).padStart(2, "0");
			return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
		}
		/**
		* Card time: <60s (including clock skew into the future) is 刚刚, then
		* whole minutes/hours; from 24h on the absolute short date takes over —
		* a column of "3 天前" carries no information, "08-22 14:03" does
		* (UX-13). Non-finite input renders empty.
		*/
		function formatRelativeTime$1(thenMs, nowMs) {
			if (!Number.isFinite(thenMs)) return "";
			const delta = nowMs - thenMs;
			if (delta < MINUTE_MS$2) return BOARD_STRINGS.time.justNow;
			if (delta < HOUR_MS$2) return formatTemplate$2(BOARD_STRINGS.time.minutesAgo, { n: Math.floor(delta / MINUTE_MS$2) });
			if (delta < DAY_MS$2) return formatTemplate$2(BOARD_STRINGS.time.hoursAgo, { n: Math.floor(delta / HOUR_MS$2) });
			return formatAbsoluteShort(thenMs);
		}
		/** Label for a time-window option: whole days as 天, otherwise 小时. */
		function timeWindowLabel(hours) {
			if (hours >= 24 && hours % 24 === 0) return formatTemplate$2(BOARD_STRINGS.timeWindow.days, { n: hours / 24 });
			return formatTemplate$2(BOARD_STRINGS.timeWindow.hours, { n: hours });
		}
		const DAEMON_TONE = {
			probe: "neutral",
			adopted: "success",
			defer: "warn",
			reprobe: "neutral",
			hosting: "neutral",
			hosted: "success",
			backoff: "warn",
			failed: "danger"
		};
		/** Daemon state → top-bar badge (tone + label). */
		function deriveDaemonBadge(state) {
			return {
				tone: DAEMON_TONE[state],
				label: BOARD_STRINGS.daemon[state]
			};
		}
		/** Stream health → indicator tone. */
		function streamHealthTone(health) {
			if (health === "ok") return "success";
			if (health === "degraded") return "warn";
			return "neutral";
		}
		/**
		* Top banner: daemon FAILED (red, last-snapshot notice) outranks a
		* degraded stream (yellow, may-lag notice). 'unknown' stream health alone
		* raises no banner — it is the pre-first-connect startup state and a
		* warning bar on every mount would be noise.
		*/
		function deriveBanner(daemonState, streamHealth) {
			if (daemonState === "failed") return {
				tone: "danger",
				text: BOARD_STRINGS.banner.daemonFailed
			};
			if (streamHealth === "degraded") return {
				tone: "warn",
				text: BOARD_STRINGS.banner.streamDegraded
			};
			return null;
		}
		/**
		* Empty-state guidance, only when nothing is visible. Daemon trouble
		* (failed > defer) explains an empty board better than filter settings;
		* otherwise distinguish "all filtered out" from "nothing observed yet".
		*/
		function deriveEmptyState(daemonState, visibleCount, totalCount) {
			if (visibleCount > 0) return null;
			if (daemonState === "failed") return {
				kind: "daemon-failed",
				title: BOARD_STRINGS.empty.daemonFailedTitle,
				hint: BOARD_STRINGS.empty.daemonFailedHint
			};
			if (daemonState === "defer") return {
				kind: "daemon-defer",
				title: BOARD_STRINGS.empty.daemonDeferTitle,
				hint: BOARD_STRINGS.empty.daemonDeferHint
			};
			if (totalCount > 0) return {
				kind: "filtered",
				title: BOARD_STRINGS.empty.filteredTitle,
				hint: BOARD_STRINGS.empty.filteredHint
			};
			return {
				kind: "no-sessions",
				title: BOARD_STRINGS.empty.noSessionsTitle,
				hint: BOARD_STRINGS.empty.noSessionsHint
			};
		}
		const AGENT_GLYPHS$1 = {
			dsh: "◆",
			claude: "✳",
			codex: "▣",
			cursor: "▮",
			"cursor-cli": "▮",
			"cursor-ide": "▮",
			copilot: "◉",
			kimi: "◐"
		};
		/** Single-character agent marker; unknown agents get a neutral dot. */
		function agentGlyph$1(agent) {
			return AGENT_GLYPHS$1[agent.trim().toLowerCase()] ?? "●";
		}
		/** Head…tail abbreviation for long session ids (full id goes in `title`). */
		function abbreviateSessionId(id, max = 20) {
			if (id.length <= max) return id;
			return `${id.slice(0, 12)}…${id.slice(-6)}`;
		}
		/**
		* Slice a status-sorted card list down to `limit` for display, unless
		* `expanded`. The cut never lands inside the leading working/waiting run:
		* attention-worthy sessions are the reason the board exists, so the
		* effective limit grows to cover all of them (an all-active group renders
		* fully — honest, and rare). Non-positive limits disable truncation.
		*/
		function sliceCardsForDisplay(cards, limit, expanded) {
			if (expanded || limit <= 0 || cards.length <= limit) return {
				shown: [...cards],
				hiddenCount: 0
			};
			let activeRun = 0;
			while (activeRun < cards.length) {
				const status = normalizeStatus(cards[activeRun].status);
				if (status !== "working" && status !== "waiting") break;
				activeRun += 1;
			}
			const effectiveLimit = Math.max(limit, activeRun);
			return {
				shown: cards.slice(0, effectiveLimit),
				hiddenCount: cards.length - Math.min(cards.length, effectiveLimit)
			};
		}
		/**
		* Connection dot: green only when the daemon is connected (adopted/hosted)
		* AND the event stream is healthy; FAILED is the only hard-off state;
		* every transitional state shows the cautious yellow.
		*/
		function deriveWidgetConnection(daemonState, streamHealth) {
			if (daemonState === "failed") return "off";
			if ((daemonState === "adopted" || daemonState === "hosted") && streamHealth === "ok") return "ok";
			return "degraded";
		}
		/** Count of sessions observed in one normalized status. */
		function countByStatus(sessions, status) {
			let count = 0;
			for (const session of sessions) if (normalizeStatus(session.status) === status) count += 1;
			return count;
		}
		/** Count of sessions currently observed as working. */
		function countWorking(sessions) {
			return countByStatus(sessions, "working");
		}
		/** Widget hover/aria text: connection state, plus the count when nonzero. */
		function widgetTitle(connection, workingCount) {
			const base = `${BOARD_STRINGS.widget.label}: ${BOARD_STRINGS.widget.connection[connection]}`;
			if (workingCount <= 0) return base;
			return `${base} · ${formatTemplate$2(BOARD_STRINGS.widget.working, { n: workingCount })}`;
		}
		/** filter → group → per-card derive; the one call Board.tsx renders from. */
		function buildBoardViewModel(input) {
			const { sessions, filters, daemonState, streamHealth, lastReconcileAtMs, nowMs } = input;
			const visible = filterSessions(sessions, filters, nowMs);
			return {
				groups: groupSessions(visible.map((session) => ({
					...session,
					badge: deriveBadge(session.status, session.gap),
					glyph: agentGlyph$1(session.agent),
					shortId: abbreviateSessionId(session.sessionId),
					relativeTime: formatRelativeTime$1(session.updatedAtMs, nowMs),
					hoverTitle: badgeHoverTitle(session.status, lastReconcileAtMs, nowMs)
				}))),
				banner: deriveBanner(daemonState, streamHealth),
				emptyState: deriveEmptyState(daemonState, visible.length, sessions.length),
				daemonBadge: deriveDaemonBadge(daemonState),
				streamLabel: BOARD_STRINGS.stream[streamHealth],
				streamTone: streamHealthTone(streamHealth),
				visibleCount: visible.length,
				totalCount: sessions.length,
				workingCount: countWorking(sessions),
				waitingCount: countByStatus(sessions, "waiting")
			};
		}
		//#endregion
		//#region src/client/lifecycle/handoff.ts
		const DEFAULT_DELAYS_MS = [
			8,
			16,
			32,
			64,
			128,
			256,
			496
		];
		const defaultScheduler = {
			setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
			clearTimeout: (handle) => globalThis.clearTimeout(handle)
		};
		/**
		* Acquire a registry resource now, or briefly wait for an overlapping old
		* fiber to release it. The returned lease owns both waiting and acquisition.
		*/
		function acquireWithHandoff(register, options) {
			const scheduler = options.scheduler ?? defaultScheduler;
			let stopped = false;
			let attempts = 0;
			let delayIndex = 0;
			let timer;
			let acquired;
			const finishError = (error) => {
				stopped = true;
				options.onError(error);
			};
			const attempt = () => {
				if (stopped) return;
				timer = void 0;
				attempts += 1;
				try {
					acquired = register();
				} catch (error) {
					if (!options.isCollision(error)) {
						finishError(error);
						return;
					}
				}
				if (acquired !== void 0) return;
				if (attempts >= 8 || delayIndex >= DEFAULT_DELAYS_MS.length) {
					stopped = true;
					options.onTimeout();
					return;
				}
				const delay = DEFAULT_DELAYS_MS[delayIndex++];
				try {
					timer = scheduler.setTimeout(attempt, delay);
				} catch (error) {
					finishError(error);
				}
			};
			attempt();
			return () => {
				if (stopped) return;
				stopped = true;
				if (timer !== void 0) scheduler.clearTimeout(timer);
				const dispose = acquired;
				acquired = void 0;
				dispose?.();
			};
		}
		/** Only known duplicate-registry diagnostics are eligible for handoff. */
		function isRegistrationCollision(error) {
			return error instanceof Error && /\b(?:duplicate|already registered)\b/i.test(error.message);
		}
		//#endregion
		//#region src/client/locales/command.ts
		/**
		* `command.*` locale segment for the `/sidecar` slash command (T4.6).
		*
		* T5.10b unification: the `command.*` copy now LIVES in the main
		* dictionaries (./zh.ts + ./en.ts, domain `command`) and this module is a
		* thin derived view kept for its consumers ({@link tCommand} for
		* commands.ts, {@link commandZh}/{@link commandEn} for tests and for the
		* `ctx.locale.register(ns, locale, dict)` bridge). Translation goes
		* through the shared engine and the shared active-locale switch, so
		* `setLocale` drives every surface consistently and the main-table parity
		* test covers these keys too.
		*
		* @module
		*/
		function commandSlice(dict) {
			const out = {};
			for (const [key, value] of Object.entries(dict)) if (key.startsWith("command.")) out[key] = value;
			return out;
		}
		commandSlice(zh);
		commandSlice(en);
		/**
		* Translate a command-segment key in the shared active locale (delegates
		* to the main-table {@link t}; lookup chain: active locale → zh → key).
		* @param key - a key of the command segment.
		* @param params - optional `{name}` template params.
		* @returns the translated string.
		*/
		function tCommand(key, params) {
			return t(key, params);
		}
		//#endregion
		//#region src/client/commands.ts
		/**
		* `/sidecar` slash command (design §4.c `ctx.commands` row / M2 交付,
		* T4.6): a client-owned quick status overview of the sidecar — daemon
		* state, connection health, working/waiting counts, and the top-N active
		* sessions grouped by project — plus a pointer to the full board tab.
		*
		* MECHANISM (source audit, code-first): this dsh version has a CLIENT-side
		* slash-command extension point. The web GUI's slash menu is served by the
		* harness `ui-commands` client package, whose `CommandUiRuntime` service
		* (registered as `commandUi`) accepts client-owned contributions:
		*
		* - `commandUi.register({ name, description, available, ui })` adds one
		*   slash-menu entry whose behavior lives entirely on the client (no host
		*   descriptor) — merged with the host catalog by name, collisions fail
		*   loud, duplicate contributions throw at registration
		*   (harness `packages/client/ui-commands/src/client/contract.ts` +
		*   `service.ts`; ecosystem precedent for the host-side flavor:
		*   `dsh-agent-teams/src/command.ts`).
		* - The only `ui.kind` this dsh version supports is `popupSelect`:
		*   an async `options(session, signal)` provider plus `onSelect`. The
		*   `/sidecar` overview therefore presents as a popup card of rows; its
		*   board row opens Agent Center while informational rows remain inert.
		*
		* The `commandUi` service type is NOT part of the published plugin SDK
		* (same situation as `settingsScope` in ./index.ts), so this module keeps
		* STRUCTURAL mirrors of the harness contract, verified against the source
		* above. Registration goes through `ctx.inject(['commandUi'], …)` — lazy,
		* like the design's `ctx.commands` row: a composition without the service
		* simply never gains the command.
		*
		* DATA: the overview reuses the existing client data layer (`fetchState`,
		* ./api.ts) and the board's pure derivations (./board/logic.ts). No new
		* backend endpoint. The snapshot→overview derivation is the pure, fully
		* unit-testable {@link buildOverview}.
		*
		* Wiring note (S5 integration wave): call {@link registerSidecarCommand}
		* from the client `apply`; this module performs no self-registration.
		*
		* @module
		*/
		/** The slash command name (without the leading slash). */
		const SIDECAR_COMMAND_NAME = "sidecar";
		const MINUTE_MS$1 = 6e4;
		const HOUR_MS$1 = 36e5;
		const DAY_MS$1 = 864e5;
		/**
		* Coarse relative time over the command locale segment (same thresholds as
		* the board's formatRelativeTime, which is bound to the zh-only board
		* table and therefore not reused here).
		*/
		function relativeTime(thenMs, nowMs) {
			if (!Number.isFinite(thenMs)) return "";
			const delta = nowMs - thenMs;
			if (delta < MINUTE_MS$1) return tCommand("command.time.justNow");
			if (delta < HOUR_MS$1) return tCommand("command.time.minutesAgo", { n: Math.floor(delta / MINUTE_MS$1) });
			if (delta < DAY_MS$1) return tCommand("command.time.hoursAgo", { n: Math.floor(delta / HOUR_MS$1) });
			return tCommand("command.time.daysAgo", { n: Math.floor(delta / DAY_MS$1) });
		}
		/** Wire SessionView → board card VM (epoch seconds → epoch ms boundary). */
		function toCardVM(view) {
			return {
				agent: view.agent,
				sessionId: view.session_id,
				status: view.status,
				title: view.title,
				project: view.project,
				updatedAtMs: view.updated_at * 1e3,
				lastEvent: view.last_event === null ? null : {
					kind: view.last_event.kind,
					text: view.last_event.text
				},
				gap: view.gap
			};
		}
		/** Card VM → overview row (labels resolved in the active locale). */
		function toRow(card, nowMs) {
			const status = normalizeStatus(card.status);
			const title = card.title.trim();
			return {
				agent: card.agent,
				glyph: agentGlyph$1(card.agent),
				sessionId: card.sessionId,
				shortId: abbreviateSessionId(card.sessionId),
				status,
				statusLabel: tCommand(`command.status.${status}`),
				title: title === "" ? tCommand("command.untitled") : title,
				relativeTime: relativeTime(card.updatedAtMs, nowMs)
			};
		}
		/** Offline/unreachable guidance for the given daemon situation. */
		function deriveGuidance(daemonState) {
			if (daemonState === null) return {
				kind: "unreachable",
				title: tCommand("command.unreachable"),
				hint: tCommand("command.unreachableHint")
			};
			if (daemonState === "failed") return {
				kind: "daemon-failed",
				title: tCommand("command.offlineFailed"),
				hint: tCommand("command.offlineFailedHint")
			};
			if (daemonState === "defer") return {
				kind: "daemon-defer",
				title: tCommand("command.offlineDefer"),
				hint: tCommand("command.offlineDeferHint")
			};
			return null;
		}
		/**
		* Pure snapshot→overview derivation. `null` means the state endpoint was
		* unreachable (daemon offline / plugin host down): the model degrades to
		* the 「sidecar 未连接」 guidance. A snapshot with daemon failed/defer keeps
		* its (last-snapshot) sessions and counts — same honesty posture as the
		* board's degraded banner — with the matching guidance attached.
		*
		* Grouping and ordering reuse the board's pure logic: groups by project
		* (most recent first, unknown-project bucket last), cards by status rank
		* then recency. The top-N cap walks groups in that order; groups beyond
		* the cap are dropped and their sessions counted in `truncatedCount`.
		* Dead sessions are never listed (the overview is an "active sessions"
		* glance) but still count into `totalCount`.
		*/
		function buildOverview(snapshot, opts = {}) {
			const boardHint = tCommand("command.boardHint");
			if (snapshot === null) return {
				reachable: false,
				daemonState: null,
				daemonLabel: tCommand("command.daemon.unknown"),
				connection: "off",
				connectionLabel: tCommand("command.connection.off"),
				workingCount: 0,
				waitingCount: 0,
				totalCount: 0,
				groups: [],
				truncatedCount: 0,
				guidance: deriveGuidance(null),
				boardHint
			};
			const nowMs = opts.nowMs ?? Date.now();
			const topN = opts.topN ?? 5;
			const daemonState = snapshot.daemon.state;
			const sessions = snapshot.board.sessions;
			const connection = deriveWidgetConnection(daemonState, snapshot.board.streamHealth);
			const active = sessions.filter((view) => normalizeStatus(view.status) !== "dead").map(toCardVM);
			const groups = [];
			let remaining = Math.max(0, topN);
			let truncatedCount = 0;
			for (const group of groupSessions(active)) {
				if (remaining <= 0) {
					truncatedCount += group.cards.length;
					continue;
				}
				const taken = group.cards.slice(0, remaining);
				truncatedCount += group.cards.length - taken.length;
				remaining -= taken.length;
				groups.push({
					key: group.key,
					label: group.key === "" ? tCommand("command.unknownProject") : group.label,
					fullPath: group.fullPath,
					rows: taken.map((card) => toRow(card, nowMs))
				});
			}
			return {
				reachable: true,
				daemonState,
				daemonLabel: tCommand(`command.daemon.${daemonState}`),
				connection,
				connectionLabel: tCommand(`command.connection.${connection}`),
				workingCount: countWorking(sessions),
				waitingCount: sessions.filter((view) => normalizeStatus(view.status) === "waiting").length,
				totalCount: sessions.length,
				groups,
				truncatedCount,
				guidance: deriveGuidance(daemonState),
				boardHint
			};
		}
		/**
		* Render the overview as popupSelect rows (stable unique ids). Row order:
		* daemon/connection status, offline guidance (when any), counts or the
		* empty notice, session rows in group order (project carried in the
		* detail line), the truncation marker, and the board-tab pointer.
		*/
		function overviewToOptions(model) {
			const options = [{
				id: "daemon",
				label: tCommand("command.daemonRow", { state: model.daemonLabel }),
				detail: model.connectionLabel
			}];
			if (model.guidance !== null) options.push({
				id: `guidance:${model.guidance.kind}`,
				label: model.guidance.title,
				detail: model.guidance.hint
			});
			if (model.reachable) {
				if (model.totalCount === 0) options.push({
					id: "empty",
					label: tCommand("command.noSessions")
				});
				else options.push({
					id: "counts",
					label: tCommand("command.countsRow", {
						working: model.workingCount,
						waiting: model.waitingCount
					}),
					detail: tCommand("command.countsDetail", { total: model.totalCount })
				});
				for (const group of model.groups) for (const row of group.rows) options.push({
					id: `session:${row.sessionId}`,
					label: `${row.glyph} ${row.title}`,
					detail: tCommand("command.sessionDetail", {
						project: group.label,
						status: row.statusLabel,
						time: row.relativeTime
					})
				});
				if (model.truncatedCount > 0) options.push({
					id: "truncated",
					label: tCommand("command.truncated", { n: model.truncatedCount })
				});
			}
			options.push({
				id: "board",
				label: model.boardHint
			});
			return options;
		}
		/**
		* Build the `/sidecar` client command contribution. `options` fetches a
		* fresh snapshot per popup open; an abort (popup closed) propagates so the
		* shell can drop the stale request, while every other failure degrades to
		* the unreachable-guidance overview instead of throwing into the shell.
		* `description` is a live getter so a locale switch after registration
		* still reaches the slash menu's next candidate pass.
		*/
		function createSidecarCommandContribution(deps = {}) {
			const doFetch = deps.fetchState ?? fetchState;
			const now = deps.now ?? Date.now;
			return {
				name: SIDECAR_COMMAND_NAME,
				get description() {
					return tCommand("command.description");
				},
				available: () => true,
				ui: {
					kind: "popupSelect",
					options: async (_session, signal) => {
						let snapshot = null;
						try {
							snapshot = await doFetch({ signal });
						} catch (err) {
							if (isApiError(err) && err.kind === "aborted") throw err;
							console.error("agent-sidecar: /sidecar state fetch failed", err);
						}
						const overviewOpts = { nowMs: now() };
						if (deps.topN !== void 0) overviewOpts.topN = deps.topN;
						return overviewToOptions(buildOverview(snapshot, overviewOpts));
					},
					onSelect: (option) => {
						if (option.id === "board") deps.openCenter?.();
					}
				}
			};
		}
		/**
		* Register `/sidecar` once the `commandUi` service is available (lazy, per
		* the design's `ctx.commands` consumption row — a composition without the
		* slash-menu runtime simply never gains the command).
		*
		* HMR handoff: a duplicate contribution means the old fiber still owns the
		* name. The new injected fiber retries briefly, then registers its own fresh
		* contribution after the old disposer runs. Foreign squatters time out.
		*/
		function registerSidecarCommand(ctx, deps = {}) {
			try {
				ctx.inject(["commandUi"], (injected) => {
					const { commandUi } = injected;
					return acquireWithHandoff(() => commandUi.register(createSidecarCommandContribution(deps)), {
						isCollision: isRegistrationCollision,
						onError: (error) => {
							console.error("agent-sidecar: /sidecar command registration failed", error);
						},
						onTimeout: () => {
							console.error("agent-sidecar: /sidecar command handoff timed out");
						}
					});
				});
			} catch (err) {
				console.error("agent-sidecar: commandUi injection failed", err);
			}
		}
		//#endregion
		//#region src/client/sse.ts
		/**
		* StateStream — the browser half's live data feed (design §5.3 / ADR-3).
		*
		* One class, two modes, one UI-facing surface (onSnapshot/onStatus):
		*
		* - 'sse' (main channel): EventSource on `GET <prefix>/stream`. Every
		*   `state` event carries a full StateSnapshot (routes.ts pushes full
		*   snapshots; heartbeats are comment frames the browser never surfaces).
		*   On top of EventSource's native auto-reconnect, consecutive errors
		*   beyond the threshold — or a CLOSED readyState — degrade the stream:
		*   the instance is torn down and manually rebuilt on a 5s→30s capped
		*   exponential backoff. `degraded` is a visible status, never a circuit
		*   break; a successful reconnect resets the ladder.
		* - 'poll' (settings fallback, stream.mode=poll): pollFn (defaults to
		*   api.fetchState) on a dual cadence — 3s while the latest snapshot has
		*   a `working` session, 15s otherwise. `visible()` returning false
		*   pauses fetching (ticks keep running as cheap visibility checks), and
		*   {@link StateStream.pollNow} gives the visibilitychange handler an
		*   immediate resume fetch.
		*
		* The status vocabulary is unified across modes:
		* 'connecting' | 'open' | 'degraded'. stop() is terminal and tears
		* everything down: timers, the EventSource, the in-flight poll fetch
		* (aborted), and both listener sets.
		*
		* Pure data layer: no React, no slots SDK, injectable primitives only.
		*
		* @module
		*/
		/** SSE endpoint within the plugin route namespace (host: routes.ts). */
		const STREAM_PATH = `${API_PREFIX}/stream`;
		/** EventSource.CLOSED (numeric literal so fakes need no DOM constants). */
		const EVENTSOURCE_CLOSED = 2;
		const DEFAULT_POLL_ACTIVE_MS = 3e3;
		const DEFAULT_POLL_IDLE_MS = 15e3;
		const DEFAULT_ERROR_THRESHOLD = 3;
		const DEFAULT_BACKOFF_BASE_MS = 5e3;
		const DEFAULT_BACKOFF_CAP_MS = 3e4;
		const defaultSetTimeout$2 = (fn, ms) => globalThis.setTimeout(fn, ms);
		const defaultClearTimeout$2 = (handle) => {
			globalThis.clearTimeout(handle);
		};
		const defaultCreateAbortController$2 = () => new AbortController();
		const defaultEventSourceFactory = (url) => {
			const Ctor = globalThis.EventSource;
			if (Ctor === void 0) throw new Error("EventSource unavailable: inject eventSourceFactory or use poll mode");
			return new Ctor(url);
		};
		const defaultPollFn = (opts) => fetchState({ signal: opts.signal });
		var StateStream = class {
			url;
			streamMode;
			esFactory;
			pollFn;
			pollActiveMs;
			pollIdleMs;
			visibleFn;
			setT;
			clearT;
			createController;
			errorThreshold;
			backoffBaseMs;
			backoffCapMs;
			currentStatus = "connecting";
			started = false;
			stopped = false;
			snapshotListeners = /* @__PURE__ */ new Set();
			statusListeners = /* @__PURE__ */ new Set();
			es = null;
			sseErrors = 0;
			rebuildAttempts = 0;
			rebuildTimer = null;
			pollTimer = null;
			inFlight = null;
			lastSnapshot = null;
			constructor(opts) {
				this.url = opts.url;
				this.streamMode = opts.mode;
				this.esFactory = opts.eventSourceFactory ?? defaultEventSourceFactory;
				this.pollFn = opts.pollFn ?? defaultPollFn;
				this.pollActiveMs = opts.pollActiveMs ?? DEFAULT_POLL_ACTIVE_MS;
				this.pollIdleMs = opts.pollIdleMs ?? DEFAULT_POLL_IDLE_MS;
				this.visibleFn = opts.visible;
				this.setT = opts.setTimeout ?? defaultSetTimeout$2;
				this.clearT = opts.clearTimeout ?? defaultClearTimeout$2;
				this.createController = opts.createAbortController ?? defaultCreateAbortController$2;
				this.errorThreshold = opts.errorThreshold ?? DEFAULT_ERROR_THRESHOLD;
				this.backoffBaseMs = opts.backoffBaseMs ?? DEFAULT_BACKOFF_BASE_MS;
				this.backoffCapMs = opts.backoffCapMs ?? DEFAULT_BACKOFF_CAP_MS;
			}
			get mode() {
				return this.streamMode;
			}
			get status() {
				return this.currentStatus;
			}
			/** Begin streaming. One-shot: calling again (or after stop) is a no-op. */
			start() {
				if (this.started || this.stopped) return;
				this.started = true;
				if (this.streamMode === "sse") this.connectSse();
				else this.pollTick();
			}
			/**
			* Terminal teardown: clears every timer, closes the EventSource, aborts
			* the in-flight poll fetch, and drops all listeners. Idempotent; a
			* stopped stream cannot be restarted (build a fresh instance instead).
			*/
			stop() {
				if (this.stopped) return;
				this.stopped = true;
				if (this.rebuildTimer !== null) {
					this.clearT(this.rebuildTimer);
					this.rebuildTimer = null;
				}
				if (this.pollTimer !== null) {
					this.clearT(this.pollTimer);
					this.pollTimer = null;
				}
				const es = this.es;
				this.es = null;
				if (es !== null) es.close();
				const inFlight = this.inFlight;
				this.inFlight = null;
				if (inFlight !== null) inFlight.abort();
				this.snapshotListeners.clear();
				this.statusListeners.clear();
			}
			/** Subscribe to snapshots; returns the unsubscribe function. */
			onSnapshot(cb) {
				if (this.stopped) return () => {};
				this.snapshotListeners.add(cb);
				return () => {
					this.snapshotListeners.delete(cb);
				};
			}
			/**
			* Subscribe to status changes; fires synchronously with the current
			* status on subscription, then on every transition. Returns the
			* unsubscribe function.
			*/
			onStatus(cb) {
				if (this.stopped) return () => {};
				this.statusListeners.add(cb);
				cb(this.currentStatus);
				return () => {
					this.statusListeners.delete(cb);
				};
			}
			/**
			* Immediate out-of-cadence poll (poll mode only). This is the
			* visibility-resume hook: wire the document's `visibilitychange` event
			* to call this when the page becomes visible again, so "resume → fetch
			* immediately" holds without waiting for the next scheduled tick. Any
			* pending tick is cancelled first, so cadence never doubles up.
			*/
			pollNow() {
				if (!this.started || this.stopped || this.streamMode !== "poll") return;
				if (this.pollTimer !== null) {
					this.clearT(this.pollTimer);
					this.pollTimer = null;
				}
				this.pollTick();
			}
			connectSse() {
				if (this.stopped) return;
				const es = this.esFactory(this.url);
				this.es = es;
				es.addEventListener("open", () => {
					if (this.stopped || this.es !== es) return;
					this.sseErrors = 0;
					this.rebuildAttempts = 0;
					this.setStatus("open");
				});
				es.addEventListener("state", (ev) => {
					if (this.stopped || this.es !== es) return;
					if (typeof ev.data !== "string") return;
					let parsed;
					try {
						parsed = JSON.parse(ev.data);
					} catch {
						return;
					}
					if (typeof parsed !== "object" || parsed === null) return;
					this.emitSnapshot(parsed);
				});
				es.addEventListener("error", () => {
					if (this.stopped || this.es !== es) return;
					this.sseErrors += 1;
					if (es.readyState === EVENTSOURCE_CLOSED || this.sseErrors > this.errorThreshold) this.scheduleRebuild();
					else this.setStatus("connecting");
				});
			}
			/**
			* Take over from native auto-reconnect: close the instance, surface
			* `degraded`, and rebuild after a doubling delay capped at
			* backoffCapMs. Never gives up — the ladder just stays at the cap.
			*/
			scheduleRebuild() {
				const es = this.es;
				this.es = null;
				if (es !== null) es.close();
				this.setStatus("degraded");
				const delay = Math.min(this.backoffBaseMs * 2 ** this.rebuildAttempts, this.backoffCapMs);
				this.rebuildAttempts += 1;
				this.rebuildTimer = this.setT(() => {
					this.rebuildTimer = null;
					this.connectSse();
				}, delay);
			}
			hasWorkingSession() {
				const snap = this.lastSnapshot;
				if (snap === null) return false;
				return snap.board.sessions.some((s) => s.status === "working");
			}
			scheduleNextPoll() {
				if (this.stopped) return;
				const delay = this.hasWorkingSession() ? this.pollActiveMs : this.pollIdleMs;
				this.pollTimer = this.setT(() => {
					this.pollTimer = null;
					this.pollTick();
				}, delay);
			}
			pollTick() {
				if (this.stopped) return;
				if (this.visibleFn !== void 0 && !this.visibleFn()) {
					this.scheduleNextPoll();
					return;
				}
				if (this.inFlight !== null) {
					this.scheduleNextPoll();
					return;
				}
				const controller = this.createController();
				this.inFlight = controller;
				this.pollFn({ signal: controller.signal }).then((snapshot) => {
					if (this.stopped || this.inFlight !== controller) return;
					this.inFlight = null;
					this.lastSnapshot = snapshot;
					this.setStatus("open");
					this.emitSnapshot(snapshot);
					this.scheduleNextPoll();
				}, () => {
					if (this.stopped || this.inFlight !== controller) return;
					this.inFlight = null;
					this.setStatus("degraded");
					this.scheduleNextPoll();
				});
			}
			setStatus(status) {
				if (this.currentStatus === status) return;
				this.currentStatus = status;
				for (const cb of [...this.statusListeners]) cb(status);
			}
			emitSnapshot(snapshot) {
				for (const cb of [...this.snapshotListeners]) cb(snapshot);
			}
		};
		//#endregion
		//#region src/client/controller.ts
		/**
		* SidecarController — the browser half's one data controller (T2.4).
		*
		* Owns the live feed (a {@link StateStream}, default 'sse' mode on the
		* plugin's stream route) and folds it into two externally-subscribable
		* stores consumed via `useSyncExternalStore`:
		*
		* - view state: the host `StateSnapshot` mapped onto the board view models
		*   (wire `updated_at` epoch SECONDS → `updatedAtMs` epoch MILLISECONDS at
		*   this boundary, per board/logic.ts's contract), plus the composite
		*   stream health (browser stream status overrides the host-reported
		*   subscribe health — see {@link combineStreamHealth});
		* - filters: board filter state persisted to localStorage under a
		*   package-name-prefixed key; until the user touches them, the ui.*
		*   settings defaults may be adopted ({@link SidecarController.adoptConfigDefaults}).
		*
		* Pure mapping functions are exported for unit tests. Browser primitives
		* (stream, storage) are injectable so the whole controller runs under
		* plain node. No React, no slots SDK.
		*
		* @module
		*/
		/**
		* Package name, used as the localStorage key prefix and the style-tag owner
		* mark. Mirrors package.json `name` (client code cannot read package.json at
		* runtime); pinned against it by test/client-integration.test.ts.
		*/
		const PLUGIN_ID = "@shendeguize/dsh-agent-sidecar";
		/** localStorage key for the persisted board filters. */
		const FILTERS_STORAGE_KEY = `${PLUGIN_ID}:board-filters`;
		/** Pre-first-snapshot state: probing daemon, unknown health, empty board. */
		function initialViewState() {
			return {
				daemonState: "probe",
				lastPing: null,
				daemonDetail: void 0,
				streamHealth: "unknown",
				streamStatus: "connecting",
				lastReconcileAtMs: null,
				sessions: [],
				injectCapability: false,
				hasSnapshot: false
			};
		}
		/**
		* Wire sessions → board card view models. The one place the epoch-seconds
		* `updated_at` becomes the epoch-milliseconds `updatedAtMs` (board/logic.ts
		* consumes milliseconds everywhere).
		*/
		function mapSessions(sessions) {
			return sessions.map((session) => ({
				agent: session.agent,
				sessionId: session.session_id,
				status: session.status,
				title: session.title,
				project: session.project,
				updatedAtMs: session.updated_at * 1e3,
				lastEvent: session.last_event === null ? null : {
					kind: session.last_event.kind,
					text: session.last_event.text
				},
				gap: session.gap
			}));
		}
		/** Daemon badge hover detail from the last ping ("pid 123 · v0.6.0"). */
		function daemonDetailOf(ping) {
			if (ping === null) return void 0;
			return `pid ${ping.pid} · v${ping.version}`;
		}
		/**
		* Composite stream health for the UI:
		* - before the first snapshot nothing is known → 'unknown';
		* - a degraded BROWSER stream overrides (the host may still be healthy,
		*   but what this page shows is stale);
		* - otherwise the host-reported daemon-subscribe health stands ('connecting'
		*   during a native EventSource reconnect keeps the last host verdict —
		*   the reconcile timestamp already conveys staleness).
		*/
		function combineStreamHealth(hostHealth, browserStatus, hasSnapshot) {
			if (!hasSnapshot || hostHealth === null) return "unknown";
			if (browserStatus === "degraded") return "degraded";
			return hostHealth;
		}
		/** Full snapshot → view state fold (pure; exported for tests). */
		function mapSnapshot(snapshot, browserStatus) {
			return {
				daemonState: snapshot.daemon.state,
				lastPing: snapshot.daemon.lastPing,
				daemonDetail: daemonDetailOf(snapshot.daemon.lastPing),
				streamHealth: combineStreamHealth(snapshot.board.streamHealth, browserStatus, true),
				streamStatus: browserStatus,
				lastReconcileAtMs: snapshot.board.lastReconcileAt,
				sessions: mapSessions(snapshot.board.sessions),
				injectCapability: snapshot.capabilities.inject,
				hasSnapshot: true
			};
		}
		/** Resolve the real localStorage; some privacy modes throw on access. */
		function defaultStorage() {
			try {
				return globalThis.localStorage ?? null;
			} catch {
				return null;
			}
		}
		/**
		* Parse + validate persisted filters; anything malformed reads as absent.
		* The optional statusFilter (UX-01) survives only as one of its two legal
		* values — an unrecognized token is dropped, not the whole record.
		*/
		function readStoredFilters(storage) {
			if (storage === null) return null;
			try {
				const raw = storage.getItem(FILTERS_STORAGE_KEY);
				if (raw === null) return null;
				const parsed = JSON.parse(raw);
				if (typeof parsed !== "object" || parsed === null) return null;
				const candidate = parsed;
				if (typeof candidate.timeWindowHours !== "number" || !Number.isFinite(candidate.timeWindowHours) || candidate.timeWindowHours <= 0 || typeof candidate.showDead !== "boolean") return null;
				const filters = {
					timeWindowHours: candidate.timeWindowHours,
					showDead: candidate.showDead
				};
				if (candidate.statusFilter === "working" || candidate.statusFilter === "waiting") filters.statusFilter = candidate.statusFilter;
				return filters;
			} catch {
				return null;
			}
		}
		const defaultVisible = () => typeof document === "undefined" || !document.hidden;
		/**
		* One instance per plugin apply. `start()` wires the stream, `stop()` is
		* terminal (the underlying StateStream cannot restart — a re-apply builds
		* a fresh controller).
		*/
		var SidecarController = class {
			stream;
			storage;
			fetchFn;
			listeners = /* @__PURE__ */ new Set();
			state = initialViewState();
			filters;
			/** True once filters came from storage or a user gesture (config defaults then stop adopting). */
			filtersTouched;
			lastHostHealth = null;
			started = false;
			constructor(opts = {}) {
				this.storage = opts.storage === void 0 ? defaultStorage() : opts.storage;
				this.fetchFn = opts.fetchStateFn ?? fetchState;
				this.stream = opts.stream ?? new StateStream({
					url: STREAM_PATH,
					mode: "sse",
					visible: opts.visible ?? defaultVisible
				});
				const stored = readStoredFilters(this.storage);
				this.filters = stored ?? {
					timeWindowHours: 48,
					showDead: false
				};
				this.filtersTouched = stored !== null;
			}
			/** Wire stream listeners and begin streaming (idempotent). */
			start() {
				if (this.started) return;
				this.started = true;
				this.stream.onSnapshot((snapshot) => {
					this.applySnapshot(snapshot);
				});
				this.stream.onStatus((status) => {
					this.applyStatus(status);
				});
				this.stream.start();
			}
			/** Terminal teardown of the live feed. */
			stop() {
				this.stream.stop();
			}
			/** Forward the visibility resume to the stream (poll-mode immediate fetch). */
			pollNow() {
				this.stream.pollNow();
			}
			/** Change notifications for BOTH stores (state and filters). */
			subscribe(listener) {
				this.listeners.add(listener);
				return () => {
					this.listeners.delete(listener);
				};
			}
			/** Stable-reference view state (uSES getSnapshot source). */
			getState() {
				return this.state;
			}
			/** Stable-reference filters (uSES getSnapshot source). */
			getFilters() {
				return this.filters;
			}
			/** User filter change: persist (package-prefixed key) and notify. */
			setFilters(next) {
				this.filters = { ...next };
				this.filtersTouched = true;
				if (this.storage !== null) try {
					this.storage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(this.filters));
				} catch {}
				this.notify();
			}
			/**
			* Adopt ui.* settings defaults as the filter values — only while the user
			* has never touched the filters (no stored value, no gesture). Not
			* persisted: an untouched board keeps following the settings defaults.
			*/
			adoptConfigDefaults(ui) {
				if (this.filtersTouched) return;
				if (this.filters.timeWindowHours === ui.timeWindowHours && this.filters.showDead === ui.showDead) return;
				this.filters = {
					timeWindowHours: ui.timeWindowHours,
					showDead: ui.showDead
				};
				this.notify();
			}
			/**
			* Manual refresh (board's refresh button): one out-of-band snapshot
			* pull. Resolves true when the snapshot applied, false on failure so
			* the button can surface honest feedback (UX-07) — never rejects; the
			* stream (and its status surface) remains the health authority.
			*/
			async refresh() {
				try {
					const snapshot = await this.fetchFn({});
					this.applySnapshot(snapshot);
					return true;
				} catch (err) {
					console.error("agent-sidecar: manual refresh failed", err);
					return false;
				}
			}
			applySnapshot(snapshot) {
				this.lastHostHealth = snapshot.board.streamHealth;
				this.state = mapSnapshot(snapshot, this.stream.status);
				this.notify();
			}
			applyStatus(status) {
				if (status === this.state.streamStatus) return;
				this.state = {
					...this.state,
					streamStatus: status,
					streamHealth: combineStreamHealth(this.lastHostHealth, status, this.state.hasSnapshot)
				};
				this.notify();
			}
			notify() {
				for (const listener of [...this.listeners]) listener();
			}
		};
		//#endregion
		//#region src/client/inject-glue.ts
		/**
		* Inject integration glue (S5 wiring, T4.9): everything that stands between
		* the presentational {@link InjectPanel} (T4.5, src/client/inject/) and the
		* rest of the client half, kept pure/injectable so it unit-tests under
		* plain node (same posture as controller.ts / settings-glue.ts).
		*
		* - {@link prepareEnvelope} / {@link executeEnvelope}: build the M2 action
		*   wire bodies of the host dispatcher (src/routes.ts `handleAction`:
		*   `{type:'inject.prepare'|'inject.execute', ...}` with the phase fields
		*   at the top level). The body types are api.ts's `ActionEnvelope` union
		*   members — the data layer owns the wire mirror, so `postAction` is
		*   typed end-to-end and this module casts nothing on the way out.
		* - {@link createInjectActions}: the panel's `onPrepare`/`onExecute` props.
		*   Execute posts with {@link EXECUTE_TIMEOUT_MS} (path two's server-side
		*   budget outlives the 15s data-layer default; prepare keeps the default,
		*   it has no delivery side effect).
		*   Per the panel contract, transport failures resolve AS VALUES: the data
		*   layer's normalized ApiError satisfies the panel's structural
		*   {@link ApiErrorLike} and is returned, never thrown (anything that is
		*   not an ApiError is re-thrown and lands in the panel's defensive catch:
		*   retryable notice for prepare, terminal unknown for execute — S6).
		*   A delivered execute fires the optional `onDelivered` hook so the owner
		*   can pull one fresh snapshot; failed/unknown never do.
		* - {@link findInjectTarget}: board card selection → panel target (the
		*   design §5.1 view-3 target summary comes from the selected SessionView
		*   as mapped into the controller's card VMs). A session that has left the
		*   snapshot resolves to null — the panel then shows its no-target hint
		*   instead of injecting into a stale target.
		*
		* @module
		*/
		/**
		* Execute-path HTTP deadline. Path two's server-side budget is the 30s CLI
		* timeout + 5s hard-kill buffer (host: send-cli.ts `DEFAULT_SEND_TIMEOUT_MS`
		* + `HARD_TIMEOUT_BUFFER_MS` = 35s worst case); a client deadline below
		* that fabricates a terminal 'unknown' out of every slow-but-honest
		* delivery receipt (M2 review F-3). 45s = server worst case + 10s margin;
		* the mirror relation is pinned by test against the host constants.
		*/
		const EXECUTE_TIMEOUT_MS = 45e3;
		/** Panel prepare request → the host prepare wire body. */
		function prepareEnvelope(req) {
			return {
				type: "inject.prepare",
				target: {
					agent: req.target.agent,
					sessionId: req.target.sessionId
				},
				mode: req.mode,
				message: req.message
			};
		}
		/** Panel execute request → the host execute wire body. */
		function executeEnvelope(req) {
			return {
				type: "inject.execute",
				requestId: req.requestId,
				confirmToken: req.confirmToken,
				message: req.message
			};
		}
		/** Default transport: api.ts postAction (typed directly; the wire bodies
		* are ActionEnvelope union members, so no cast stands between the panel
		* and the dispatcher). */
		const defaultPost = (body, opts) => postAction(body, opts);
		/**
		* Build the panel's `onPrepare`/`onExecute` callbacks over the action
		* transport. Error posture per the panel contract: ApiError resolves as a
		* value (the panel classifies http-kind as a server vocabulary verdict and
		* everything else as a transport failure); non-ApiError throws propagate.
		* @param deps - transport and delivery-hook seams.
		* @returns the callbacks, ready to spread onto the panel props.
		*/
		function createInjectActions(deps = {}) {
			const post = deps.post ?? defaultPost;
			return {
				async onPrepare(req) {
					try {
						return await post(prepareEnvelope(req));
					} catch (err) {
						if (isApiError(err)) return err;
						throw err;
					}
				},
				async onExecute(req) {
					let result;
					try {
						result = await post(executeEnvelope(req), { timeoutMs: EXECUTE_TIMEOUT_MS });
					} catch (err) {
						if (isApiError(err)) return err;
						throw err;
					}
					if (result.outcome === "delivered") deps.onDelivered?.();
					return result;
				}
			};
		}
		//#endregion
		//#region \0dsh-css:src/client/theme/agsc.module.css.mjs
		const css$11 = ".V4kgfa_root{--agsc-accent:var(--dsw-alias-brand-primary);--agsc-bg:var(--dsw-alias-bg-layer-1);--agsc-bg-raised:var(--dsw-alias-bg-layer-3);--agsc-fg:var(--dsw-alias-label-primary);--agsc-fg-secondary:var(--dsw-alias-label-secondary);--agsc-fg-dimmed:var(--dsw-alias-label-dimmed);--agsc-border:var(--dsw-alias-border-l1);--agsc-border-strong:var(--dsw-alias-border-l2);--agsc-ok:var(--dsw-alias-state-success-primary);--agsc-warn:var(--dsw-alias-state-warn-primary);--agsc-err:var(--dsw-alias-state-error-primary);--agsc-radius-card:12px;--agsc-radius-control:8px;--agsc-shadow-card:var(--dsw-shadow-lv2);--agsc-font-mono:var(--ds-font-family-code);color:var(--agsc-fg);font-family:inherit}";
		const tagId$11 = "@shendeguize/dsh-agent-sidecar/src/client/theme/agsc.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$11, css$11);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$11) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$11;
				created = true;
			}
			tag.textContent = css$11;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var agsc_module_css_default = { "root": "V4kgfa_root" };
		//#endregion
		//#region src/client/theme/parts.ts
		/** Stable owner value for Agent Sidecar skin anchors. */
		const PLUGIN_DOM_ID = "agent-sidecar";
		Object.freeze([
			"board",
			"board-toolbar",
			"board-card",
			"project-view",
			"detail",
			"timeline",
			"inject-panel",
			"analysis-panel",
			"dsh-tools",
			"footer-widget",
			"sidebar-entry",
			"settings-card",
			"overlay",
			"sidebar-tab"
		]);
		function requireThemeClass(name) {
			const className = agsc_module_css_default[name];
			if (className === void 0 || className.length === 0) throw new Error(`Agent Sidecar theme is missing its ${name} class`);
			return className;
		}
		const rootClassName = requireThemeClass("root");
		/**
		* Returns the complete, spreadable contract for one mounted surface.
		* This keeps hashed CSS classes internal while exposing stable DSH anchors.
		*/
		function surfaceProps(part, className) {
			const localClassName = className?.trim();
			return {
				className: localClassName ? `${rootClassName} ${localClassName}` : rootClassName,
				"data-dsh-plugin": PLUGIN_DOM_ID,
				"data-dsh-part": part
			};
		}
		//#endregion
		//#region \0dsh-css:src/client/board/board.module.css.mjs
		const css$10 = ".PLm6rG_root{box-sizing:border-box;height:100%;min-height:420px;color:var(--agsc-fg);background:var(--agsc-bg);flex-direction:column;gap:12px;padding:16px 20px;display:flex;overflow-y:auto}.PLm6rG_topbar{flex-wrap:wrap;align-items:center;gap:10px;display:flex}.PLm6rG_title{color:var(--agsc-fg);margin-right:2px;font-size:15px;font-weight:650}.PLm6rG_dot{background:var(--dsw-alias-label-tertiary);border-radius:50%;flex:none;width:8px;height:8px}.PLm6rG_dot[data-tone=neutral]{background:var(--dsw-alias-label-tertiary)}.PLm6rG_dot[data-tone=muted]{background:var(--agsc-fg-dimmed)}.PLm6rG_countBadge{white-space:nowrap}.PLm6rG_countTotal{color:var(--agsc-fg-secondary);white-space:nowrap;font-size:12px}.PLm6rG_spacer{flex:1}.PLm6rG_control{color:var(--agsc-fg-secondary);white-space:nowrap;align-items:center;gap:5px;font-size:12px;display:inline-flex}.PLm6rG_select{height:28px;color:var(--agsc-fg);background:var(--agsc-bg-raised);border:1px solid var(--agsc-border-strong);border-radius:14px;padding:0 10px;font-family:inherit;font-size:12px}.PLm6rG_checkbox{accent-color:var(--agsc-accent);margin:0}.PLm6rG_banner{border-radius:var(--agsc-radius-control);border:1px solid var(--agsc-border);padding:6px 10px;font-size:12px;line-height:18px}.PLm6rG_banner[data-tone=warn]{color:var(--agsc-warn);background:var(--dsw-alias-state-warn-tertiary);border-color:var(--dsw-alias-state-warn-secondary)}.PLm6rG_banner[data-tone=danger]{color:var(--agsc-err);background:var(--dsw-alias-state-error-secondary);border-color:var(--dsw-alias-state-error-secondary)}.PLm6rG_bannerDismiss{color:inherit;margin-left:10px;text-decoration:underline}.PLm6rG_empty{text-align:center;flex-direction:column;flex:1;justify-content:center;align-items:center;gap:6px;min-height:180px;padding:24px;display:flex}.PLm6rG_emptyTitle{color:var(--agsc-fg);font-size:14px;font-weight:600}.PLm6rG_emptyHint{max-width:420px;color:var(--agsc-fg-secondary);font-size:12px;line-height:20px}.PLm6rG_group{flex-direction:column;gap:8px;display:flex}.PLm6rG_groupHead{text-align:left;cursor:pointer;background:0 0;border:none;align-items:baseline;gap:8px;width:100%;min-width:0;padding:0;font-family:inherit;display:flex}.PLm6rG_chevron{color:var(--agsc-fg-dimmed);flex:none;font-size:10px}.PLm6rG_groupAttention{color:var(--agsc-warn);flex:none;font-size:11px}.PLm6rG_showMore{align-self:flex-start}.PLm6rG_groupName{color:var(--agsc-fg);text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:600;overflow:hidden}.PLm6rG_groupCount{color:var(--agsc-fg-secondary);background:var(--dsw-alias-bg-layer-2);border-radius:999px;flex:none;padding:0 8px;font-size:11px;line-height:18px}.PLm6rG_grid{grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px;display:grid}.PLm6rG_card{border-radius:var(--agsc-radius-card);border:1px solid var(--agsc-border-strong);background:var(--agsc-bg-raised);flex-direction:column;gap:6px;min-width:0;padding:10px 12px;display:flex}.PLm6rG_card:hover{border-color:var(--dsw-alias-label-dimmed);background:var(--dsw-alias-interactive-bg-hover)}.PLm6rG_cardOpen{border-radius:var(--agsc-radius-control);width:100%;min-width:0;color:inherit;font:inherit;text-align:left;cursor:pointer;background:0 0;border:0;flex-direction:column;gap:6px;padding:0;display:flex}.PLm6rG_cardOpen:focus-visible{outline:2px solid var(--agsc-accent);outline-offset:2px}.PLm6rG_cardHead{justify-content:space-between;align-items:center;gap:8px;min-width:0;display:flex}.PLm6rG_agent{text-overflow:ellipsis;white-space:nowrap;min-width:0;color:var(--agsc-fg);align-items:center;gap:5px;font-size:12px;font-weight:600;display:inline-flex;overflow:hidden}.PLm6rG_glyph{color:var(--agsc-accent);flex:none}.PLm6rG_statusPill{max-width:100%}.PLm6rG_attention{color:var(--agsc-warn);font-size:11px}.PLm6rG_attention[data-kind=gap]{color:var(--agsc-err)}.PLm6rG_cardTitle{color:var(--agsc-fg);text-overflow:ellipsis;white-space:nowrap;font-size:13px;overflow:hidden}.PLm6rG_cardId{max-width:100%;font-size:11px;font-family:var(--agsc-font-mono);color:var(--agsc-fg-secondary);text-overflow:ellipsis;white-space:nowrap;justify-content:flex-start;align-self:flex-start;overflow:hidden}.PLm6rG_copied{color:var(--agsc-ok);margin-left:6px;font-family:inherit}.PLm6rG_cardEvent{color:var(--agsc-fg-secondary);text-overflow:ellipsis;white-space:nowrap;font-size:12px;overflow:hidden}.PLm6rG_cardTime{color:var(--agsc-fg-secondary);font-size:11px}";
		const tagId$10 = "@shendeguize/dsh-agent-sidecar/src/client/board/board.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$10, css$10);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$10) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$10;
				created = true;
			}
			tag.textContent = css$10;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var board_module_css_default = {
			"agent": "PLm6rG_agent",
			"attention": "PLm6rG_attention",
			"banner": "PLm6rG_banner",
			"bannerDismiss": "PLm6rG_bannerDismiss",
			"card": "PLm6rG_card",
			"cardEvent": "PLm6rG_cardEvent",
			"cardHead": "PLm6rG_cardHead",
			"cardId": "PLm6rG_cardId",
			"cardOpen": "PLm6rG_cardOpen",
			"cardTime": "PLm6rG_cardTime",
			"cardTitle": "PLm6rG_cardTitle",
			"checkbox": "PLm6rG_checkbox",
			"chevron": "PLm6rG_chevron",
			"control": "PLm6rG_control",
			"copied": "PLm6rG_copied",
			"countBadge": "PLm6rG_countBadge",
			"countTotal": "PLm6rG_countTotal",
			"dot": "PLm6rG_dot",
			"empty": "PLm6rG_empty",
			"emptyHint": "PLm6rG_emptyHint",
			"emptyTitle": "PLm6rG_emptyTitle",
			"glyph": "PLm6rG_glyph",
			"grid": "PLm6rG_grid",
			"group": "PLm6rG_group",
			"groupAttention": "PLm6rG_groupAttention",
			"groupCount": "PLm6rG_groupCount",
			"groupHead": "PLm6rG_groupHead",
			"groupName": "PLm6rG_groupName",
			"root": "PLm6rG_root",
			"select": "PLm6rG_select",
			"showMore": "PLm6rG_showMore",
			"spacer": "PLm6rG_spacer",
			"statusPill": "PLm6rG_statusPill",
			"title": "PLm6rG_title",
			"topbar": "PLm6rG_topbar"
		};
		//#endregion
		//#region src/client/board/Board.tsx
		/**
		* Cross-agent session board (design §5.1 view 1).
		*
		* Presentation-only: no data fetching, no api/sse imports. Everything
		* arrives through props already shaped as the view models of `logic.ts`;
		* the integration layer (T2.4) owns state, transport, and the mapping
		* from the host wire types (epoch-seconds → epoch-ms conversion included).
		*
		* Interaction surface handed back to the owner:
		* - `onFiltersChange` — time-window select / show-dead checkbox / the
		*   top-bar status-filter badges (controlled, UX-01);
		* - `onRefresh`      — manual snapshot pull button; a Promise<boolean>
		*   return drives the in-flight/failure feedback (UX-07);
		* - `onSelectSession` — card click, pass-through for the M3 detail view.
		*
		* Local UI state (deliberately NOT lifted into the controller stores):
		* group collapse and per-group truncation (UX-02) are ephemeral view
		* concerns — useState here, reset on tab remount.
		*/
		/** Time-window choices offered by the top bar (hours). */
		const TIME_WINDOW_OPTIONS = [
			6,
			12,
			24,
			48,
			168
		];
		function sessionDotState$1(status) {
			if (status === "working") return "ongoing";
			if (status === "waiting") return "warning";
			return null;
		}
		function SessionCard(props) {
			const { card, onSelect } = props;
			const [copied, setCopied] = (0, react.useState)(false);
			const copyTimerRef = (0, react.useRef)(null);
			const copyAliveRef = (0, react.useRef)(true);
			const dotState = sessionDotState$1(card.badge.status);
			(0, react.useEffect)(() => {
				copyAliveRef.current = true;
				return () => {
					copyAliveRef.current = false;
					if (copyTimerRef.current !== null) {
						clearTimeout(copyTimerRef.current);
						copyTimerRef.current = null;
					}
				};
			}, []);
			const onCopyId = () => {
				const clipboard = typeof navigator === "undefined" ? void 0 : navigator.clipboard;
				if (clipboard === void 0) return;
				clipboard.writeText(card.sessionId).then(() => {
					if (!copyAliveRef.current) return;
					if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
					setCopied(true);
					copyTimerRef.current = setTimeout(() => {
						copyTimerRef.current = null;
						setCopied(false);
					}, 2e3);
				}, () => {});
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("article", {
				...surfaceProps("board-card", board_module_css_default["card"]),
				"data-testid": "agent-sidecar-card",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
						type: "button",
						className: board_module_css_default["cardOpen"],
						onClick: () => onSelect(card.sessionId),
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: board_module_css_default["cardHead"],
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
								className: board_module_css_default["agent"],
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: board_module_css_default["glyph"],
									"aria-hidden": true,
									children: card.glyph
								}), card.agent]
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								title: card.hoverTitle,
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
									className: board_module_css_default["statusPill"],
									children: [
										dotState === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
											className: board_module_css_default["dot"],
											"data-tone": card.badge.tone
										}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
											state: dotState,
											size: 8
										}),
										card.badge.label,
										card.badge.attention !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
											className: board_module_css_default["attention"],
											"data-kind": card.badge.attention,
											children: card.badge.attentionLabel
										})
									]
								})
							})]
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: board_module_css_default["cardTitle"],
							title: card.title,
							children: card.title.trim() === "" ? BOARD_STRINGS.card.untitled : card.title
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Button, {
						size: "sm",
						variant: "ghost",
						className: board_module_css_default["cardId"],
						title: `${card.sessionId}\n${BOARD_STRINGS.card.copyId}`,
						onClick: onCopyId,
						"data-testid": "agent-sidecar-card-id",
						children: [card.shortId, copied && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: board_module_css_default["copied"],
							role: "status",
							children: BOARD_STRINGS.card.copied
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: board_module_css_default["cardEvent"],
						children: card.lastEvent === null ? BOARD_STRINGS.card.noEvent : `${card.lastEvent.kind} · ${card.lastEvent.text}`
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: board_module_css_default["cardTime"],
						children: card.relativeTime
					})
				]
			});
		}
		function ProjectGroup(props) {
			const { group, onSelect } = props;
			const [collapsed, setCollapsed] = (0, react.useState)(false);
			const [expanded, setExpanded] = (0, react.useState)(false);
			const { shown, hiddenCount } = sliceCardsForDisplay(group.cards, 20, expanded);
			const waitingInGroup = group.cards.filter((card) => card.badge.status === "waiting").length;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: board_module_css_default["group"],
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
					type: "button",
					className: board_module_css_default["groupHead"],
					"aria-expanded": !collapsed,
					title: collapsed ? BOARD_STRINGS.group.expandTitle : BOARD_STRINGS.group.collapseTitle,
					onClick: () => {
						setCollapsed(!collapsed);
					},
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: board_module_css_default["chevron"],
							"aria-hidden": true,
							children: collapsed ? "▸" : "▾"
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: board_module_css_default["groupName"],
							title: group.fullPath === "" ? void 0 : group.fullPath,
							children: group.label
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: board_module_css_default["groupCount"],
							children: formatTemplate$2(BOARD_STRINGS.groupCount, { n: group.cards.length })
						}),
						collapsed && waitingInGroup > 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: board_module_css_default["groupAttention"],
							children: formatTemplate$2(BOARD_STRINGS.topbar.countWaiting, { n: waitingInGroup })
						})
					]
				}), !collapsed && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: board_module_css_default["grid"],
						children: shown.map((card) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(SessionCard, {
							card,
							onSelect
						}, `${card.agent}:${card.sessionId}`))
					}),
					hiddenCount > 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
						size: "sm",
						variant: "outline",
						className: board_module_css_default["showMore"],
						onClick: () => {
							setExpanded(true);
						},
						children: formatTemplate$2(BOARD_STRINGS.group.showAll, { n: group.cards.length })
					}),
					expanded && group.cards.length > 20 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
						size: "sm",
						variant: "outline",
						className: board_module_css_default["showMore"],
						onClick: () => {
							setExpanded(false);
						},
						children: formatTemplate$2(BOARD_STRINGS.group.showLess, { n: 20 })
					})
				] })]
			});
		}
		/** The board view. Pure render of `buildBoardViewModel` over the props. */
		function Board(props) {
			const nowMs = props.nowMs ?? Date.now();
			const vm = buildBoardViewModel({
				sessions: props.sessions,
				filters: props.filters,
				daemonState: props.daemonState,
				streamHealth: props.streamHealth,
				lastReconcileAtMs: props.lastReconcileAtMs,
				nowMs
			});
			const windowOptions = TIME_WINDOW_OPTIONS.includes(props.filters.timeWindowHours) ? TIME_WINDOW_OPTIONS : [...TIME_WINDOW_OPTIONS, props.filters.timeWindowHours].sort((a, b) => a - b);
			const [refreshing, setRefreshing] = (0, react.useState)(false);
			const [refreshFailed, setRefreshFailed] = (0, react.useState)(false);
			const onRefreshClick = () => {
				if (refreshing) return;
				setRefreshFailed(false);
				const result = props.onRefresh();
				if (result instanceof Promise) {
					setRefreshing(true);
					result.then((ok) => {
						setRefreshFailed(!ok);
					}).catch(() => {
						setRefreshFailed(true);
					}).finally(() => {
						setRefreshing(false);
					});
				}
			};
			const toggleStatusFilter = (status) => {
				const next = { ...props.filters };
				if (next.statusFilter === status) delete next.statusFilter;
				else next.statusFilter = status;
				props.onFiltersChange(next);
			};
			const statusBadgeTitle = (status) => props.filters.statusFilter === status ? BOARD_STRINGS.topbar.clearStatusFilterTitle : formatTemplate$2(BOARD_STRINGS.topbar.filterByStatusTitle, { label: BOARD_STRINGS.status[status] });
			const daemonDotState = props.daemonState === "failed" ? "error" : props.daemonState === "defer" || props.daemonState === "backoff" ? "warning" : props.daemonState === "adopted" || props.daemonState === "hosted" ? "done" : "ongoing";
			const streamDotState = props.streamHealth === "ok" ? "done" : props.streamHealth === "degraded" ? "warning" : "ongoing";
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				...surfaceProps("board", board_module_css_default["root"]),
				"data-testid": "agent-sidecar-board",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("header", {
						...surfaceProps("board-toolbar", board_module_css_default["topbar"]),
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: board_module_css_default["title"],
								children: BOARD_STRINGS.topbar.title
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								title: props.daemonDetail,
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Pill, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
									state: daemonDotState,
									size: 8
								}), vm.daemonBadge.label] })
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Pill, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
								state: streamDotState,
								size: 8
							}), vm.streamLabel] }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
								className: board_module_css_default["countBadge"],
								active: props.filters.statusFilter === "working",
								"aria-pressed": props.filters.statusFilter === "working",
								title: statusBadgeTitle("working"),
								onClick: () => {
									toggleStatusFilter("working");
								},
								"data-testid": "agent-sidecar-count-working",
								children: [vm.workingCount > 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
									state: "ongoing",
									size: 8
								}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: board_module_css_default["dot"],
									"data-tone": "neutral"
								}), formatTemplate$2(BOARD_STRINGS.topbar.countWorking, { n: vm.workingCount })]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
								className: board_module_css_default["countBadge"],
								active: props.filters.statusFilter === "waiting",
								"aria-pressed": props.filters.statusFilter === "waiting",
								title: statusBadgeTitle("waiting"),
								onClick: () => {
									toggleStatusFilter("waiting");
								},
								"data-testid": "agent-sidecar-count-waiting",
								children: [vm.waitingCount > 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
									state: "warning",
									size: 8
								}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: board_module_css_default["dot"],
									"data-tone": "neutral"
								}), formatTemplate$2(BOARD_STRINGS.topbar.countWaiting, { n: vm.waitingCount })]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: board_module_css_default["countTotal"],
								"data-testid": "agent-sidecar-count-total",
								children: formatTemplate$2(BOARD_STRINGS.topbar.countTotal, { n: vm.totalCount })
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { className: board_module_css_default["spacer"] }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("label", {
								className: board_module_css_default["control"],
								children: [BOARD_STRINGS.topbar.timeWindow, /* @__PURE__ */ (0, react_jsx_runtime.jsx)("select", {
									className: board_module_css_default["select"],
									value: String(props.filters.timeWindowHours),
									onChange: (ev) => props.onFiltersChange({
										...props.filters,
										timeWindowHours: Number(ev.target.value)
									}),
									children: windowOptions.map((hours) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)("option", {
										value: String(hours),
										children: timeWindowLabel(hours)
									}, hours))
								})]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("label", {
								className: board_module_css_default["control"],
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
									type: "checkbox",
									className: board_module_css_default["checkbox"],
									checked: props.filters.showDead,
									onChange: (ev) => props.onFiltersChange({
										...props.filters,
										showDead: ev.target.checked
									})
								}), BOARD_STRINGS.topbar.showDead]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
								size: "sm",
								variant: "toolbar",
								title: BOARD_STRINGS.topbar.refreshTitle,
								disabled: refreshing,
								onClick: onRefreshClick,
								children: refreshing ? BOARD_STRINGS.topbar.refreshing : BOARD_STRINGS.topbar.refresh
							})
						]
					}),
					refreshFailed && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: board_module_css_default["banner"],
						"data-tone": "warn",
						role: "status",
						children: [BOARD_STRINGS.topbar.refreshFailed, /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
							size: "sm",
							variant: "ghost",
							className: board_module_css_default["bannerDismiss"],
							onClick: () => {
								setRefreshFailed(false);
							},
							children: BOARD_STRINGS.topbar.dismiss
						})]
					}),
					vm.banner !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: board_module_css_default["banner"],
						"data-tone": vm.banner.tone,
						role: "status",
						children: vm.banner.text
					}),
					vm.emptyState !== null ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: board_module_css_default["empty"],
						"data-kind": vm.emptyState.kind,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: board_module_css_default["emptyTitle"],
							children: vm.emptyState.title
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: board_module_css_default["emptyHint"],
							children: vm.emptyState.hint
						})]
					}) : vm.groups.map((group) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ProjectGroup, {
						group,
						onSelect: props.onSelectSession
					}, group.key === "" ? "\0unknown" : group.key))
				]
			});
		}
		//#endregion
		//#region src/client/board/project-view-logic.ts
		/**
		* Pure view-model logic for the cross-agent project correlation view
		* (design §4.e.2 / §5.1 M3, T5.5). No React, no I/O, no data-layer
		* imports — plain values in, plain values out, unit-testable in a bare
		* node environment (same discipline as `logic.ts`).
		*
		* Wire contract (hand-written mirror, per module-ownership rules): the
		* input types below mirror the host's `GET <prefix>/projects` response —
		* `{groups: ProjectGroup[]}` where `ProjectGroup` is fusion.ts
		* `getProjectGroups()` output: `project` (normalized path, '' when
		* unknown), `agents` (distinct, sorted), `sessions` (UnifiedSession[],
		* camelCase, **epoch milliseconds** `lastActivityAt`, most recent first)
		* and the group-level `lastActivityAt`. Only the fields this view renders
		* are mirrored; extra wire fields are structurally ignored.
		*
		* Reuse contract (T2.2): tone/glyph/relative-time/label tools are imported
		* from `./logic.ts`, so both board views speak the same visual language.
		* Project-only copy is exposed through the dynamic
		* {@link PROJECT_VIEW_STRINGS} locale facade.
		*
		* @module
		*/
		const PROJECT_VIEW_STRINGS = createLocaleView({
			title: "project.title",
			summary: "project.summary",
			crossAgent: "project.crossAgent",
			sessionCount: "project.sessionCount",
			lastActive: "project.lastActive",
			liveChip: "project.liveChip",
			untitled: "project.untitled",
			showAllSessions: "project.showAllSessions",
			showLessSessions: "project.showLessSessions",
			empty: {
				title: "project.empty.title",
				hint: "project.empty.hint"
			},
			loading: "project.loading",
			errorTitle: "project.errorTitle"
		});
		const KEY_SEP$1 = "\0";
		/**
		* Group-key normalization, aligned with fusion's correlation key: trim,
		* strip trailing slashes (keeping root '/'), whitespace-only → '' (the
		* unknown bucket). The host already normalizes; this re-run makes the
		* view robust to hand-fed or merged inputs.
		*/
		function normalizeProjectKey(project) {
			const trimmed = project.trim();
			if (trimmed === "") return "";
			if (trimmed.length > 1 && trimmed.endsWith("/")) {
				const stripped = trimmed.replace(/\/+$/, "");
				return stripped === "" ? "/" : stripped;
			}
			return trimmed;
		}
		/**
		* Distinct participating agents of a session list, sorted by name. This
		* is derived from the sessions themselves (not the wire `agents` field)
		* so the badge row can never disagree with the lanes actually rendered —
		* notably after two wire groups merge under one normalized key.
		*/
		function deriveAgentBadges(sessions) {
			const names = /* @__PURE__ */ new Set();
			for (const session of sessions) names.add(session.agent);
			return [...names].sort().map((agent) => ({
				agent,
				glyph: agentGlyph$1(agent)
			}));
		}
		/**
		* Session ordering inside a lane, mirroring the board's card order so
		* both views read the same way: status rank (working first, dead last),
		* then recency, then id as the deterministic tiebreak.
		*/
		function compareProjectSessions(a, b) {
			const rankDelta = statusRank(normalizeStatus(a.status)) - statusRank(normalizeStatus(b.status));
			if (rankDelta !== 0) return rankDelta;
			if (a.lastActivityAt !== b.lastActivityAt) return b.lastActivityAt - a.lastActivityAt;
			return a.sessionId.localeCompare(b.sessionId);
		}
		function deriveSession(session, nowMs) {
			const live = session.live === true;
			const gap = session.gap === true;
			return {
				agent: session.agent,
				sessionId: session.sessionId,
				status: session.status,
				title: session.title,
				lastActivityAt: session.lastActivityAt,
				live,
				gap,
				badge: deriveBadge(session.status, gap),
				glyph: agentGlyph$1(session.agent),
				shortId: abbreviateSessionId(session.sessionId),
				relativeTime: formatRelativeTime$1(session.lastActivityAt, nowMs),
				displayTitle: session.title.trim() === "" ? PROJECT_VIEW_STRINGS.untitled : session.title
			};
		}
		/**
		* Split a group's sessions into per-agent lanes. Lanes are sorted by
		* agent name (matching the header badge order); sessions inside a lane
		* follow {@link compareProjectSessions}.
		*/
		function buildAgentLanes(sessions, nowMs) {
			const byAgent = /* @__PURE__ */ new Map();
			for (const session of sessions) {
				const lane = byAgent.get(session.agent);
				if (lane === void 0) byAgent.set(session.agent, [session]);
				else lane.push(session);
			}
			return [...byAgent.keys()].sort().map((agent) => {
				const members = byAgent.get(agent) ?? [];
				members.sort(compareProjectSessions);
				return {
					agent,
					glyph: agentGlyph$1(agent),
					sessions: members.map((session) => deriveSession(session, nowMs))
				};
			});
		}
		/**
		* normalize/merge → derive → sort; the one call project-view.tsx renders
		* from.
		*
		* Normalization: groups collapsing onto the same normalized key are
		* merged; duplicate sessions (same agent + sessionId) within a merged
		* group keep the freshest copy. Group order is `lastActivityAt`
		* descending (recomputed from member sessions, so a stale wire value
		* cannot misplace a group), with the unknown-project bucket ('') always
		* last — same rule as the board.
		*/
		function buildProjectViewModel(input) {
			const { groups, nowMs } = input;
			const buckets = /* @__PURE__ */ new Map();
			for (const group of groups) {
				const key = normalizeProjectKey(group.project);
				let bucket = buckets.get(key);
				if (bucket === void 0) {
					bucket = {
						sessions: /* @__PURE__ */ new Map(),
						wireLastActivityAt: Number.NEGATIVE_INFINITY
					};
					buckets.set(key, bucket);
				}
				if (Number.isFinite(group.lastActivityAt)) bucket.wireLastActivityAt = Math.max(bucket.wireLastActivityAt, group.lastActivityAt);
				for (const session of group.sessions) {
					const id = `${session.agent}${KEY_SEP$1}${session.sessionId}`;
					const existing = bucket.sessions.get(id);
					if (existing === void 0 || session.lastActivityAt > existing.lastActivityAt) bucket.sessions.set(id, session);
				}
			}
			const derived = [];
			let sessionCount = 0;
			for (const [key, bucket] of buckets) {
				const sessions = [...bucket.sessions.values()];
				sessionCount += sessions.length;
				const memberMax = sessions.reduce((max, s) => Number.isFinite(s.lastActivityAt) ? Math.max(max, s.lastActivityAt) : max, Number.NEGATIVE_INFINITY);
				const lastActivityAt = Math.max(memberMax, bucket.wireLastActivityAt);
				const agentBadges = deriveAgentBadges(sessions);
				derived.push({
					key,
					label: projectDisplayName(key),
					fullPath: key,
					agentBadges,
					crossAgentLabel: agentBadges.length > 1 ? formatTemplate$2(PROJECT_VIEW_STRINGS.crossAgent, { n: agentBadges.length }) : null,
					sessionCount: sessions.length,
					sessionCountLabel: formatTemplate$2(PROJECT_VIEW_STRINGS.sessionCount, { n: sessions.length }),
					lastActivityAt: Number.isFinite(lastActivityAt) ? lastActivityAt : 0,
					lastActiveLabel: Number.isFinite(lastActivityAt) ? formatTemplate$2(PROJECT_VIEW_STRINGS.lastActive, { time: formatRelativeTime$1(lastActivityAt, nowMs) }) : "",
					lanes: buildAgentLanes(sessions, nowMs)
				});
			}
			derived.sort((a, b) => {
				if (a.key === "") return b.key === "" ? 0 : 1;
				if (b.key === "") return -1;
				return b.lastActivityAt - a.lastActivityAt || a.key.localeCompare(b.key);
			});
			return {
				groups: derived,
				emptyState: derived.length === 0 ? { ...PROJECT_VIEW_STRINGS.empty } : null,
				projectCount: derived.length,
				sessionCount,
				summaryLabel: formatTemplate$2(PROJECT_VIEW_STRINGS.summary, {
					projects: derived.length,
					sessions: sessionCount
				})
			};
		}
		//#endregion
		//#region \0dsh-css:src/client/board/project-view.module.css.mjs
		const css$9 = ".Twjdya_root{box-sizing:border-box;height:100%;min-height:320px;color:var(--agsc-fg);background:var(--agsc-bg);flex-direction:column;gap:12px;padding:16px 20px;display:flex;overflow-y:auto}.Twjdya_topbar{flex-wrap:wrap;align-items:center;gap:10px;display:flex}.Twjdya_title{color:var(--agsc-fg);font-size:15px;font-weight:650}.Twjdya_summary{color:var(--agsc-fg-secondary);font-size:12px}.Twjdya_banner{border-radius:var(--agsc-radius-control);border:1px solid var(--agsc-border);padding:6px 10px;font-size:12px;line-height:18px}.Twjdya_banner[data-tone=danger]{color:var(--agsc-err);background:var(--dsw-alias-state-error-secondary);border-color:var(--dsw-alias-state-error-secondary)}.Twjdya_empty{text-align:center;flex-direction:column;flex:1;justify-content:center;align-items:center;gap:6px;min-height:160px;padding:24px;display:flex}.Twjdya_emptyTitle{color:var(--agsc-fg);font-size:14px;font-weight:600}.Twjdya_emptyHint{max-width:420px;color:var(--agsc-fg-secondary);font-size:12px;line-height:20px}.Twjdya_group{border-radius:var(--agsc-radius-card);border:1px solid var(--agsc-border-strong);background:var(--agsc-bg-raised);flex-direction:column;gap:8px;padding:10px 12px;display:flex}.Twjdya_groupHead{flex-wrap:wrap;align-items:baseline;gap:8px;min-width:0;display:flex}.Twjdya_groupName{color:var(--agsc-fg);text-overflow:ellipsis;white-space:nowrap;font-size:13px;font-weight:600;overflow:hidden}.Twjdya_agentBadges{flex-wrap:wrap;align-items:center;gap:4px;display:inline-flex}.Twjdya_agentBadge{border:1px solid var(--agsc-border);background:var(--dsw-alias-bg-layer-2);color:var(--agsc-fg-secondary);white-space:nowrap;border-radius:999px;align-items:center;gap:4px;padding:0 7px;font-size:11px;line-height:18px;display:inline-flex}.Twjdya_crossBadge{color:var(--dsw-alias-label-primary-foreground);background:var(--agsc-accent);white-space:nowrap;border-radius:999px;padding:0 7px;font-size:11px;line-height:18px}.Twjdya_spacer{flex:1}.Twjdya_groupMeta{color:var(--agsc-fg-dimmed);white-space:nowrap;flex:none;font-size:11px}.Twjdya_lanes{flex-direction:column;gap:6px;display:flex}.Twjdya_lane{flex-direction:column;gap:4px;display:flex}.Twjdya_laneHead{color:var(--agsc-fg);align-items:center;gap:5px;font-size:12px;font-weight:600;display:inline-flex}.Twjdya_glyph{color:var(--agsc-accent);flex:none}.Twjdya_laneSessions{flex-direction:column;gap:4px;display:flex}.Twjdya_showMore{align-self:flex-start}.Twjdya_session{text-align:left;border-radius:var(--agsc-radius-control);border:1px solid var(--agsc-border);background:var(--dsw-alias-bg-base);min-width:0;color:var(--agsc-fg);cursor:pointer;align-items:center;gap:8px;padding:5px 8px;font-family:inherit;font-size:12px;display:flex}.Twjdya_session:hover{border-color:var(--agsc-border-strong);background:var(--dsw-alias-interactive-bg-hover)}.Twjdya_session:focus-visible{outline:2px solid var(--agsc-accent);outline-offset:1px}.Twjdya_statusPill{flex:none}.Twjdya_dot{background:var(--dsw-alias-label-tertiary);border-radius:50%;flex:none;width:7px;height:7px}.Twjdya_dot[data-tone=neutral]{background:var(--dsw-alias-label-tertiary)}.Twjdya_dot[data-tone=muted]{background:var(--agsc-fg-dimmed)}.Twjdya_attention{color:var(--agsc-warn);font-size:11px}.Twjdya_attention[data-kind=gap]{color:var(--agsc-err)}.Twjdya_sessionTitle{text-overflow:ellipsis;white-space:nowrap;min-width:0;overflow:hidden}.Twjdya_liveChip{color:var(--agsc-ok);flex:none}.Twjdya_sessionId{font-size:11px;font-family:var(--agsc-font-mono);color:var(--agsc-fg-dimmed);flex:none}.Twjdya_sessionTime{color:var(--agsc-fg-dimmed);flex:none;margin-left:auto;font-size:11px}";
		const tagId$9 = "@shendeguize/dsh-agent-sidecar/src/client/board/project-view.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$9, css$9);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$9) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$9;
				created = true;
			}
			tag.textContent = css$9;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var project_view_module_css_default = {
			"agentBadge": "Twjdya_agentBadge",
			"agentBadges": "Twjdya_agentBadges",
			"attention": "Twjdya_attention",
			"banner": "Twjdya_banner",
			"crossBadge": "Twjdya_crossBadge",
			"dot": "Twjdya_dot",
			"empty": "Twjdya_empty",
			"emptyHint": "Twjdya_emptyHint",
			"emptyTitle": "Twjdya_emptyTitle",
			"glyph": "Twjdya_glyph",
			"group": "Twjdya_group",
			"groupHead": "Twjdya_groupHead",
			"groupMeta": "Twjdya_groupMeta",
			"groupName": "Twjdya_groupName",
			"lane": "Twjdya_lane",
			"laneHead": "Twjdya_laneHead",
			"laneSessions": "Twjdya_laneSessions",
			"lanes": "Twjdya_lanes",
			"liveChip": "Twjdya_liveChip",
			"root": "Twjdya_root",
			"session": "Twjdya_session",
			"sessionId": "Twjdya_sessionId",
			"sessionTime": "Twjdya_sessionTime",
			"sessionTitle": "Twjdya_sessionTitle",
			"showMore": "Twjdya_showMore",
			"spacer": "Twjdya_spacer",
			"statusPill": "Twjdya_statusPill",
			"summary": "Twjdya_summary",
			"title": "Twjdya_title",
			"topbar": "Twjdya_topbar"
		};
		//#endregion
		//#region src/client/board/project-view.tsx
		/**
		* Cross-agent project correlation view (design §4.e.2 / §5.1 M3, T5.5).
		*
		* Presentation-only and fully controlled: no data fetching, no api/sse
		* imports. The owner fetches `GET <prefix>/projects`, hands the wire
		* groups in as props (the wire shape IS the input view model — camelCase,
		* epoch-ms `lastActivityAt`), and receives session clicks back through
		* `onSelectSession` (pass-through to the detail view, same as Board).
		*
		* Render precedence when there is nothing to show: error > loading >
		* empty state. When groups ARE available, an error renders as a banner
		* above the (possibly stale) content instead of replacing it, and
		* `loading` shows as a quiet header chip — honest degradation without
		* blanking data the user already has.
		*/
		function sessionDotState(status) {
			if (status === "working") return "ongoing";
			if (status === "waiting") return "warning";
			return null;
		}
		function SessionRow$1(props) {
			const { session, onSelect } = props;
			const dotState = sessionDotState(session.badge.status);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
				type: "button",
				className: project_view_module_css_default["session"],
				onClick: () => onSelect(session.sessionId),
				"data-testid": "agent-sidecar-project-session",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
						className: project_view_module_css_default["statusPill"],
						children: [
							dotState === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: project_view_module_css_default["dot"],
								"data-tone": session.badge.tone
							}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
								state: dotState,
								size: 8
							}),
							session.badge.label,
							session.badge.attention !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: project_view_module_css_default["attention"],
								"data-kind": session.badge.attention,
								children: session.badge.attentionLabel
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: project_view_module_css_default["sessionTitle"],
						title: session.title,
						children: session.displayTitle
					}),
					session.live && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
						className: project_view_module_css_default["liveChip"],
						children: PROJECT_VIEW_STRINGS.liveChip
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: project_view_module_css_default["sessionId"],
						title: session.sessionId,
						children: session.shortId
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: project_view_module_css_default["sessionTime"],
						children: session.relativeTime
					})
				]
			});
		}
		function AgentLane(props) {
			const { lane, onSelect } = props;
			const [expanded, setExpanded] = (0, react.useState)(false);
			const { shown, hiddenCount } = sliceCardsForDisplay(lane.sessions, 10, expanded);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: project_view_module_css_default["lane"],
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: project_view_module_css_default["laneHead"],
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: project_view_module_css_default["glyph"],
							"aria-hidden": true,
							children: lane.glyph
						}), lane.agent]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: project_view_module_css_default["laneSessions"],
						children: shown.map((session) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(SessionRow$1, {
							session,
							onSelect
						}, `${session.agent}:${session.sessionId}`))
					}),
					hiddenCount > 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
						size: "sm",
						variant: "outline",
						className: project_view_module_css_default["showMore"],
						onClick: () => {
							setExpanded(true);
						},
						children: formatTemplate$2(PROJECT_VIEW_STRINGS.showAllSessions, { n: lane.sessions.length })
					}),
					expanded && lane.sessions.length > 10 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
						size: "sm",
						variant: "outline",
						className: project_view_module_css_default["showMore"],
						onClick: () => {
							setExpanded(false);
						},
						children: formatTemplate$2(PROJECT_VIEW_STRINGS.showLessSessions, { n: 10 })
					})
				]
			});
		}
		function ProjectSection(props) {
			const { group, onSelect } = props;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: project_view_module_css_default["group"],
				"data-testid": "agent-sidecar-project-group",
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: project_view_module_css_default["groupHead"],
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: project_view_module_css_default["groupName"],
							title: group.fullPath === "" ? void 0 : group.fullPath,
							children: group.label
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: project_view_module_css_default["agentBadges"],
							children: [group.agentBadges.map((badge) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
								className: project_view_module_css_default["agentBadge"],
								title: badge.agent,
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: project_view_module_css_default["glyph"],
									"aria-hidden": true,
									children: badge.glyph
								}), badge.agent]
							}, badge.agent)), group.crossAgentLabel !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: project_view_module_css_default["crossBadge"],
								children: group.crossAgentLabel
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { className: project_view_module_css_default["spacer"] }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: project_view_module_css_default["groupMeta"],
							children: group.sessionCountLabel
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: project_view_module_css_default["groupMeta"],
							children: group.lastActiveLabel
						})
					]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: project_view_module_css_default["lanes"],
					children: group.lanes.map((lane) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(AgentLane, {
						lane,
						onSelect
					}, lane.agent))
				})]
			});
		}
		/** The project correlation view. Pure render of `buildProjectViewModel`. */
		function ProjectView(props) {
			const nowMs = props.nowMs ?? Date.now();
			const vm = buildProjectViewModel({
				groups: props.groups,
				nowMs
			});
			const hasContent = vm.groups.length > 0;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				...surfaceProps("project-view", project_view_module_css_default["root"]),
				"data-testid": "agent-sidecar-project-view",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("header", {
						className: project_view_module_css_default["topbar"],
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: project_view_module_css_default["title"],
								children: PROJECT_VIEW_STRINGS.title
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: project_view_module_css_default["summary"],
								children: vm.summaryLabel
							}),
							props.loading && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								role: "status",
								children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, { children: PROJECT_VIEW_STRINGS.loading })
							})
						]
					}),
					props.error !== null && hasContent && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: project_view_module_css_default["banner"],
						"data-tone": "danger",
						role: "alert",
						children: [
							PROJECT_VIEW_STRINGS.errorTitle,
							": ",
							props.error
						]
					}),
					hasContent ? vm.groups.map((group) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ProjectSection, {
						group,
						onSelect: props.onSelectSession
					}, group.key === "" ? "\0unknown" : group.key)) : props.error !== null ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: project_view_module_css_default["empty"],
						"data-kind": "error",
						role: "alert",
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: project_view_module_css_default["emptyTitle"],
							children: PROJECT_VIEW_STRINGS.errorTitle
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: project_view_module_css_default["emptyHint"],
							children: props.error
						})]
					}) : props.loading ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: project_view_module_css_default["empty"],
						"data-kind": "loading",
						role: "status",
						children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: project_view_module_css_default["emptyHint"],
							children: PROJECT_VIEW_STRINGS.loading
						})
					}) : vm.emptyState !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: project_view_module_css_default["empty"],
						"data-kind": "no-projects",
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: project_view_module_css_default["emptyTitle"],
							children: vm.emptyState.title
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: project_view_module_css_default["emptyHint"],
							children: vm.emptyState.hint
						})]
					})
				]
			});
		}
		//#endregion
		//#region src/client/widget.tsx
		const DOT_COLORS = {
			ok: "var(--agsc-ok)",
			degraded: "var(--agsc-warn)",
			off: "var(--agsc-fg-dimmed)"
		};
		const rootStyle = {
			display: "inline-flex",
			alignItems: "center",
			gap: 4,
			padding: "2px 6px",
			border: "none",
			borderRadius: 6,
			background: "transparent",
			font: "inherit",
			color: "var(--agsc-fg-secondary)"
		};
		const countStyle = {
			fontSize: 11,
			fontVariantNumeric: "tabular-nums",
			lineHeight: "14px",
			whiteSpace: "nowrap"
		};
		/**
		* Connection dot + working-session counter (e.g. `▸2`) for the footer.
		*
		* Without `onOpen` the widget remains an inert status `<span>` so button
		* semantics are only emitted for an actual control.
		*/
		function SidecarWidget(props) {
			const title = widgetTitle(props.connection, props.workingCount);
			const body = /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
				"aria-hidden": true,
				style: {
					width: 8,
					height: 8,
					borderRadius: "50%",
					flex: "none",
					background: DOT_COLORS[props.connection]
				}
			}), props.workingCount > 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
				style: countStyle,
				"data-testid": "agent-sidecar-widget-count",
				children: `▸${props.workingCount}`
			})] });
			if (props.onOpen === void 0) return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
				...surfaceProps("footer-widget"),
				style: rootStyle,
				title,
				"aria-label": title,
				role: "status",
				"data-testid": "agent-sidecar-widget",
				"data-connection": props.connection,
				children: body
			});
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
				...surfaceProps("footer-widget"),
				type: "button",
				style: {
					...rootStyle,
					cursor: "pointer"
				},
				title,
				"aria-label": title,
				onClick: props.onOpen,
				"data-testid": "agent-sidecar-widget",
				"data-connection": props.connection,
				children: body
			});
		}
		//#endregion
		//#region \0dsh-css:src/client/settings-card.module.css.mjs
		const css$8 = ".KApx_W_card{box-sizing:border-box;border:1px solid var(--dsw-alias-border-l2);border-radius:var(--agsc-radius-card);background:var(--dsw-alias-bg-layer-3);width:100%;max-width:760px;list-style:none;transition:border-color .16s,background .16s}.KApx_W_card:hover{border-color:var(--dsw-alias-label-dimmed)}.KApx_W_cardOpen{background:var(--dsw-alias-bg-layer-2);border-color:var(--dsw-alias-label-dimmed)}.KApx_W_header{appearance:none;width:100%;font:inherit;color:inherit;text-align:left;cursor:pointer;border-radius:var(--agsc-radius-card);background:0 0;border:0;align-items:center;gap:12px;padding:14px 16px;display:flex}.KApx_W_header:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:-2px}.KApx_W_headText{flex-direction:column;flex:1;gap:4px;min-width:0;display:flex}.KApx_W_name{color:var(--dsw-alias-label-primary);font-size:15px;font-weight:600;line-height:1.4}.KApx_W_description{color:var(--dsw-alias-label-tertiary);font-size:13px;line-height:1.5}.KApx_W_pending{white-space:nowrap;flex:none}.KApx_W_chevron{color:var(--dsw-alias-label-tertiary);flex:none;transition:transform .16s}.KApx_W_chevronOpen{transform:rotate(180deg)}.KApx_W_body{border-top:1px solid var(--dsw-alias-border-l2);margin:0 16px;padding-bottom:8px}.KApx_W_readOnly{color:var(--dsw-alias-label-tertiary);margin:12px 0 0;font-size:12px;line-height:1.5}.KApx_W_section{padding:12px 0 4px}.KApx_W_section+.KApx_W_section{border-top:1px solid var(--dsw-alias-border-l2)}.KApx_W_sectionTitle{color:var(--dsw-alias-label-primary);margin:0 0 4px;font-size:13px;font-weight:600;line-height:1.5}.KApx_W_field{flex-direction:column;gap:6px;padding:8px 0;display:flex}.KApx_W_label{color:var(--dsw-alias-label-primary);font-size:13px;line-height:1.5}.KApx_W_hint{color:var(--dsw-alias-label-tertiary);margin:0;font-size:12px;line-height:1.5}.KApx_W_invalidHint{color:var(--dsw-alias-state-error-primary);margin:0;font-size:12px;line-height:1.5}.KApx_W_input{box-sizing:border-box;width:100%;max-width:420px}.KApx_W_inputInvalid{border-color:var(--dsw-alias-state-error-primary)}.KApx_W_input:has(input:disabled){opacity:.5}.KApx_W_select{box-sizing:border-box;border:1px solid var(--dsw-alias-border-l2);border-radius:var(--agsc-radius-control);background:var(--dsw-alias-bg-layer-1);width:100%;max-width:420px;height:32px;font:inherit;color:var(--dsw-alias-label-primary);padding:0 8px;font-size:13px;line-height:1.5}.KApx_W_select:focus-visible{border-color:var(--dsw-alias-brand-primary);outline:none}.KApx_W_select:disabled{opacity:.5;cursor:default}.KApx_W_toggleRow{cursor:pointer;align-items:center;gap:10px;display:flex}.KApx_W_toggleRow input{accent-color:var(--dsw-alias-brand-primary);cursor:pointer;width:16px;height:16px;margin:0}.KApx_W_toggleRow input:disabled{cursor:default}.KApx_W_note{background:var(--dsw-alias-bg-module-platform);color:var(--dsw-alias-label-secondary);border-radius:8px;margin:6px 0 2px;padding:8px 10px;font-size:12px;line-height:1.6}.KApx_W_statusRow{align-items:center;gap:8px;padding:4px 0;display:flex}.KApx_W_statusDot{background:var(--agsc-fg-dimmed);border-radius:50%;flex:none;width:8px;height:8px}.KApx_W_statusOk{background:var(--agsc-ok)}.KApx_W_statusError{background:var(--agsc-err)}.KApx_W_statusText{color:var(--dsw-alias-label-primary);font-size:13px;line-height:1.5}.KApx_W_statusMeta{color:var(--dsw-alias-label-tertiary);font-size:12px;line-height:1.5}.KApx_W_retry{margin-left:auto}.KApx_W_footer{border-top:1px solid var(--dsw-alias-border-l2);align-items:center;gap:8px;padding:12px 0 4px;display:flex}.KApx_W_docs{color:var(--dsw-alias-label-tertiary);font-size:12px;line-height:1.5;text-decoration:none}.KApx_W_docs:hover{color:var(--dsw-alias-label-primary);text-decoration:underline}.KApx_W_failed{text-align:right;min-width:0;color:var(--dsw-alias-state-error-primary);flex:1;margin:0;font-size:12px;line-height:1.5}.KApx_W_spacer{flex:1}";
		const tagId$8 = "@shendeguize/dsh-agent-sidecar/src/client/settings-card.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$8, css$8);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$8) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$8;
				created = true;
			}
			tag.textContent = css$8;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var settings_card_module_css_default = {
			"body": "KApx_W_body",
			"card": "KApx_W_card",
			"cardOpen": "KApx_W_cardOpen",
			"chevron": "KApx_W_chevron",
			"chevronOpen": "KApx_W_chevronOpen",
			"description": "KApx_W_description",
			"docs": "KApx_W_docs",
			"failed": "KApx_W_failed",
			"field": "KApx_W_field",
			"footer": "KApx_W_footer",
			"headText": "KApx_W_headText",
			"header": "KApx_W_header",
			"hint": "KApx_W_hint",
			"input": "KApx_W_input",
			"inputInvalid": "KApx_W_inputInvalid",
			"invalidHint": "KApx_W_invalidHint",
			"label": "KApx_W_label",
			"name": "KApx_W_name",
			"note": "KApx_W_note",
			"pending": "KApx_W_pending",
			"readOnly": "KApx_W_readOnly",
			"retry": "KApx_W_retry",
			"section": "KApx_W_section",
			"sectionTitle": "KApx_W_sectionTitle",
			"select": "KApx_W_select",
			"spacer": "KApx_W_spacer",
			"statusDot": "KApx_W_statusDot",
			"statusError": "KApx_W_statusError",
			"statusMeta": "KApx_W_statusMeta",
			"statusOk": "KApx_W_statusOk",
			"statusRow": "KApx_W_statusRow",
			"statusText": "KApx_W_statusText",
			"toggleRow": "KApx_W_toggleRow"
		};
		//#endregion
		//#region src/client/settings-fields.tsx
		function SelectField(props) {
			const id = (0, react.useId)();
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: settings_card_module_css_default["field"],
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("label", {
						className: settings_card_module_css_default["label"],
						htmlFor: id,
						children: props.label
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("select", {
						id,
						className: settings_card_module_css_default["select"],
						value: props.value,
						disabled: props.disabled,
						onChange: (event) => {
							props.onCommit(event.target.value);
						},
						children: props.options.map((option) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)("option", {
							value: option.value,
							children: option.label
						}, option.value))
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: settings_card_module_css_default["hint"],
						children: props.hint
					})
				]
			});
		}
		function ToggleField(props) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: settings_card_module_css_default["field"],
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("label", {
					className: settings_card_module_css_default["toggleRow"],
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
						type: "checkbox",
						checked: props.checked,
						disabled: props.disabled,
						onChange: (event) => {
							props.onCommit(event.target.checked);
						}
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: settings_card_module_css_default["label"],
						children: props.label
					})]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
					className: settings_card_module_css_default["hint"],
					children: props.hint
				})]
			});
		}
		function TextField(props) {
			const id = (0, react.useId)();
			const [draft, setDraft] = (0, react.useState)(props.value);
			(0, react.useEffect)(() => {
				setDraft(props.value);
			}, [props.value]);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: settings_card_module_css_default["field"],
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("label", {
						className: settings_card_module_css_default["label"],
						htmlFor: id,
						children: props.label
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Input, {
						id,
						className: settings_card_module_css_default["input"],
						type: "text",
						value: draft,
						placeholder: props.placeholder ?? "",
						disabled: props.disabled,
						onChange: (event) => {
							setDraft(event.target.value);
							props.onCommit(event.target.value);
						}
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: settings_card_module_css_default["hint"],
						children: props.hint
					})
				]
			});
		}
		function NumberField(props) {
			const id = (0, react.useId)();
			const [draft, setDraft] = (0, react.useState)(String(props.value));
			const [invalid, setInvalid] = (0, react.useState)(false);
			(0, react.useEffect)(() => {
				setDraft(String(props.value));
				setInvalid(false);
			}, [props.value]);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: settings_card_module_css_default["field"],
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("label", {
						className: settings_card_module_css_default["label"],
						htmlFor: id,
						children: props.label
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Input, {
						id,
						className: `${settings_card_module_css_default["input"]} ${invalid ? settings_card_module_css_default["inputInvalid"] : ""}`,
						type: "text",
						inputMode: "numeric",
						value: draft,
						disabled: props.disabled,
						...invalid ? { "aria-invalid": true } : {},
						onChange: (event) => {
							const text = event.target.value;
							const parsed = Number(text);
							const acceptable = text.trim() !== "" && Number.isInteger(parsed) && parsed >= props.min;
							setDraft(text);
							setInvalid(!acceptable);
							if (acceptable && parsed !== props.value) props.onCommit(parsed);
						}
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: invalid ? settings_card_module_css_default["invalidHint"] : settings_card_module_css_default["hint"],
						...invalid ? { role: "alert" } : {},
						children: invalid ? props.invalidHint : props.hint
					})
				]
			});
		}
		//#endregion
		//#region src/client/settings-card.tsx
		/**
		* Agent Sidecar settings card (browser half, T2.3).
		*
		* MECHANISM (source-audit conclusion): the dsh settings pane does NOT
		* auto-render a form from the host Config schema. The plugin-configuration
		* tab dispatches the keyed `settings.plugin.item` slot per Host-served
		* settings namespace and "a card draws its own internals; the tab only
		* decides which namespaces to dispatch and stacks what comes back"
		* (harness `packages/client/ui-settings-plugins/src/client/slot-contract.ts`;
		* unclaimed namespaces render nothing per the adding-a-settings-card
		* cookbook). This card therefore carries the form UI for the key items of
		* the host Config (src/config.ts): daemon.policy/backoffLimit,
		* sidecar.command/runtimeDir, stream.reconcileActiveMs/IdleMs,
		* inject.enabled/defaultMode, analysis.enabled, ui.timeWindowHours/showDead,
		* skill.provide — plus the daemon status/retry row and the injection safety
		* note the design doc (§4.a/§5.3/§6) puts on the settings surface.
		*
		* WIRING CONTRACT (T2.4): the card is fully controlled and presentational.
		* `values` is the staged draft owned by the wiring controller; every edit
		* reports through `onChange(field, value)` and the controller must echo the
		* staged value back through `values` (the shipped ui-settings-plugins cards
		* follow the same staged-edit split). Save/discard/retry are plain
		* callbacks. Registration into `settings.plugin.item` (keyed by the Host
		* settings namespace) also belongs to the wiring half — this module exports
		* the component and its props contract only.
		*
		* The card renders as an `<li>` because the plugin-configuration tab stacks
		* cards in a list (shipped PluginCard precedent). Chrome (disclosure
		* header, unsaved pill, save/discard footer) mirrors the shipped card so
		* the sidecar card reads native next to first-party ones.
		*/
		const DAEMON_STATE_KEY = {
			"probe": "settings.daemonStateProbe",
			"adopted": "settings.daemonStateAdopted",
			"defer": "settings.daemonStateDefer",
			"reprobe": "settings.daemonStateReprobe",
			"hosting": "settings.daemonStateHosting",
			"hosted": "settings.daemonStateHosted",
			"backoff": "settings.daemonStateBackoff",
			"failed": "settings.daemonStateFailed"
		};
		/** Dot classes per daemon state: healthy / transitional / tripped. */
		function statusDotClass(state) {
			if (state === "adopted" || state === "hosted") return `${settings_card_module_css_default["statusDot"]} ${settings_card_module_css_default["statusOk"]}`;
			if (state === "failed") return `${settings_card_module_css_default["statusDot"]} ${settings_card_module_css_default["statusError"]}`;
			return settings_card_module_css_default["statusDot"] ?? "";
		}
		function Section(props) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				className: settings_card_module_css_default["section"],
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("h3", {
					className: settings_card_module_css_default["sectionTitle"],
					children: props.title
				}), props.children]
			});
		}
		/**
		* Render the Agent Sidecar settings card.
		* @param props - staged values, form state, and the wiring callbacks.
		* @returns the card.
		*/
		function SettingsCard(props) {
			const [open, setOpen] = (0, react.useState)(false);
			const t$2 = props.t ?? t;
			const { values } = props;
			const disabled = !props.writable || props.saving;
			const title = t$2("settings.cardTitle");
			const daemonNote = props.daemon?.state === "defer" ? t$2("settings.daemonDeferNote") : props.daemon?.state === "failed" ? t$2("settings.daemonFailedNote") : void 0;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("li", {
				...surfaceProps("settings-card", `${settings_card_module_css_default["card"]} ${open ? settings_card_module_css_default["cardOpen"] : ""}`),
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
					type: "button",
					className: settings_card_module_css_default["header"],
					"aria-expanded": open,
					"aria-label": `${t$2(open ? "settings.collapse" : "settings.expand")}: ${title}`,
					onClick: () => {
						setOpen(!open);
					},
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: settings_card_module_css_default["headText"],
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: settings_card_module_css_default["name"],
								children: title
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: settings_card_module_css_default["description"],
								children: t$2("settings.cardDescription")
							})]
						}),
						props.dirty ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
							className: settings_card_module_css_default["pending"],
							children: t$2("settings.unsaved")
						}) : null,
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.IconChevronDownOutline14, { className: `${settings_card_module_css_default["chevron"]} ${open ? settings_card_module_css_default["chevronOpen"] : ""}` })
					]
				}), open ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: settings_card_module_css_default["body"],
					children: [
						!props.writable ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
							className: settings_card_module_css_default["readOnly"],
							role: "status",
							children: t$2("settings.readOnly")
						}) : null,
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Section, {
							title: t$2("settings.sectionDaemon"),
							children: [
								props.daemon ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: settings_card_module_css_default["field"],
									children: [
										/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
											className: settings_card_module_css_default["label"],
											children: t$2("settings.daemonStatusLabel")
										}),
										/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
											className: settings_card_module_css_default["statusRow"],
											children: [
												/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
													className: statusDotClass(props.daemon.state),
													"aria-hidden": true
												}),
												/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
													className: settings_card_module_css_default["statusText"],
													children: t$2(DAEMON_STATE_KEY[props.daemon.state])
												}),
												props.daemon.pid !== void 0 && props.daemon.version !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
													className: settings_card_module_css_default["statusMeta"],
													children: t$2("settings.daemonPidVersion", {
														pid: props.daemon.pid,
														version: props.daemon.version
													})
												}) : null,
												props.daemon.state === "failed" && props.onDaemonRetry !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
													type: "button",
													size: "sm",
													variant: "outline",
													className: settings_card_module_css_default["retry"],
													onClick: props.onDaemonRetry,
													children: t$2("settings.daemonRetry")
												}) : null
											]
										}),
										daemonNote !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
											className: settings_card_module_css_default["note"],
											children: daemonNote
										}) : null
									]
								}) : null,
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)(SelectField, {
									label: t$2("settings.daemonPolicyLabel"),
									hint: t$2("settings.daemonPolicyHint"),
									value: values.daemonPolicy,
									disabled,
									options: [
										{
											value: "adopt-or-host",
											label: t$2("settings.daemonPolicyAdoptOrHost")
										},
										{
											value: "adopt-only",
											label: t$2("settings.daemonPolicyAdoptOnly")
										},
										{
											value: "off",
											label: t$2("settings.daemonPolicyOff")
										}
									],
									onCommit: (value) => {
										props.onChange("daemonPolicy", value);
									}
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)(NumberField, {
									label: t$2("settings.daemonBackoffLimitLabel"),
									hint: t$2("settings.daemonBackoffLimitHint"),
									invalidHint: t$2("settings.invalidNumber", { min: 1 }),
									min: 1,
									value: values.daemonBackoffLimit,
									disabled,
									onCommit: (value) => {
										props.onChange("daemonBackoffLimit", value);
									}
								})
							]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Section, {
							title: t$2("settings.sectionSidecar"),
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(TextField, {
								label: t$2("settings.sidecarCommandLabel"),
								hint: t$2("settings.sidecarCommandHint"),
								value: values.sidecarCommand,
								placeholder: "agent-sidecar",
								disabled,
								onCommit: (value) => {
									props.onChange("sidecarCommand", value);
								}
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(TextField, {
								label: t$2("settings.sidecarRuntimeDirLabel"),
								hint: t$2("settings.sidecarRuntimeDirHint"),
								value: values.sidecarRuntimeDir,
								placeholder: "~/.agent_sidecar",
								disabled,
								onCommit: (value) => {
									props.onChange("sidecarRuntimeDir", value);
								}
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Section, {
							title: t$2("settings.sectionStream"),
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(NumberField, {
								label: t$2("settings.streamActiveMsLabel"),
								hint: t$2("settings.streamActiveMsHint"),
								invalidHint: t$2("settings.invalidNumber", { min: 100 }),
								min: 100,
								value: values.streamReconcileActiveMs,
								disabled,
								onCommit: (value) => {
									props.onChange("streamReconcileActiveMs", value);
								}
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(NumberField, {
								label: t$2("settings.streamIdleMsLabel"),
								hint: t$2("settings.streamIdleMsHint"),
								invalidHint: t$2("settings.invalidNumber", { min: 100 }),
								min: 100,
								value: values.streamReconcileIdleMs,
								disabled,
								onCommit: (value) => {
									props.onChange("streamReconcileIdleMs", value);
								}
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Section, {
							title: t$2("settings.sectionInject"),
							children: [
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
									className: settings_card_module_css_default["note"],
									children: t$2("settings.injectSafetyNote")
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)(ToggleField, {
									label: t$2("settings.injectEnabledLabel"),
									hint: t$2("settings.injectEnabledHint"),
									checked: values.injectEnabled,
									disabled,
									onCommit: (checked) => {
										props.onChange("injectEnabled", checked);
									}
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)(SelectField, {
									label: t$2("settings.injectDefaultModeLabel"),
									hint: t$2("settings.injectDefaultModeHint"),
									value: values.injectDefaultMode,
									disabled,
									options: [{
										value: "queue",
										label: t$2("settings.injectModeQueue")
									}, {
										value: "steer",
										label: t$2("settings.injectModeSteer")
									}],
									onCommit: (value) => {
										props.onChange("injectDefaultMode", value);
									}
								})
							]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Section, {
							title: t$2("settings.sectionAnalysis"),
							children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ToggleField, {
								label: t$2("settings.analysisEnabledLabel"),
								hint: t$2("settings.analysisEnabledHint"),
								checked: values.analysisEnabled,
								disabled,
								onCommit: (checked) => {
									props.onChange("analysisEnabled", checked);
								}
							})
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(Section, {
							title: t$2("settings.sectionUi"),
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(NumberField, {
								label: t$2("settings.uiTimeWindowHoursLabel"),
								hint: t$2("settings.uiTimeWindowHoursHint"),
								invalidHint: t$2("settings.invalidNumber", { min: 1 }),
								min: 1,
								value: values.uiTimeWindowHours,
								disabled,
								onCommit: (value) => {
									props.onChange("uiTimeWindowHours", value);
								}
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ToggleField, {
								label: t$2("settings.uiShowDeadLabel"),
								hint: t$2("settings.uiShowDeadHint"),
								checked: values.uiShowDead,
								disabled,
								onCommit: (checked) => {
									props.onChange("uiShowDead", checked);
								}
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Section, {
							title: t$2("settings.sectionSkill"),
							children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ToggleField, {
								label: t$2("settings.skillProvideLabel"),
								hint: t$2("settings.skillProvideHint"),
								checked: values.skillProvide,
								disabled,
								onCommit: (checked) => {
									props.onChange("skillProvide", checked);
								}
							})
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: settings_card_module_css_default["footer"],
							children: [
								props.docsUrl !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("a", {
									className: settings_card_module_css_default["docs"],
									href: props.docsUrl,
									target: "_blank",
									rel: "noreferrer",
									children: t$2("settings.docsLink")
								}) : null,
								props.saveFailed === true ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
									className: settings_card_module_css_default["failed"],
									role: "status",
									children: t$2("settings.saveFailed")
								}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { className: settings_card_module_css_default["spacer"] }),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
									type: "button",
									size: "sm",
									variant: "outline",
									disabled: !props.dirty || props.saving,
									onClick: props.onDiscard,
									children: t$2("settings.discard")
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
									type: "button",
									size: "sm",
									variant: "primary",
									disabled: !props.dirty || props.saving || !props.writable,
									onClick: props.onSave,
									children: t$2(props.saving ? "settings.saving" : "settings.save")
								})
							]
						})
					]
				}) : null]
			});
		}
		//#endregion
		//#region src/client/detail/strings.ts
		/** Dynamic session-detail vocabulary; templates are interpolated by `formatTemplate`. */
		const DETAIL_STRINGS = createLocaleView({
			header: {
				close: "detail.header.close",
				listenOn: "detail.header.listenOn",
				listenOff: "detail.header.listenOff",
				listenHint: "detail.header.listenHint",
				refresh: "detail.header.refresh",
				refreshing: "detail.header.refreshing",
				refreshHint: "detail.header.refreshHint",
				copyIdTitle: "detail.header.copyIdTitle",
				copied: "detail.header.copied",
				untitled: "detail.header.untitled",
				unknownProject: "detail.header.unknownProject",
				observedDisclaimer: "detail.header.observedDisclaimer"
			},
			status: {
				working: "detail.status.working",
				waiting: "detail.status.waiting",
				idle: "detail.status.idle",
				dead: "detail.status.dead",
				unknown: "detail.status.unknown"
			},
			sources: {
				title: "detail.sources.title",
				dshLive: "detail.sources.dshLive",
				dshCold: "detail.sources.dshCold",
				sidecarReplay: "detail.sources.sidecarReplay",
				sidecarBuffer: "detail.sources.sidecarBuffer",
				none: "detail.sources.none"
			},
			kind: {
				user: "detail.kind.user",
				assistant: "detail.kind.assistant",
				thinking: "detail.kind.thinking",
				toolCall: "detail.kind.toolCall",
				toolResult: "detail.kind.toolResult",
				turn: "detail.kind.turn",
				step: "detail.kind.step",
				error: "detail.kind.error",
				other: "detail.kind.other"
			},
			gap: { label: "detail.gap.label" },
			filter: {
				conversation: "detail.filter.conversation",
				all: "detail.filter.all",
				hiddenNotice: "detail.filter.hiddenNotice"
			},
			timeline: {
				loadMore: "detail.timeline.loadMore",
				loadingMore: "detail.timeline.loadingMore",
				noMore: "detail.timeline.noMore",
				expand: "detail.timeline.expand",
				collapse: "detail.timeline.collapse",
				newBadge: "detail.timeline.newBadge",
				seq: "detail.timeline.seq",
				hiddenNotice: "detail.timeline.hiddenNotice",
				showAll: "detail.timeline.showAll",
				chunkRun: "detail.timeline.chunkRun"
			},
			states: {
				loadingTitle: "detail.states.loadingTitle",
				emptyTitle: "detail.states.emptyTitle",
				emptyHint: "detail.states.emptyHint",
				errorTitle: "detail.states.errorTitle",
				errorFallback: "detail.states.errorFallback",
				errors: {
					session_not_found: "detail.states.errors.session_not_found",
					invalid_cursor: "detail.states.errors.invalid_cursor",
					fusion_not_wired: "detail.states.errors.fusion_not_wired",
					network_error: "detail.states.errors.network_error",
					request_timeout: "detail.states.errors.request_timeout"
				}
			},
			time: {
				justNow: "detail.time.justNow",
				minutesAgo: "detail.time.minutesAgo",
				hoursAgo: "detail.time.hoursAgo",
				daysAgo: "detail.time.daysAgo"
			}
		});
		//#endregion
		//#region src/client/detail/logic.ts
		/**
		* Pure view-model logic for the session-detail timeline view (design §5.1
		* view 2, §5.3 honest presentation). No React, no I/O, no imports from the
		* data layer — everything arrives and leaves as plain values, so this whole
		* module is unit-testable in a bare node environment (same posture as
		* board/logic.ts and inject/logic.ts).
		*
		* Decoupling contract (T5.3 ↔ S7 integration): the `*Wire` types below are
		* this module's OWN hand-written mirrors of the host M3 response shapes
		* (src/routes.ts `timelineBody` over src/fusion.ts `TimelinePage`) — the
		* client TS program cannot import host modules. The integration layer owns
		* transport and feeds pages in; this module owns accumulation, dedup,
		* ordering, gap detection and presentation derivation.
		*
		* Honesty invariants implemented here (design §4.b.3 / §5.3):
		* - a seq discontinuity between consecutive seq-carrying entries inserts a
		*   visible gap marker row (「缺口:可能有 N 条事件未捕获」); seq-less
		*   entries are skipped by the detector and can never fake or mask a gap;
		* - page provenance (`sources`) is surfaced as badges, never dropped;
		* - listen-mode merges only ever append facts (dedup by entry identity),
		*   and newly appended entries are reported for highlight, not invented.
		*
		* @module
		*/
		const MINUTE_MS = 6e4;
		const HOUR_MS = 36e5;
		const DAY_MS = 864e5;
		const KEY_SEP = "\0";
		/** Resolve `{name}` placeholders in a message template. */
		function formatTemplate$1(template, params) {
			return template.replace(/\{(\w+)\}/g, (match, key) => {
				const value = params[key];
				return value === void 0 ? match : String(value);
			});
		}
		/**
		* Coarse relative time: <60s (including clock skew into the future) is
		* 刚刚, then whole minutes/hours/days. Non-finite input renders empty.
		*/
		function formatRelativeTime(thenMs, nowMs) {
			if (!Number.isFinite(thenMs)) return "";
			const delta = nowMs - thenMs;
			if (delta < MINUTE_MS) return DETAIL_STRINGS.time.justNow;
			if (delta < HOUR_MS) return formatTemplate$1(DETAIL_STRINGS.time.minutesAgo, { n: Math.floor(delta / MINUTE_MS) });
			if (delta < DAY_MS) return formatTemplate$1(DETAIL_STRINGS.time.hoursAgo, { n: Math.floor(delta / HOUR_MS) });
			return formatTemplate$1(DETAIL_STRINGS.time.daysAgo, { n: Math.floor(delta / DAY_MS) });
		}
		function pad2(n) {
			return n < 10 ? `0${n}` : String(n);
		}
		/**
		* Absolute short timestamp for the time column (UX-13): the same local
		* calendar day renders「HH:mm」, anything older (or a different day)
		* renders「MM-DD HH:mm」— so a column of same-age rows stays tellable
		* apart, unlike the coarse relative buckets. The format rule is
		* copy-implemented to match the board column (no cross-surface import);
		* the full ISO timestamp stays in the hover title.
		*/
		function formatEventTime(thenMs, nowMs) {
			if (!Number.isFinite(thenMs) || !Number.isFinite(nowMs)) return "";
			const then = new Date(thenMs);
			const now = new Date(nowMs);
			const hhmm = `${pad2(then.getHours())}:${pad2(then.getMinutes())}`;
			return then.getFullYear() === now.getFullYear() && then.getMonth() === now.getMonth() && then.getDate() === now.getDate() ? hhmm : `${pad2(then.getMonth() + 1)}-${pad2(then.getDate())} ${hhmm}`;
		}
		const KIND_GLYPHS = {
			user: "▷",
			assistant: "◁",
			thinking: "…",
			toolCall: "⚙",
			toolResult: "↩",
			turn: "§",
			step: "·",
			error: "✕",
			other: "•"
		};
		/** One path segment → family token, or null when it names no family. */
		function segmentToken(segment) {
			if (segment === "user") return "user";
			if (segment === "assistant") return "assistant";
			if (segment === "thinking" || segment === "reasoning") return "thinking";
			if (segment === "tool_call" || segment === "tool-call" || segment === "toolcall") return "toolCall";
			if (segment === "tool_result" || segment === "tool-result" || segment === "toolresult") return "toolResult";
			if (segment.startsWith("turn_") || segment === "turn") return "turn";
			if (segment.startsWith("step_") || segment === "step") return "step";
			if (segment === "error") return "error";
			return null;
		}
		/**
		* Map a raw event kind onto the glyph/label vocabulary. Covers the sidecar
		* normalized kinds — user/assistant/thinking/tool_call/tool_result plus
		* the turn_ and step_ prefixes (sidecar/model.py) — and dsh native
		* slash-path types in BOTH orders: `message/user` style (family last) and
		* the observed `user/message` / `assistant/chunk` / `turn/start` style
		* (family first; live-data fact, UX-03 — without it the conversation
		* filter would misfile real dsh user messages as protocol noise). The
		* last segment stays authoritative; the first is only a fallback.
		* Everything else is honestly 'other'.
		*/
		function classifyKind(kind) {
			const k = kind.trim().toLowerCase();
			const segments = k.split("/");
			const last = segmentToken(segments[segments.length - 1] ?? k);
			if (last !== null) return last;
			if (segments.length > 1) {
				const first = segmentToken(segments[0] ?? "");
				if (first !== null) return first;
			}
			return "other";
		}
		/** Glyph for a kind token. */
		function kindGlyph(token) {
			return KIND_GLYPHS[token];
		}
		/**
		* Display label: the Chinese vocabulary for recognized kinds; unknown kinds
		* keep their raw text (the view never invents a category, design §5.3).
		*/
		function kindLabel(token, rawKind) {
			if (token === "other") {
				const trimmed = rawKind.trim();
				return trimmed === "" ? DETAIL_STRINGS.kind.other : trimmed;
			}
			return DETAIL_STRINGS.kind[token];
		}
		/** Expanded-body cap so a single pathological entry cannot freeze the tab. */
		const BODY_MAX_CHARS = 4e3;
		/**
		* Dedup identity of one wire entry within a session timeline. Seq-carrying
		* entries collapse on seq+kind+text — NOT seq alone: one dsh record can
		* normalize into several sibling events sharing one seq (reasoning+text
		* blocks, multi-block messages), and a seq-only key would silently fold
		* them away (F1). Same rule as fusion.ts `sidecarEventKey`, so a client
		* multi-page merge matches the host's single-page merge. Seq-less entries
		* fall back to ts+kind+text.
		*/
		function entryKey(entry) {
			return entry.seq !== null ? `s:${entry.seq}${KEY_SEP}${entry.kind}${KEY_SEP}${entry.text}` : `t:${entry.ts}${KEY_SEP}${entry.kind}${KEY_SEP}${entry.text}`;
		}
		const ELLIPSIS = "…";
		function truncateChars(text, max) {
			return text.length <= max ? text : `${text.slice(0, max)}${ELLIPSIS}`;
		}
		/** Best-effort primary text of an entry: normalized text, else common data fields. */
		function extractBaseText(entry) {
			if (entry.text !== "") return entry.text;
			const data = entry.data;
			if (typeof data === "string") return data;
			if (typeof data === "object" && data !== null && !Array.isArray(data)) {
				const record = data;
				for (const field of [
					"text",
					"title",
					"content",
					"message"
				]) {
					const value = record[field];
					if (typeof value === "string" && value !== "") return value;
				}
			}
			return "";
		}
		function safePrettyJson(value) {
			try {
				const json = JSON.stringify(value, null, 2);
				return typeof json === "string" ? json : null;
			} catch {
				return null;
			}
		}
		/**
		* Wire entry → render-ready view model. Summary is the first line of the
		* best-effort text; the expandable body is the full text when the summary
		* truncated it, else the pretty-printed dsh `data` payload when one exists
		* (both bounded by {@link BODY_MAX_CHARS}).
		*/
		function normalizeTimelineEntry(entry) {
			const token = classifyKind(entry.kind);
			const baseText = extractBaseText(entry);
			const summary = truncateChars((baseText.split("\n", 1)[0] ?? "").trim(), 120);
			let body = null;
			if (baseText.trim() !== "" && baseText.trim() !== summary) body = truncateChars(baseText, BODY_MAX_CHARS);
			else if (entry.data !== void 0) {
				const json = safePrettyJson(entry.data);
				if (json !== null && json !== summary) body = truncateChars(json, BODY_MAX_CHARS);
			}
			return {
				key: entryKey(entry),
				origin: entry.origin,
				seq: entry.seq,
				ts: entry.ts,
				kindRaw: entry.kind,
				kind: token,
				glyph: kindGlyph(token),
				label: kindLabel(token, entry.kind),
				summary,
				body,
				expandable: body !== null
			};
		}
		/**
		* Sort entries the way the host merges them: seq-carrying entries keep
		* exact seq order among themselves, seq-less entries sort by ts and are
		* interleaved by ts (tie: seq domain first). This keeps a client-side
		* multi-page merge byte-identical to what one giant host page would be.
		* Within one seq (sibling block events of one dsh record) the dsh entry
		* sorts first — mirroring the host merge — and sidecar siblings keep
		* their arrival order (the sort is stable).
		*/
		function sortTimelineEntries(entries) {
			const seqDomain = entries.filter((e) => e.seq !== null);
			const unseqed = entries.filter((e) => e.seq === null);
			seqDomain.sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0) || (a.origin === b.origin ? 0 : a.origin === "dsh" ? -1 : 1));
			unseqed.sort((a, b) => a.ts - b.ts || a.key.localeCompare(b.key));
			const out = [];
			let i = 0;
			let j = 0;
			for (;;) {
				const a = seqDomain[i];
				const b = unseqed[j];
				if (a === void 0 && b === void 0) break;
				if (b === void 0 || a !== void 0 && a.ts <= b.ts) {
					if (a !== void 0) {
						out.push(a);
						i += 1;
					}
				} else {
					out.push(b);
					j += 1;
				}
			}
			return out;
		}
		/** Fresh empty state for a session. */
		function createTimelineVM(sessionId) {
			return {
				sessionId,
				entries: [],
				sources: {
					dshLive: false,
					dshCold: false,
					sidecarReplay: false,
					sidecarBuffer: false
				},
				nextCursor: null,
				reachedStart: false,
				newKeys: []
			};
		}
		function unionSources(a, b) {
			return {
				dshLive: a.dshLive || b.dshLive,
				dshCold: a.dshCold || b.dshCold,
				sidecarReplay: a.sidecarReplay || b.sidecarReplay,
				sidecarBuffer: a.sidecarBuffer || b.sidecarBuffer
			};
		}
		/** (seq, kind) twin-slot sub-key of the convergence maps in mergeEntries. */
		function seqKindKey(seq, kind) {
			return `${seq}${KEY_SEP}${kind}`;
		}
		/** The key an un-supplemented dsh entry (empty wire text) carries. */
		function emptyTextKey(seq, kind) {
			return `s:${seq}${KEY_SEP}${kind}${KEY_SEP}`;
		}
		/**
		* Dedup-by-key merge, then host-order sort.
		*
		* Cross-page convergence: the host folds the first sidecar twin's text
		* into the matching dsh entry, so the SAME event can arrive with
		* `text: ''` in one page (twin not yet observed) and with the folded
		* text in a later one — two different keys for one event. A text-carrying
		* arrival therefore upgrades the empty-text (seq, kind) slot in place,
		* and an empty-text arrival for an already-supplemented (seq, kind) is
		* dropped as stale. This keeps the accumulated client merge equal to the
		* host's latest single-page merge instead of duplicating the entry.
		* Genuine same-seq siblings always carry distinct kind/text and are
		* untouched by the rule.
		*/
		function mergeEntries(existing, incoming) {
			const entries = [...existing];
			const seen = new Set(entries.map((e) => e.key));
			const emptySlot = /* @__PURE__ */ new Map();
			const filled = /* @__PURE__ */ new Set();
			for (let i = 0; i < entries.length; i += 1) {
				const e = entries[i];
				if (e === void 0 || e.seq === null) continue;
				const sk = seqKindKey(e.seq, e.kindRaw);
				if (e.key === emptyTextKey(e.seq, e.kindRaw)) emptySlot.set(sk, i);
				else filled.add(sk);
			}
			const appendedKeys = [];
			let changed = false;
			for (const wire of incoming) {
				const vm = normalizeTimelineEntry(wire);
				if (seen.has(vm.key)) continue;
				if (vm.seq !== null) {
					const sk = seqKindKey(vm.seq, vm.kindRaw);
					if (wire.text === "") {
						if (filled.has(sk)) continue;
						emptySlot.set(sk, entries.length);
					} else {
						filled.add(sk);
						const slot = emptySlot.get(sk);
						if (slot !== void 0) {
							entries[slot] = vm;
							emptySlot.delete(sk);
							seen.add(vm.key);
							appendedKeys.push(vm.key);
							changed = true;
							continue;
						}
					}
				}
				seen.add(vm.key);
				entries.push(vm);
				appendedKeys.push(vm.key);
				changed = true;
			}
			if (!changed) return {
				entries,
				appendedKeys: []
			};
			return {
				entries: sortTimelineEntries(entries),
				appendedKeys
			};
		}
		/**
		* Merge one HISTORY page (the initial newest page, or an older page fetched
		* via `nextCursor`) into the state. Advances the pagination token to the
		* page's own `nextCursor` and never marks entries as new (paging back is
		* not fresh activity). Duplicate entries across overlapping pages dedup on
		* {@link entryKey}.
		*/
		function applyTimelinePage(vm, page) {
			const merged = mergeEntries(vm.entries, page.entries);
			return {
				...vm,
				entries: merged.entries,
				sources: unionSources(vm.sources, page.sources),
				nextCursor: page.nextCursor,
				reachedStart: page.nextCursor === null,
				newKeys: vm.newKeys
			};
		}
		/**
		* Merge one LISTEN update (the newest page refetched after an SSE `state`
		* signal — stream snapshots carry no per-session events, so listen mode is
		* snapshot-trigger + timeline-refetch, ADR-2/ADR-3). Appended entries are
		* reported in `newKeys` for highlight; replays of already-known entries
		* dedup silently and do NOT re-highlight. The pagination token is left
		* alone: a newest-window page must never reset the older-history cursor.
		*/
		function applyListenPage(vm, page) {
			const merged = mergeEntries(vm.entries, page.entries);
			return {
				...vm,
				entries: merged.entries,
				sources: unionSources(vm.sources, page.sources),
				newKeys: merged.appendedKeys
			};
		}
		function isoOrEmpty(ts) {
			if (!Number.isFinite(ts)) return "";
			try {
				return new Date(ts).toISOString();
			} catch {
				return "";
			}
		}
		function eventHoverTitle(entry, nowMs) {
			const parts = [
				isoOrEmpty(entry.ts),
				entry.kindRaw,
				entry.origin
			];
			if (entry.seq !== null) parts.push(formatTemplate$1(DETAIL_STRINGS.timeline.seq, { n: entry.seq }));
			parts.push(formatRelativeTime(entry.ts, nowMs));
			return parts.filter((p) => p !== "").join(" · ");
		}
		/** Gap marker text: 「缺口:可能有 N 条事件未捕获(256 队列上限或未持久化)」. */
		function gapLabel(missingCount) {
			return formatTemplate$1(DETAIL_STRINGS.gap.label, { n: missingCount });
		}
		/**
		* Derive render rows from the accumulated state, inserting a gap marker
		* wherever consecutive seq-carrying entries jump by more than 1 (honest
		* presentation of the 256-slot subscribe queue drop / unpersisted events,
		* design §4.b.3). Seq-less entries are transparent to the detector: they
		* neither trigger a gap nor reset the last-seen seq, so mixed timelines
		* cannot produce false positives. Nothing is inserted before the first
		* seq entry — unloaded older history is pagination, not a gap.
		*/
		function buildTimelineRows(vm, nowMs) {
			const newKeys = new Set(vm.newKeys);
			const rows = [];
			let lastSeq = null;
			for (const entry of vm.entries) {
				if (entry.seq !== null) {
					if (lastSeq !== null && entry.seq > lastSeq + 1) {
						const missing = entry.seq - lastSeq - 1;
						rows.push({
							type: "gap",
							key: `gap:${lastSeq}-${entry.seq}`,
							missingCount: missing,
							label: gapLabel(missing)
						});
					}
					lastSeq = entry.seq;
				}
				rows.push({
					type: "event",
					key: entry.key,
					entry,
					timeLabel: formatEventTime(entry.ts, nowMs),
					hoverTitle: eventHoverTitle(entry, nowMs),
					isNew: newKeys.has(entry.key)
				});
			}
			return rows;
		}
		/** The kinds that count as conversation (review UX-03 vocabulary). */
		const CONVERSATION_KINDS = /* @__PURE__ */ new Set([
			"user",
			"assistant",
			"error"
		]);
		/**
		* Kind filter over derived rows: 'conversation' keeps user/assistant/error
		* events only; 'all' passes everything through. Gap markers ALWAYS stay —
		* honesty rows are not noise and hiding them could fake a clean timeline.
		* Runs BEFORE {@link aggregateChunkRows} (gaps are detected on the full
		* entry list upstream, so filtering can never fabricate a gap).
		*/
		function filterTimelineRows(rows, mode) {
			if (mode === "all") return {
				rows: [...rows],
				hiddenCount: 0
			};
			const out = [];
			let hiddenCount = 0;
			for (const row of rows) {
				if (row.type === "event" && !CONVERSATION_KINDS.has(row.entry.kind)) {
					hiddenCount += 1;
					continue;
				}
				out.push(row);
			}
			return {
				rows: out,
				hiddenCount
			};
		}
		/**
		* True for a protocol streaming-chunk entry: an empty one-line summary and
		* a chunk-flavored kind (`assistant/chunk` style, matched on the last
		* slash segment). These are the rows that drowned real conversation in
		* the walkthrough (18 of 38 rows, review UX-03).
		*/
		function isStreamChunkEntry(entry) {
			if (entry.summary !== "") return false;
			const k = entry.kindRaw.trim().toLowerCase();
			const last = k.includes("/") ? k.split("/").pop() ?? k : k;
			return last === "chunk" || last.endsWith("_chunk") || last.endsWith("-chunk");
		}
		/**
		* Collapse each maximal run of ≥2 adjacent same-kind streaming-chunk rows
		* into one 'chunks' row carrying the members verbatim (lossless — the view
		* offers 展开). Gap markers and any non-chunk row break a run, so the
		* aggregation can never paper over a seq discontinuity. Single chunks stay
		* as plain rows (a 1-run header would add noise, not remove it).
		*/
		function aggregateChunkRows(rows) {
			const out = [];
			let run = [];
			const flush = () => {
				if (run.length >= 2) {
					const first = run[0];
					const last = run[run.length - 1];
					out.push({
						type: "chunks",
						key: `chunks:${first.key}`,
						kindRaw: first.entry.kindRaw,
						count: run.length,
						label: formatTemplate$1(DETAIL_STRINGS.timeline.chunkRun, { n: run.length }),
						timeLabel: last.timeLabel,
						hoverTitle: `${first.entry.kindRaw} ×${run.length}`,
						isNew: run.some((r) => r.isNew),
						members: run
					});
				} else out.push(...run);
				run = [];
			};
			for (const row of rows) {
				if (row.type === "event" && isStreamChunkEntry(row.entry)) {
					if (!(run.length === 0 || run[run.length - 1].entry.kindRaw === row.entry.kindRaw)) flush();
					run.push(row);
					continue;
				}
				flush();
				out.push(row);
			}
			flush();
			return out;
		}
		/**
		* Whether the view should pin its scroll position to the newest rows:
		* on the first non-empty render (initial landing — understanding the
		* current context needs the latest events, review UX-04), and on every
		* append while listen mode is on. Loading older history must never yank
		* the viewport (`positioned` stays true after the first landing).
		*/
		function shouldStickToLatest(input) {
			if (input.entryCount === 0) return false;
			return !input.positioned || input.listening;
		}
		/**
		* Keep only the newest `max` rows (the tail — listen mode appends there).
		* The data stays in the VM; this is purely a render bound the component
		* can lift via its「全部显示」toggle.
		*/
		function limitTimelineRows(rows, max = 400) {
			if (!Number.isFinite(max) || max <= 0 || rows.length <= max) return {
				rows: [...rows],
				hiddenCount: 0,
				notice: null
			};
			const hiddenCount = rows.length - max;
			return {
				rows: rows.slice(hiddenCount),
				hiddenCount,
				notice: formatTemplate$1(DETAIL_STRINGS.timeline.hiddenNotice, { n: hiddenCount })
			};
		}
		const SOURCE_ORDER = [
			{
				id: "dshLive",
				tone: "success"
			},
			{
				id: "dshCold",
				tone: "neutral"
			},
			{
				id: "sidecarReplay",
				tone: "neutral"
			},
			{
				id: "sidecarBuffer",
				tone: "muted"
			}
		];
		/**
		* Provenance badges for the header, stable order: dsh 实时 → dsh 冷读 →
		* sidecar 重放 → sidecar 缓冲. Only contributing sources appear; an
		* all-false set yields an empty list (the empty state explains itself).
		*/
		function deriveSourceBadges(sources) {
			const out = [];
			for (const { id, tone } of SOURCE_ORDER) if (sources[id]) out.push({
				id,
				label: DETAIL_STRINGS.sources[id],
				tone
			});
			return out;
		}
		const KNOWN_STATUSES = [
			"working",
			"waiting",
			"idle",
			"dead"
		];
		const STATUS_TONE = {
			working: "success",
			waiting: "warn",
			idle: "neutral",
			unknown: "neutral",
			dead: "muted"
		};
		/**
		* Raw observed status → badge. Unknown raw statuses keep their raw text as
		* the label (the view never invents a state); empty raw text reads 未知.
		*/
		function deriveDetailStatus(rawStatus) {
			const cleaned = rawStatus.trim().toLowerCase();
			const status = KNOWN_STATUSES.includes(cleaned) ? cleaned : "unknown";
			const trimmed = rawStatus.trim();
			const label = status === "unknown" ? trimmed === "" ? DETAIL_STRINGS.status.unknown : trimmed : DETAIL_STRINGS.status[status];
			return {
				status,
				tone: STATUS_TONE[status],
				label
			};
		}
		/** Single-character agent marker (board vocabulary; unknown → neutral dot). */
		const AGENT_GLYPHS = {
			dsh: "◆",
			claude: "✳",
			codex: "▣",
			cursor: "▮",
			"cursor-cli": "▮",
			"cursor-ide": "▮",
			copilot: "◉",
			kimi: "◐"
		};
		function agentGlyph(agent) {
			return AGENT_GLYPHS[agent.trim().toLowerCase()] ?? "●";
		}
		/**
		* Friendly text for a machine error reason (ApiError.reason / server
		* `{reason}` codes). Unknown codes fall back to an honest 错误码 template.
		*/
		function detailErrorText(reason) {
			return DETAIL_STRINGS.states.errors[reason] ?? formatTemplate$1(DETAIL_STRINGS.states.errorFallback, { reason });
		}
		/**
		* Body-state resolution, honest-by-priority:
		* - entries present → always 'list' (never hide data already shown); a
		*   concurrent error surfaces as an inline banner instead;
		* - no entries + error → 'error' (mapped text);
		* - no entries + loading → 'loading';
		* - otherwise → 'empty'.
		*/
		function deriveDetailBodyState(input) {
			if (input.entryCount > 0) return {
				kind: "list",
				title: null,
				hint: null,
				errorBanner: input.error === null ? null : detailErrorText(input.error)
			};
			if (input.error !== null) return {
				kind: "error",
				title: DETAIL_STRINGS.states.errorTitle,
				hint: detailErrorText(input.error),
				errorBanner: null
			};
			if (input.loading) return {
				kind: "loading",
				title: DETAIL_STRINGS.states.loadingTitle,
				hint: null,
				errorBanner: null
			};
			return {
				kind: "empty",
				title: DETAIL_STRINGS.states.emptyTitle,
				hint: DETAIL_STRINGS.states.emptyHint,
				errorBanner: null
			};
		}
		//#endregion
		//#region src/client/primitives/StaticPill.tsx
		/**
		* Preserve native span attributes around the rc.2 Pill static branch, which
		* currently omits its rest props. The inner primitive remains the sole owner
		* of the DSH pill visuals.
		*/
		function StaticPill({ className, children, ...rest }) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
				className,
				...rest,
				children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, { children })
			});
		}
		//#endregion
		//#region \0dsh-css:src/client/detail/detail.module.css.mjs
		const css$7 = ".cBtswW_root{box-sizing:border-box;height:100%;min-height:420px;color:var(--agsc-fg);background:var(--agsc-bg);flex-direction:column;gap:10px;padding:16px 20px;display:flex}.cBtswW_header{flex-direction:column;gap:6px;display:flex}.cBtswW_headerTop{flex-wrap:wrap;align-items:center;gap:10px;display:flex}.cBtswW_agent{color:var(--dsw-alias-label-primary);align-items:center;gap:5px;font-size:13px;font-weight:650;display:inline-flex}.cBtswW_agentGlyph{color:var(--dsw-alias-brand-primary);flex:none}.cBtswW_badge{white-space:nowrap}.cBtswW_dot{background:var(--agsc-fg-dimmed);border-radius:50%;flex:none;width:8px;height:8px}.cBtswW_dot[data-tone=success]{background:var(--agsc-ok)}.cBtswW_dot[data-tone=warn]{background:var(--agsc-warn)}.cBtswW_dot[data-tone=danger]{background:var(--agsc-err)}.cBtswW_dot[data-tone=neutral]{background:var(--agsc-fg-secondary)}.cBtswW_dot[data-tone=muted]{background:var(--agsc-fg-dimmed)}.cBtswW_spacer{flex:1}.cBtswW_title{color:var(--dsw-alias-label-primary);text-overflow:ellipsis;white-space:nowrap;font-size:15px;font-weight:600;overflow:hidden}.cBtswW_meta{align-items:baseline;gap:10px;min-width:0;display:flex}.cBtswW_project{color:var(--dsw-alias-label-secondary);text-overflow:ellipsis;white-space:nowrap;font-size:12px;overflow:hidden}.cBtswW_sessionId{font-size:11px;font-family:var(--ds-font-family-code);color:var(--dsw-alias-label-tertiary);text-overflow:ellipsis;white-space:nowrap;flex:none;max-width:40%;overflow:hidden}.cBtswW_sessionId:hover{color:var(--dsw-alias-label-primary);text-decoration:underline}.cBtswW_copiedBubble{color:var(--agsc-ok);flex:none}.cBtswW_copiedBubble>span,.cBtswW_sourceBadge>span{color:inherit}.cBtswW_metaRow{flex-wrap:wrap;align-items:center;gap:10px;display:flex}.cBtswW_disclaimer{color:var(--dsw-alias-label-caption);font-size:11px}.cBtswW_sourceList{align-items:center;gap:4px;display:inline-flex}.cBtswW_sourceBadge{white-space:nowrap}.cBtswW_sourceBadge[data-tone=success]{color:var(--agsc-ok)}.cBtswW_sourceBadge[data-tone=muted]{color:var(--agsc-fg-dimmed)}.cBtswW_banner{border-radius:var(--agsc-radius-control);color:var(--agsc-warn);background:var(--dsw-alias-state-warn-tertiary);border:1px solid var(--dsw-alias-state-warn-secondary);padding:6px 10px;font-size:12px;line-height:18px}.cBtswW_bodyState{text-align:center;flex-direction:column;flex:1;justify-content:center;align-items:center;gap:6px;min-height:160px;padding:24px;display:flex}.cBtswW_bodyStateTitle{color:var(--dsw-alias-label-primary);font-size:14px;font-weight:600}.cBtswW_bodyState[data-kind=error] .cBtswW_bodyStateTitle{color:var(--agsc-err)}.cBtswW_bodyStateHint{max-width:420px;color:var(--dsw-alias-label-secondary);font-size:12px;line-height:20px}.cBtswW_filterRow{flex-wrap:wrap;align-items:center;gap:6px;display:flex}.cBtswW_filterHiddenNote{color:var(--dsw-alias-label-tertiary);font-size:11px}.cBtswW_pager{justify-content:center;display:flex}.cBtswW_pagerNote{color:var(--dsw-alias-label-tertiary);font-size:11px}.cBtswW_hiddenNotice{color:var(--dsw-alias-label-tertiary);justify-content:center;align-items:center;gap:8px;font-size:11px;display:flex}.cBtswW_timeline{flex-direction:column;flex:1;gap:6px;margin:0;padding:0 2px 8px 0;list-style:none;display:flex;overflow-y:auto}.cBtswW_event{border-radius:var(--agsc-radius-control);border:1px solid var(--agsc-border);background:var(--agsc-bg-raised);flex-direction:column;gap:4px;padding:6px 10px;display:flex}.cBtswW_event[data-new]{border-color:var(--agsc-accent);background:color-mix(in srgb, var(--agsc-accent) 6%, transparent)}.cBtswW_chunkRun{border-radius:var(--agsc-radius-control);color:var(--dsw-alias-label-tertiary);background:var(--agsc-bg-raised);border:1px dashed var(--agsc-border-strong);align-items:center;gap:8px;padding:4px 10px;font-size:11px;line-height:16px;display:flex}.cBtswW_chunkRun[data-new]{border-color:var(--agsc-accent)}.cBtswW_chunkRunLabel{flex:none}.cBtswW_eventHead{align-items:center;gap:6px;min-width:0;display:flex}.cBtswW_eventGlyph{color:var(--dsw-alias-label-tertiary);flex:none;font-size:12px}.cBtswW_event[data-kind=user] .cBtswW_eventGlyph,.cBtswW_event[data-kind=assistant] .cBtswW_eventGlyph{color:var(--dsw-alias-brand-primary)}.cBtswW_event[data-kind=error] .cBtswW_eventGlyph{color:var(--dsw-alias-state-error-primary)}.cBtswW_eventLabel{color:var(--dsw-alias-label-primary);white-space:nowrap;text-overflow:ellipsis;font-size:12px;font-weight:600;overflow:hidden}.cBtswW_eventSeq{font-size:10px;font-family:var(--ds-font-family-code);color:var(--dsw-alias-label-tertiary);flex:none}.cBtswW_eventNew{color:var(--agsc-accent);flex:none}.cBtswW_eventSpacer{flex:1}.cBtswW_eventTime{color:var(--dsw-alias-label-caption);flex:none;font-size:11px}.cBtswW_eventSummary{color:var(--dsw-alias-label-secondary);overflow-wrap:anywhere;font-size:12px;line-height:18px}.cBtswW_expandButton{align-self:flex-start}.cBtswW_eventBody{border-radius:var(--agsc-radius-control);font-size:11px;line-height:16px;font-family:var(--ds-font-family-code);color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-layer-2);white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;margin:0;padding:8px 10px;overflow-y:auto}.cBtswW_gap{border-radius:var(--agsc-radius-control);text-align:center;color:var(--agsc-warn);background:var(--dsw-alias-state-warn-tertiary);border:1px dashed var(--dsw-alias-state-warn-secondary);padding:4px 10px;font-size:11px;line-height:16px}";
		const tagId$7 = "@shendeguize/dsh-agent-sidecar/src/client/detail/detail.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$7, css$7);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$7) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$7;
				created = true;
			}
			tag.textContent = css$7;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var detail_module_css_default = {
			"agent": "cBtswW_agent",
			"agentGlyph": "cBtswW_agentGlyph",
			"badge": "cBtswW_badge",
			"banner": "cBtswW_banner",
			"bodyState": "cBtswW_bodyState",
			"bodyStateHint": "cBtswW_bodyStateHint",
			"bodyStateTitle": "cBtswW_bodyStateTitle",
			"chunkRun": "cBtswW_chunkRun",
			"chunkRunLabel": "cBtswW_chunkRunLabel",
			"copiedBubble": "cBtswW_copiedBubble",
			"disclaimer": "cBtswW_disclaimer",
			"dot": "cBtswW_dot",
			"event": "cBtswW_event",
			"eventBody": "cBtswW_eventBody",
			"eventGlyph": "cBtswW_eventGlyph",
			"eventHead": "cBtswW_eventHead",
			"eventLabel": "cBtswW_eventLabel",
			"eventNew": "cBtswW_eventNew",
			"eventSeq": "cBtswW_eventSeq",
			"eventSpacer": "cBtswW_eventSpacer",
			"eventSummary": "cBtswW_eventSummary",
			"eventTime": "cBtswW_eventTime",
			"expandButton": "cBtswW_expandButton",
			"filterHiddenNote": "cBtswW_filterHiddenNote",
			"filterRow": "cBtswW_filterRow",
			"gap": "cBtswW_gap",
			"header": "cBtswW_header",
			"headerTop": "cBtswW_headerTop",
			"hiddenNotice": "cBtswW_hiddenNotice",
			"meta": "cBtswW_meta",
			"metaRow": "cBtswW_metaRow",
			"pager": "cBtswW_pager",
			"pagerNote": "cBtswW_pagerNote",
			"project": "cBtswW_project",
			"root": "cBtswW_root",
			"sessionId": "cBtswW_sessionId",
			"sourceBadge": "cBtswW_sourceBadge",
			"sourceList": "cBtswW_sourceList",
			"spacer": "cBtswW_spacer",
			"timeline": "cBtswW_timeline",
			"title": "cBtswW_title"
		};
		//#endregion
		//#region src/client/detail/SessionDetail.tsx
		/**
		* Session-detail view: header + merged event timeline (design §5.1 view 2).
		*
		* Presentation-only and fully controlled: no data fetching, no api/sse
		* imports. The integration layer (S7) owns transport and accumulation —
		* it feeds the {@link TimelineVM} built via logic.ts (`applyTimelinePage`
		* for history pages, `applyListenPage` for listen-mode refetches) and
		* handles `onLoadMore` / `onToggleListen` / `onRefresh`.
		*
		* Row pipeline (all pure, logic.ts): buildTimelineRows (gaps on the FULL
		* entry list) → filterTimelineRows (UX-03 kind filter, conversation-first
		* by default with an honest hidden count) → aggregateChunkRows (UX-03
		* adjacent empty streaming chunks collapse into one expandable run) →
		* limitTimelineRows (render cap with a 全部显示 escape hatch).
		*
		* Long-list posture (task report): no full virtualization — history only
		* grows page-by-page on explicit 加载更多, and rendering is additionally
		* capped at {@link DEFAULT_MAX_RENDER_ROWS} newest rows. View-local
		* concerns (expanded bodies/runs, filter mode, the lift-cap flag, the
		* UX-04 initial landing, copy feedback) are component state; everything
		* else comes through props.
		*/
		function EventRow(props) {
			const { row, expanded, onToggleExpand } = props;
			const entry = row.entry;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("li", {
				className: detail_module_css_default["event"],
				"data-kind": entry.kind,
				"data-new": row.isNew || void 0,
				"data-testid": "agent-sidecar-detail-event",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: detail_module_css_default["eventHead"],
						title: row.hoverTitle,
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: detail_module_css_default["eventGlyph"],
								"aria-hidden": true,
								children: entry.glyph
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: detail_module_css_default["eventLabel"],
								children: entry.label
							}),
							entry.seq !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: detail_module_css_default["eventSeq"],
								children: DETAIL_STRINGS.timeline.seq.replace("{n}", String(entry.seq))
							}),
							row.isNew && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
								className: detail_module_css_default["eventNew"],
								children: DETAIL_STRINGS.timeline.newBadge
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { className: detail_module_css_default["eventSpacer"] }),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: detail_module_css_default["eventTime"],
								children: row.timeLabel
							})
						]
					}),
					entry.summary !== "" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: detail_module_css_default["eventSummary"],
						children: entry.summary
					}),
					entry.expandable && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
						type: "button",
						size: "sm",
						variant: "ghost",
						className: detail_module_css_default["expandButton"],
						"aria-expanded": expanded,
						onClick: () => onToggleExpand(entry.key),
						children: expanded ? DETAIL_STRINGS.timeline.collapse : DETAIL_STRINGS.timeline.expand
					}),
					entry.expandable && expanded && entry.body !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", {
						className: detail_module_css_default["eventBody"],
						children: entry.body
					})
				]
			});
		}
		/** Collapsed run of adjacent streaming chunks (UX-03); expandable lossless. */
		function ChunkRunRow(props) {
			const { row } = props;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("li", {
				className: detail_module_css_default["chunkRun"],
				"data-new": row.isNew || void 0,
				title: row.hoverTitle,
				"data-testid": "agent-sidecar-detail-chunks",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: detail_module_css_default["chunkRunLabel"],
						children: row.label
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
						type: "button",
						size: "sm",
						variant: "ghost",
						className: detail_module_css_default["expandButton"],
						"aria-expanded": props.expanded,
						onClick: () => props.onToggleRun(row.key),
						children: props.expanded ? DETAIL_STRINGS.timeline.collapse : DETAIL_STRINGS.timeline.expand
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { className: detail_module_css_default["eventSpacer"] }),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: detail_module_css_default["eventTime"],
						children: row.timeLabel
					})
				]
			}), props.expanded && row.members.map((member) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(EventRow, {
				row: member,
				expanded: props.expandedKeys.has(member.key),
				onToggleExpand: props.onToggleExpand
			}, member.key))] });
		}
		/** The session-detail view. Pure render of the logic.ts pipelines over props. */
		function SessionDetail(props) {
			const nowMs = props.nowMs ?? Date.now();
			const [expandedKeys, setExpandedKeys] = (0, react.useState)(/* @__PURE__ */ new Set());
			const [expandedRuns, setExpandedRuns] = (0, react.useState)(/* @__PURE__ */ new Set());
			const [filterMode, setFilterMode] = (0, react.useState)("conversation");
			const [renderAll, setRenderAll] = (0, react.useState)(false);
			const [copied, setCopied] = (0, react.useState)(false);
			const listRef = (0, react.useRef)(null);
			const positionedRef = (0, react.useRef)(false);
			const copyTimerRef = (0, react.useRef)(null);
			const copyAliveRef = (0, react.useRef)(true);
			const status = deriveDetailStatus(props.header.status);
			const sourceBadges = deriveSourceBadges(props.timeline.sources);
			const bodyState = deriveDetailBodyState({
				loading: props.loading,
				error: props.error,
				entryCount: props.timeline.entries.length
			});
			const filtered = filterTimelineRows(buildTimelineRows(props.timeline, nowMs), filterMode);
			const aggregated = aggregateChunkRows(filtered.rows);
			const limited = renderAll ? {
				rows: aggregated,
				hiddenCount: 0,
				notice: null
			} : limitTimelineRows(aggregated, props.maxRenderRows ?? 400);
			const entryCount = props.timeline.entries.length;
			const listening = props.listening;
			(0, react.useEffect)(() => {
				const list = listRef.current;
				if (list === null) return;
				if (shouldStickToLatest({
					entryCount,
					positioned: positionedRef.current,
					listening
				})) {
					list.scrollTop = list.scrollHeight;
					positionedRef.current = true;
				}
			}, [listening, entryCount]);
			(0, react.useEffect)(() => {
				copyAliveRef.current = true;
				return () => {
					copyAliveRef.current = false;
					if (copyTimerRef.current !== null) {
						clearTimeout(copyTimerRef.current);
						copyTimerRef.current = null;
					}
				};
			}, []);
			const toggleExpand = (key) => {
				setExpandedKeys((prev) => {
					const next = new Set(prev);
					if (next.has(key)) next.delete(key);
					else next.add(key);
					return next;
				});
			};
			const toggleRun = (key) => {
				setExpandedRuns((prev) => {
					const next = new Set(prev);
					if (next.has(key)) next.delete(key);
					else next.add(key);
					return next;
				});
			};
			const copySessionId = async () => {
				if (!await (0, _deepseek_ai_dsh_client_ui_primitives.writeClipboard)(props.sessionId)) return;
				if (!copyAliveRef.current) return;
				if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
				setCopied(true);
				copyTimerRef.current = setTimeout(() => {
					copyTimerRef.current = null;
					setCopied(false);
				}, 2e3);
			};
			const statusDotState = status.status === "working" ? "ongoing" : status.status === "waiting" ? "warning" : null;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				...surfaceProps("timeline", detail_module_css_default["root"]),
				"data-testid": "agent-sidecar-detail",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("header", {
						className: detail_module_css_default["header"],
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: detail_module_css_default["headerTop"],
								children: [
									props.onClose !== void 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
										type: "button",
										size: "sm",
										variant: "outline",
										onClick: props.onClose,
										children: DETAIL_STRINGS.header.close
									}),
									/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
										className: detail_module_css_default["agent"],
										children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
											className: detail_module_css_default["agentGlyph"],
											"aria-hidden": true,
											children: agentGlyph(props.header.agent)
										}), props.header.agent]
									}),
									/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(StaticPill, {
										className: detail_module_css_default["badge"],
										title: DETAIL_STRINGS.header.observedDisclaimer,
										children: [statusDotState === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
											className: detail_module_css_default["dot"],
											"data-tone": status.tone
										}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
											state: statusDotState,
											size: 8
										}), status.label]
									}),
									/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { className: detail_module_css_default["spacer"] }),
									props.onRefresh !== void 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
										type: "button",
										size: "sm",
										variant: "outline",
										disabled: props.refreshing === true,
										title: DETAIL_STRINGS.header.refreshHint,
										onClick: props.onRefresh,
										"data-testid": "agent-sidecar-detail-refresh",
										children: props.refreshing === true ? DETAIL_STRINGS.header.refreshing : DETAIL_STRINGS.header.refresh
									}),
									/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
										type: "button",
										active: props.listening,
										"aria-pressed": props.listening,
										title: DETAIL_STRINGS.header.listenHint,
										onClick: props.onToggleListen,
										children: props.listening ? DETAIL_STRINGS.header.listenOn : DETAIL_STRINGS.header.listenOff
									})
								]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
								className: detail_module_css_default["title"],
								title: props.header.title,
								children: props.header.title.trim() === "" ? DETAIL_STRINGS.header.untitled : props.header.title
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: detail_module_css_default["meta"],
								children: [
									/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: detail_module_css_default["project"],
										title: props.header.project,
										children: props.header.project.trim() === "" ? DETAIL_STRINGS.header.unknownProject : props.header.project
									}),
									/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
										type: "button",
										size: "sm",
										variant: "ghost",
										className: detail_module_css_default["sessionId"],
										title: `${props.sessionId} · ${DETAIL_STRINGS.header.copyIdTitle}`,
										onClick: () => {
											copySessionId();
										},
										"data-testid": "agent-sidecar-detail-copy-id",
										children: props.sessionId
									}),
									copied && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(StaticPill, {
										className: detail_module_css_default["copiedBubble"],
										role: "status",
										children: DETAIL_STRINGS.header.copied
									})
								]
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: detail_module_css_default["metaRow"],
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: detail_module_css_default["disclaimer"],
									children: DETAIL_STRINGS.header.observedDisclaimer
								}), sourceBadges.length > 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: detail_module_css_default["sourceList"],
									title: DETAIL_STRINGS.sources.title,
									children: sourceBadges.map((badge) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(StaticPill, {
										className: detail_module_css_default["sourceBadge"],
										"data-tone": badge.tone,
										children: badge.label
									}, badge.id))
								})]
							})
						]
					}),
					bodyState.errorBanner !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: detail_module_css_default["banner"],
						role: "alert",
						children: bodyState.errorBanner
					}),
					bodyState.kind !== "list" ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: detail_module_css_default["bodyState"],
						"data-kind": bodyState.kind,
						role: bodyState.kind === "error" ? "alert" : bodyState.kind === "loading" ? "status" : void 0,
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: detail_module_css_default["bodyStateTitle"],
							children: bodyState.title
						}), bodyState.hint !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: detail_module_css_default["bodyStateHint"],
							children: bodyState.hint
						})]
					}) : /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: detail_module_css_default["filterRow"],
							"data-testid": "agent-sidecar-detail-filter",
							children: [["conversation", "all"].map((mode) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
								type: "button",
								active: filterMode === mode,
								"aria-pressed": filterMode === mode,
								onClick: () => {
									setFilterMode(mode);
								},
								children: DETAIL_STRINGS.filter[mode]
							}, mode)), filtered.hiddenCount > 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: detail_module_css_default["filterHiddenNote"],
								children: formatTemplate$1(DETAIL_STRINGS.filter.hiddenNotice, { n: filtered.hiddenCount })
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
							className: detail_module_css_default["pager"],
							children: props.hasMore ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
								type: "button",
								size: "sm",
								variant: "outline",
								disabled: props.loading,
								onClick: props.onLoadMore,
								children: props.loading ? DETAIL_STRINGS.timeline.loadingMore : DETAIL_STRINGS.timeline.loadMore
							}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: detail_module_css_default["pagerNote"],
								children: DETAIL_STRINGS.timeline.noMore
							})
						}),
						limited.notice !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: detail_module_css_default["hiddenNotice"],
							children: [limited.notice, /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
								type: "button",
								size: "sm",
								variant: "ghost",
								onClick: () => setRenderAll(true),
								children: DETAIL_STRINGS.timeline.showAll
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("ol", {
							className: detail_module_css_default["timeline"],
							ref: listRef,
							children: limited.rows.map((row) => row.type === "gap" ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("li", {
								className: detail_module_css_default["gap"],
								role: "note",
								"data-testid": "agent-sidecar-detail-gap",
								children: row.label
							}, row.key) : row.type === "chunks" ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ChunkRunRow, {
								row,
								expanded: expandedRuns.has(row.key),
								onToggleRun: toggleRun,
								expandedKeys,
								onToggleExpand: toggleExpand
							}, row.key) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)(EventRow, {
								row,
								expanded: expandedKeys.has(row.key),
								onToggleExpand: toggleExpand
							}, row.key))
						})
					] })
				]
			});
		}
		//#endregion
		//#region \0dsh-css:src/client/dsh-tools/dsh-tools.module.css.mjs
		const css$6 = "._1R83Ca_panel{min-width:0;color:var(--agsc-fg);flex-direction:column;gap:8px;display:flex}._1R83Ca_panelHead{align-items:baseline;gap:8px;min-width:0;display:flex}._1R83Ca_panelTitle{color:var(--dsw-alias-label-primary);font-size:13px;font-weight:600}._1R83Ca_panelCount{flex:none}._1R83Ca_mutedLine{color:var(--dsw-alias-label-secondary);font-size:12px}._1R83Ca_errorCard{border-radius:var(--agsc-radius-control);color:var(--agsc-err);background:var(--dsw-alias-state-error-secondary);border:1px solid var(--dsw-alias-state-error-secondary);padding:8px 10px;font-size:12px;line-height:18px}._1R83Ca_errorDetail{color:var(--dsw-alias-label-secondary);word-break:break-all;margin-top:2px;font-size:11px;display:block}._1R83Ca_degradeCard{border-radius:var(--agsc-radius-control);border:1px dashed var(--agsc-border-strong);background:var(--agsc-bg-raised);flex-direction:column;gap:4px;padding:10px 12px;display:flex}._1R83Ca_degradeTitle{color:var(--dsw-alias-label-primary);font-size:12px;font-weight:600}._1R83Ca_degradeBody{color:var(--dsw-alias-label-secondary);font-size:12px;line-height:18px}._1R83Ca_degradeDetail{font-size:11px;font-family:var(--ds-font-family-code);color:var(--dsw-alias-label-tertiary);word-break:break-all}._1R83Ca_noticeBar{border-radius:var(--agsc-radius-control);color:var(--agsc-warn);background:var(--dsw-alias-state-warn-tertiary);border:1px solid var(--dsw-alias-state-warn-secondary);padding:5px 10px;font-size:12px;line-height:18px}._1R83Ca_tree{flex-direction:column;gap:2px;min-width:0;display:flex}._1R83Ca_treeRow{align-items:center;gap:4px;min-width:0;display:flex}._1R83Ca_toggle{flex:none;width:18px;height:18px;padding:0;font-size:10px;line-height:1}._1R83Ca_toggleSpacer{flex:none;width:18px}._1R83Ca_node{text-align:left;justify-content:flex-start;gap:6px;min-width:0;height:auto;padding:2px 8px}._1R83Ca_node[data-current=true]{color:var(--agsc-accent);background:color-mix(in srgb, var(--agsc-accent) 8%, transparent);box-shadow:inset 0 0 0 1px var(--agsc-accent);cursor:default}._1R83Ca_nodeId{font-family:var(--ds-font-family-code);text-overflow:ellipsis;white-space:nowrap;overflow:hidden}._1R83Ca_nodeRole{color:var(--dsw-alias-label-tertiary);flex:none;font-size:11px}._1R83Ca_nodeBadge{flex:none}._1R83Ca_nodeBadge>span,._1R83Ca_matchTag>span{color:inherit}._1R83Ca_nodeBadge[data-kind=current]{color:var(--agsc-accent)}._1R83Ca_nodeBadge[data-kind=live]{color:var(--agsc-ok)}._1R83Ca_searchForm{align-items:center;gap:8px;display:flex}._1R83Ca_searchInput{flex:1;min-width:0}._1R83Ca_projectChip{align-self:flex-start}._1R83Ca_resultList{flex-direction:column;gap:4px;min-width:0;display:flex}._1R83Ca_resultItem{text-align:left;border-radius:var(--agsc-radius-control);border:1px solid var(--agsc-border);background:var(--agsc-bg-raised);flex-direction:column;justify-content:flex-start;align-items:stretch;gap:3px;min-width:0;height:auto;padding:6px 10px;display:flex}._1R83Ca_resultItem:hover{border-color:var(--agsc-border-strong)}._1R83Ca_resultItem:focus-visible{outline:2px solid var(--agsc-accent);outline-offset:1px}._1R83Ca_resultHead{align-items:center;gap:6px;min-width:0;display:flex}._1R83Ca_resultAgent{color:var(--dsw-alias-label-secondary);flex:none;font-size:11px;font-weight:600}._1R83Ca_resultTitle{color:var(--dsw-alias-label-primary);text-overflow:ellipsis;white-space:nowrap;font-size:12px;overflow:hidden}._1R83Ca_matchTag{flex:none;margin-left:auto}._1R83Ca_matchTag[data-kind=full-text]{color:var(--agsc-accent)}._1R83Ca_resultMeta{min-width:0;color:var(--dsw-alias-label-tertiary);align-items:center;gap:8px;font-size:11px;display:flex}._1R83Ca_resultProject{text-overflow:ellipsis;white-space:nowrap;overflow:hidden}._1R83Ca_resultId{font-family:var(--ds-font-family-code);flex:none}._1R83Ca_snippet{color:var(--dsw-alias-label-secondary);text-overflow:ellipsis;white-space:nowrap;font-size:12px;line-height:18px;overflow:hidden}._1R83Ca_snippetMark{color:var(--agsc-accent);background:color-mix(in srgb, var(--agsc-accent) 12%, transparent);border-radius:2px;font-weight:600}";
		const tagId$6 = "@shendeguize/dsh-agent-sidecar/src/client/dsh-tools/dsh-tools.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$6, css$6);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$6) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$6;
				created = true;
			}
			tag.textContent = css$6;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var dsh_tools_module_css_default = {
			"degradeBody": "_1R83Ca_degradeBody",
			"degradeCard": "_1R83Ca_degradeCard",
			"degradeDetail": "_1R83Ca_degradeDetail",
			"degradeTitle": "_1R83Ca_degradeTitle",
			"errorCard": "_1R83Ca_errorCard",
			"errorDetail": "_1R83Ca_errorDetail",
			"matchTag": "_1R83Ca_matchTag",
			"mutedLine": "_1R83Ca_mutedLine",
			"node": "_1R83Ca_node",
			"nodeBadge": "_1R83Ca_nodeBadge",
			"nodeId": "_1R83Ca_nodeId",
			"nodeRole": "_1R83Ca_nodeRole",
			"noticeBar": "_1R83Ca_noticeBar",
			"panel": "_1R83Ca_panel",
			"panelCount": "_1R83Ca_panelCount",
			"panelHead": "_1R83Ca_panelHead",
			"panelTitle": "_1R83Ca_panelTitle",
			"projectChip": "_1R83Ca_projectChip",
			"resultAgent": "_1R83Ca_resultAgent",
			"resultHead": "_1R83Ca_resultHead",
			"resultId": "_1R83Ca_resultId",
			"resultItem": "_1R83Ca_resultItem",
			"resultList": "_1R83Ca_resultList",
			"resultMeta": "_1R83Ca_resultMeta",
			"resultProject": "_1R83Ca_resultProject",
			"resultTitle": "_1R83Ca_resultTitle",
			"searchForm": "_1R83Ca_searchForm",
			"searchInput": "_1R83Ca_searchInput",
			"snippet": "_1R83Ca_snippet",
			"snippetMark": "_1R83Ca_snippetMark",
			"toggle": "_1R83Ca_toggle",
			"toggleSpacer": "_1R83Ca_toggleSpacer",
			"tree": "_1R83Ca_tree",
			"treeRow": "_1R83Ca_treeRow"
		};
		//#endregion
		//#region src/client/dsh-tools/strings.ts
		/** Dynamic dsh-tools vocabulary; templates are interpolated by `formatTemplate`. */
		const DSH_TOOLS_STRINGS = createLocaleView({
			lineage: {
				title: "dshtools.lineage.title",
				loading: "dshtools.lineage.loading",
				error: "dshtools.lineage.error",
				empty: "dshtools.lineage.empty",
				currentBadge: "dshtools.lineage.currentBadge",
				liveBadge: "dshtools.lineage.liveBadge",
				notPersistedBadge: "dshtools.lineage.notPersistedBadge",
				role: {
					ancestor: "dshtools.lineage.role.ancestor",
					target: "dshtools.lineage.role.target",
					descendant: "dshtools.lineage.role.descendant"
				},
				jumpTitle: "dshtools.lineage.jumpTitle",
				currentTitle: "dshtools.lineage.currentTitle",
				expand: "dshtools.lineage.expand",
				collapse: "dshtools.lineage.collapse",
				nodeCount: "dshtools.lineage.nodeCount",
				incompleteWithId: "dshtools.lineage.incompleteWithId",
				incomplete: "dshtools.lineage.incomplete",
				degrade: {
					notDshTitle: "dshtools.lineage.degrade.notDshTitle",
					notDshBody: "dshtools.lineage.degrade.notDshBody",
					queryUnavailableTitle: "dshtools.lineage.degrade.queryUnavailableTitle",
					queryUnavailableBody: "dshtools.lineage.degrade.queryUnavailableBody",
					traceFailedTitle: "dshtools.lineage.degrade.traceFailedTitle",
					traceFailedBody: "dshtools.lineage.degrade.traceFailedBody",
					unknownTitle: "dshtools.lineage.degrade.unknownTitle",
					unknownBody: "dshtools.lineage.degrade.unknownBody"
				}
			},
			search: {
				title: "dshtools.search.title",
				placeholder: "dshtools.search.placeholder",
				submit: "dshtools.search.submit",
				loading: "dshtools.search.loading",
				error: "dshtools.search.error",
				empty: "dshtools.search.empty",
				filterOnlyNotice: "dshtools.search.filterOnlyNotice",
				projectFilter: "dshtools.search.projectFilter",
				matchedBy: {
					"full-text": "dshtools.search.matchedBy.full-text",
					title: "dshtools.search.matchedBy.title",
					project: "dshtools.search.matchedBy.project",
					other: "dshtools.search.matchedBy.other"
				},
				untitled: "dshtools.search.untitled"
			}
		});
		//#endregion
		//#region src/client/dsh-tools/logic.ts
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
		/** Resolve `{name}` placeholders in a message template (board convention). */
		function formatTemplate(template, params) {
			return template.replace(/\{(\w+)\}/g, (match, key) => {
				const value = params[key];
				return value === void 0 ? match : String(value);
			});
		}
		/** Head…tail abbreviation for long session ids (same rule as the board). */
		function abbreviateId(id, max = 20) {
			if (id.length <= max) return id;
			return `${id.slice(0, 12)}…${id.slice(-6)}`;
		}
		function toNode(record, role, depth, parentId, currentSessionId) {
			return {
				id: record.header.id,
				role,
				roleLabel: DSH_TOOLS_STRINGS.lineage.role[role],
				depth,
				parentId,
				hasChildren: false,
				isCurrent: currentSessionId !== null && record.header.id === currentSessionId,
				isTarget: role === "target",
				live: record.live,
				persisted: record.persisted,
				shortId: abbreviateId(record.header.id),
				cwd: record.header.cwd ?? null,
				createdAt: record.header.createdAt
			};
		}
		/**
		* Order the ancestors root-first by walking `header.parentSession` links
		* up from the target. Ancestors the links cannot reach (broken chain —
		* the trace then also carries `complete:false`) are still shown: they sit
		* above the resolved chain in their reported order, since everything the
		* backend lists in `ancestors` IS an ancestor even when the intermediate
		* links are unresolved.
		*/
		function orderAncestors(trace) {
			const byId = /* @__PURE__ */ new Map();
			for (const record of trace.ancestors) byId.set(record.header.id, record);
			const chain = [];
			const visited = /* @__PURE__ */ new Set();
			let cursor = trace.target.header.parentSession;
			while (cursor !== void 0 && !visited.has(cursor)) {
				const record = byId.get(cursor);
				if (record === void 0) break;
				visited.add(cursor);
				chain.push(record);
				cursor = record.header.parentSession;
			}
			return [...trace.ancestors.filter((r) => !visited.has(r.header.id)), ...chain.reverse()];
		}
		/**
		* Lineage trace → flattened tree view model. Ancestors form the spine
		* (root-most at depth 0), the target follows, and the descendant branches
		* are appended depth-first in reported order. `currentSessionId` marks the
		* highlight (usually the target, but a lineage panel opened from another
		* node keeps whatever the owner is inspecting).
		*/
		function buildLineageTree(trace, currentSessionId) {
			const nodes = [];
			const ancestors = orderAncestors(trace);
			let parentId = null;
			let depth = 0;
			for (const record of ancestors) {
				nodes.push(toNode(record, "ancestor", depth, parentId, currentSessionId));
				parentId = record.header.id;
				depth += 1;
			}
			nodes.push(toNode(trace.target, "target", depth, parentId, currentSessionId));
			const targetDepth = depth;
			const walk = (branch, branchDepth, branchParent) => {
				nodes.push(toNode(branch.session, "descendant", branchDepth, branchParent, currentSessionId));
				for (const child of branch.descendants) walk(child, branchDepth + 1, branch.session.header.id);
			};
			for (const branch of trace.descendants) walk(branch, targetDepth + 1, trace.target.header.id);
			let maxDepth = 0;
			for (let i = 0; i < nodes.length; i += 1) {
				const node = nodes[i];
				if (node === void 0) continue;
				const next = nodes[i + 1];
				node.hasChildren = next !== void 0 && next.depth > node.depth;
				if (node.depth > maxDepth) maxDepth = node.depth;
			}
			const unresolvedParentId = trace.unresolvedParentId ?? null;
			const incompleteNotice = trace.complete ? null : unresolvedParentId !== null ? formatTemplate(DSH_TOOLS_STRINGS.lineage.incompleteWithId, { id: abbreviateId(unresolvedParentId) }) : DSH_TOOLS_STRINGS.lineage.incomplete;
			return {
				nodes,
				targetId: trace.target.header.id,
				complete: trace.complete,
				unresolvedParentId,
				incompleteNotice,
				nodeCount: nodes.length,
				maxDepth
			};
		}
		/**
		* Apply component-owned collapse state over the flattened rows: a
		* collapsed row keeps itself but hides every deeper row until the DFS
		* order returns to its level.
		*/
		function visibleLineageNodes(nodes, collapsedIds) {
			const out = [];
			let hideDeeperThan = null;
			for (const node of nodes) {
				if (hideDeeperThan !== null) {
					if (node.depth > hideDeeperThan) continue;
					hideDeeperThan = null;
				}
				out.push(node);
				if (node.hasChildren && collapsedIds.has(node.id)) hideDeeperThan = node.depth;
			}
			return out;
		}
		/** Resolve the jump target of a lineage-node click. */
		function resolveJumpTarget(nodeId, currentSessionId) {
			if (currentSessionId !== null && nodeId === currentSessionId) return { kind: "current" };
			return {
				kind: "select",
				sessionId: nodeId
			};
		}
		const DSH_AGENT = "dsh";
		/**
		* Client-side pre-check for the lineage panel: sessions of non-dsh agents
		* get a synthetic degraded response (reason `not_dsh_session`) and the
		* integration never calls the backend for them; dsh sessions return null
		* (proceed to fetch). Honest presentation, not an error (§5.3).
		*/
		function externalLineageFallback(agent) {
			if (agent.trim().toLowerCase() === DSH_AGENT) return null;
			return {
				available: false,
				trace: null,
				reason: "not_dsh_session"
			};
		}
		/**
		* Degradation reason → card copy. Reasons outside the known vocabulary
		* (a newer backend) fall back to the generic card carrying the raw reason
		* — the panel never invents a state.
		*/
		function lineageDegradeCard(reason, detail) {
			const strings = DSH_TOOLS_STRINGS.lineage.degrade;
			const detailOut = detail ?? null;
			switch (reason) {
				case "not_dsh_session": return {
					reason,
					title: strings.notDshTitle,
					body: strings.notDshBody,
					detail: detailOut
				};
				case "session_query_unavailable": return {
					reason,
					title: strings.queryUnavailableTitle,
					body: strings.queryUnavailableBody,
					detail: detailOut
				};
				case "trace_failed": return {
					reason,
					title: strings.traceFailedTitle,
					body: strings.traceFailedBody,
					detail: detailOut
				};
				default: return {
					reason: "unknown",
					title: strings.unknownTitle,
					body: formatTemplate(strings.unknownBody, { reason: reason ?? "?" }),
					detail: detailOut
				};
			}
		}
		/**
		* Resolve what the lineage panel shows, in priority order:
		* loading > transport error > degraded (available:false, per-reason card)
		* > empty (no trace body) > the tree.
		*/
		function deriveLineageView(input) {
			if (input.loading) return {
				kind: "loading",
				text: DSH_TOOLS_STRINGS.lineage.loading
			};
			if (input.error !== null) return {
				kind: "error",
				text: DSH_TOOLS_STRINGS.lineage.error,
				detail: input.error
			};
			if (!input.available) return {
				kind: "degraded",
				card: lineageDegradeCard(input.reason, input.detail)
			};
			if (input.trace === null) return {
				kind: "empty",
				text: DSH_TOOLS_STRINGS.lineage.empty
			};
			return {
				kind: "tree",
				tree: buildLineageTree(input.trace, input.currentSessionId)
			};
		}
		/**
		* Split a snippet into plain/highlight segments around case-insensitive
		* occurrences of the (trimmed) query. Best-effort: the engine does not
		* mark its match, so a query the snippet spells differently simply yields
		* one plain segment. Empty snippet → no segments.
		*/
		function highlightSnippet(snippet, query) {
			if (snippet === "") return [];
			const needle = query.trim().toLowerCase();
			if (needle === "") return [{
				text: snippet,
				highlight: false
			}];
			const haystack = snippet.toLowerCase();
			const segments = [];
			let pos = 0;
			for (;;) {
				const hit = haystack.indexOf(needle, pos);
				if (hit === -1) break;
				if (hit > pos) segments.push({
					text: snippet.slice(pos, hit),
					highlight: false
				});
				segments.push({
					text: snippet.slice(hit, hit + needle.length),
					highlight: true
				});
				pos = hit + needle.length;
			}
			if (pos < snippet.length) segments.push({
				text: snippet.slice(pos),
				highlight: false
			});
			return segments;
		}
		const MATCHED_BY_TOKENS = [
			"full-text",
			"title",
			"project"
		];
		/**
		* Wire hits → render rows. Snippets are split against the response's own
		* echoed query; matchedBy values outside the known vocabulary map to the
		* 'other' tag (label keeps a stable word, never invents a match kind).
		*/
		function normalizeSearchItems(response) {
			return response.items.map((hit) => {
				const matchedBy = MATCHED_BY_TOKENS.includes(hit.matchedBy) ? hit.matchedBy : "other";
				const title = hit.session.title;
				return {
					sessionId: hit.session.sessionId,
					agent: hit.session.agent,
					title,
					titleLabel: title.trim() === "" ? DSH_TOOLS_STRINGS.search.untitled : title,
					project: hit.session.project,
					status: hit.session.status,
					matchedBy,
					matchedByLabel: DSH_TOOLS_STRINGS.search.matchedBy[matchedBy],
					snippet: hit.snippet === null || hit.snippet === "" ? null : highlightSnippet(hit.snippet, response.query),
					shortId: abbreviateId(hit.session.sessionId)
				};
			});
		}
		/**
		* The filter-only degradation bar text; null when full-text search is up.
		* Shown whenever the mode says so — the degradation is a capability fact,
		* independent of whether the current query has results.
		*/
		function searchDegradeNotice(mode) {
			return mode === "filter-only" ? DSH_TOOLS_STRINGS.search.filterOnlyNotice : null;
		}
		/**
		* Resolve the search panel body: loading > error > results > empty (a
		* submitted non-blank query with zero hits) > idle (nothing asked yet).
		*/
		function deriveSearchView(input) {
			const notice = searchDegradeNotice(input.mode);
			if (input.loading) return {
				body: "loading",
				notice,
				text: DSH_TOOLS_STRINGS.search.loading,
				detail: null
			};
			if (input.error !== null) return {
				body: "error",
				notice,
				text: DSH_TOOLS_STRINGS.search.error,
				detail: input.error
			};
			if (input.itemCount > 0) return {
				body: "results",
				notice,
				text: null,
				detail: null
			};
			if (input.query.trim() !== "") return {
				body: "empty",
				notice,
				text: DSH_TOOLS_STRINGS.search.empty,
				detail: null
			};
			return {
				body: "idle",
				notice,
				text: null,
				detail: null
			};
		}
		//#endregion
		//#region src/client/dsh-tools/LineageTree.tsx
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
		const S$1 = DSH_TOOLS_STRINGS.lineage;
		function TreeRow(props) {
			const { node, collapsed } = props;
			const jump = resolveJumpTarget(node.id, props.currentSessionId);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: dsh_tools_module_css_default["treeRow"],
				style: { paddingLeft: node.depth * 16 },
				"data-testid": "agent-sidecar-lineage-row",
				"data-role": node.role,
				children: [node.hasChildren ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
					type: "button",
					size: "sm",
					variant: "ghost",
					className: dsh_tools_module_css_default["toggle"],
					"aria-expanded": !collapsed,
					"aria-label": collapsed ? S$1.expand : S$1.collapse,
					onClick: () => props.onToggle(node.id),
					children: collapsed ? "▸" : "▾"
				}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
					className: dsh_tools_module_css_default["toggleSpacer"],
					"aria-hidden": true
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Button, {
					type: "button",
					size: "sm",
					variant: "ghost",
					className: dsh_tools_module_css_default["node"],
					"data-current": node.isCurrent ? "true" : "false",
					"aria-current": node.isCurrent ? "true" : void 0,
					title: jump.kind === "current" ? S$1.currentTitle : S$1.jumpTitle,
					onClick: () => {
						if (jump.kind === "select") props.onSelectSession(jump.sessionId);
					},
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: dsh_tools_module_css_default["nodeRole"],
							children: node.roleLabel
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: dsh_tools_module_css_default["nodeId"],
							title: node.id,
							children: node.shortId
						}),
						node.isCurrent && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(StaticPill, {
							className: dsh_tools_module_css_default["nodeBadge"],
							"data-kind": "current",
							children: S$1.currentBadge
						}),
						node.live && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(StaticPill, {
							className: dsh_tools_module_css_default["nodeBadge"],
							"data-kind": "live",
							children: S$1.liveBadge
						}),
						!node.persisted && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
							className: dsh_tools_module_css_default["nodeBadge"],
							children: S$1.notPersistedBadge
						})
					]
				})]
			});
		}
		function Tree(props) {
			const { tree } = props;
			const [collapsed, setCollapsed] = (0, react.useState)(/* @__PURE__ */ new Set());
			const [seenTarget, setSeenTarget] = (0, react.useState)(tree.targetId);
			if (seenTarget !== tree.targetId) {
				setSeenTarget(tree.targetId);
				setCollapsed(/* @__PURE__ */ new Set());
			}
			const toggle = (id) => {
				setCollapsed((prev) => {
					const next = new Set(prev);
					if (next.has(id)) next.delete(id);
					else next.add(id);
					return next;
				});
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [tree.incompleteNotice !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: dsh_tools_module_css_default["noticeBar"],
				role: "status",
				children: tree.incompleteNotice
			}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: dsh_tools_module_css_default["tree"],
				role: "tree",
				"data-testid": "agent-sidecar-lineage-tree",
				children: visibleLineageNodes(tree.nodes, collapsed).map((node) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(TreeRow, {
					node,
					collapsed: collapsed.has(node.id),
					currentSessionId: props.currentSessionId,
					onToggle: toggle,
					onSelectSession: props.onSelectSession
				}, node.id))
			})] });
		}
		/** The lineage panel. Pure render of `deriveLineageView` over the props. */
		function LineageTree(props) {
			const view = deriveLineageView({
				loading: props.loading,
				error: props.error,
				available: props.available,
				reason: props.reason ?? null,
				detail: props.detail ?? null,
				trace: props.trace,
				currentSessionId: props.currentSessionId
			});
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				...surfaceProps("dsh-tools", dsh_tools_module_css_default["panel"]),
				"data-testid": "agent-sidecar-lineage",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: dsh_tools_module_css_default["panelHead"],
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: dsh_tools_module_css_default["panelTitle"],
							children: S$1.title
						}), view.kind === "tree" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
							className: dsh_tools_module_css_default["panelCount"],
							children: formatTemplate(S$1.nodeCount, { n: view.tree.nodeCount })
						})]
					}),
					view.kind === "loading" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: dsh_tools_module_css_default["mutedLine"],
						role: "status",
						children: view.text
					}),
					view.kind === "error" && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: dsh_tools_module_css_default["errorCard"],
						role: "alert",
						children: [view.text, view.detail !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: dsh_tools_module_css_default["errorDetail"],
							children: view.detail
						})]
					}),
					view.kind === "degraded" && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: dsh_tools_module_css_default["degradeCard"],
						"data-reason": view.card.reason,
						"data-testid": "agent-sidecar-lineage-degraded",
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: dsh_tools_module_css_default["degradeTitle"],
								children: view.card.title
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: dsh_tools_module_css_default["degradeBody"],
								children: view.card.body
							}),
							view.card.detail !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: dsh_tools_module_css_default["degradeDetail"],
								children: view.card.detail
							})
						]
					}),
					view.kind === "empty" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: dsh_tools_module_css_default["mutedLine"],
						children: view.text
					}),
					view.kind === "tree" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Tree, {
						tree: view.tree,
						currentSessionId: props.currentSessionId,
						onSelectSession: props.onSelectSession
					})
				]
			});
		}
		//#endregion
		//#region src/client/dsh-tools/SearchPanel.tsx
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
		const S = DSH_TOOLS_STRINGS.search;
		function ResultItem(props) {
			const { item } = props;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Button, {
				type: "button",
				size: "sm",
				variant: "ghost",
				className: dsh_tools_module_css_default["resultItem"],
				onClick: () => props.onSelectSession(item.sessionId),
				"data-testid": "agent-sidecar-search-item",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
						className: dsh_tools_module_css_default["resultHead"],
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: dsh_tools_module_css_default["resultAgent"],
								children: item.agent
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: dsh_tools_module_css_default["resultTitle"],
								title: item.title,
								children: item.titleLabel
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)(StaticPill, {
								className: dsh_tools_module_css_default["matchTag"],
								"data-kind": item.matchedBy,
								children: item.matchedByLabel
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
						className: dsh_tools_module_css_default["resultMeta"],
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: dsh_tools_module_css_default["resultProject"],
							title: item.project,
							children: item.project
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: dsh_tools_module_css_default["resultId"],
							title: item.sessionId,
							children: item.shortId
						})]
					}),
					item.snippet !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: dsh_tools_module_css_default["snippet"],
						children: item.snippet.map((segment, index) => segment.highlight ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("mark", {
							className: dsh_tools_module_css_default["snippetMark"],
							children: segment.text
						}, index) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: segment.text }, index))
					})
				]
			});
		}
		/** The search panel. Pure render of `deriveSearchView` over the props. */
		function SearchPanel(props) {
			const view = deriveSearchView({
				loading: props.loading,
				error: props.error,
				mode: props.mode,
				itemCount: props.items.length,
				query: props.query
			});
			const project = props.project ?? null;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				...surfaceProps("dsh-tools", dsh_tools_module_css_default["panel"]),
				"data-testid": "agent-sidecar-search",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: dsh_tools_module_css_default["panelHead"],
						children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: dsh_tools_module_css_default["panelTitle"],
							children: S.title
						})
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("form", {
						className: dsh_tools_module_css_default["searchForm"],
						onSubmit: (ev) => {
							ev.preventDefault();
							props.onSubmit();
						},
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Input, {
							type: "search",
							className: dsh_tools_module_css_default["searchInput"],
							value: props.query,
							placeholder: S.placeholder,
							onChange: (ev) => props.onQueryChange(ev.target.value)
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
							type: "submit",
							size: "sm",
							variant: "outline",
							children: S.submit
						})]
					}),
					project !== null && project !== "" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(StaticPill, {
						className: dsh_tools_module_css_default["projectChip"],
						title: project,
						children: formatTemplate(S.projectFilter, { project })
					}),
					view.notice !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: dsh_tools_module_css_default["noticeBar"],
						role: "status",
						"data-testid": "agent-sidecar-search-degraded",
						children: view.notice
					}),
					view.body === "loading" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: dsh_tools_module_css_default["mutedLine"],
						role: "status",
						children: view.text
					}),
					view.body === "error" && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: dsh_tools_module_css_default["errorCard"],
						role: "alert",
						children: [view.text, view.detail !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: dsh_tools_module_css_default["errorDetail"],
							children: view.detail
						})]
					}),
					view.body === "empty" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: dsh_tools_module_css_default["mutedLine"],
						children: view.text
					}),
					view.body === "results" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: dsh_tools_module_css_default["resultList"],
						children: props.items.map((item) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ResultItem, {
							item,
							onSelectSession: props.onSelectSession
						}, `${item.agent}:${item.sessionId}`))
					})
				]
			});
		}
		//#endregion
		//#region \0dsh-css:src/client/analysis/analysis.module.css.mjs
		const css$5 = ".WYj1Qa_panel{border:1px solid var(--agsc-border-strong);border-radius:var(--agsc-radius-control);background:var(--agsc-bg);min-width:0;color:var(--agsc-fg);flex-direction:column;gap:8px;padding:10px 12px;display:flex}.WYj1Qa_head{align-items:center;gap:8px;min-width:0;display:flex}.WYj1Qa_title{color:var(--agsc-fg);font-size:13px;font-weight:600}.WYj1Qa_spacer{flex:1}.WYj1Qa_startButton{align-self:flex-start}.WYj1Qa_mutedLine{color:var(--agsc-fg-secondary);font-size:12px;line-height:18px}.WYj1Qa_noteCard,.WYj1Qa_errorCard,.WYj1Qa_noticeBar{border:1px solid var(--agsc-border-strong);border-radius:var(--agsc-radius-control);background:var(--agsc-bg-raised);color:var(--agsc-fg-secondary);padding:8px 10px;font-size:12px;line-height:18px}.WYj1Qa_errorCard{border-color:var(--agsc-err);color:var(--agsc-err)}.WYj1Qa_noticeBar{border-color:var(--agsc-warn);color:var(--agsc-warn)}.WYj1Qa_exchange{flex-direction:column;gap:4px;display:flex}.WYj1Qa_exchangeLabel{color:var(--agsc-fg-secondary);font-size:11px;font-weight:600}.WYj1Qa_question{border-radius:var(--agsc-radius-control);background:var(--agsc-bg-raised);color:var(--agsc-fg);white-space:pre-wrap;word-break:break-word;padding:6px 10px;font-size:12px;line-height:18px}.WYj1Qa_summary{border:1px solid var(--agsc-border-strong);border-radius:var(--agsc-radius-control);background:var(--agsc-bg-raised);max-height:320px;color:var(--agsc-fg);white-space:pre-wrap;word-break:break-word;margin:0;padding:8px 10px;font-family:inherit;font-size:12px;line-height:18px;overflow:auto}.WYj1Qa_truncated{color:var(--agsc-warn);font-size:11px}.WYj1Qa_disclaimer{border-top:1px solid var(--agsc-border-strong);color:var(--agsc-fg-secondary);padding-top:6px;font-size:11px;line-height:16px}.WYj1Qa_followupForm{gap:6px;display:flex}.WYj1Qa_followupInput{flex:1;min-width:0}";
		const tagId$5 = "@shendeguize/dsh-agent-sidecar/src/client/analysis/analysis.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$5, css$5);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$5) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$5;
				created = true;
			}
			tag.textContent = css$5;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var analysis_module_css_default = {
			"disclaimer": "WYj1Qa_disclaimer",
			"errorCard": "WYj1Qa_errorCard",
			"exchange": "WYj1Qa_exchange",
			"exchangeLabel": "WYj1Qa_exchangeLabel",
			"followupForm": "WYj1Qa_followupForm",
			"followupInput": "WYj1Qa_followupInput",
			"head": "WYj1Qa_head",
			"mutedLine": "WYj1Qa_mutedLine",
			"noteCard": "WYj1Qa_noteCard",
			"noticeBar": "WYj1Qa_noticeBar",
			"panel": "WYj1Qa_panel",
			"question": "WYj1Qa_question",
			"spacer": "WYj1Qa_spacer",
			"startButton": "WYj1Qa_startButton",
			"summary": "WYj1Qa_summary",
			"title": "WYj1Qa_title",
			"truncated": "WYj1Qa_truncated"
		};
		//#endregion
		//#region src/client/analysis/AnalysisPanel.tsx
		/** Terminal failure code → locale key ('' falls back to the generic). */
		const ERROR_KEYS = {
			analysis_disabled: "analysis.errDisabled",
			analysis_unavailable: "analysis.errUnavailable",
			target_not_found: "analysis.errTargetNotFound",
			too_many_active: "analysis.errTooManyActive",
			timeout: "analysis.errTimeout",
			create_failed: "analysis.errCreateFailed",
			cancelled: "analysis.errCancelled",
			network_error: "analysis.errNetwork",
			request_timeout: "analysis.errNetwork"
		};
		function errorText(code) {
			const key = ERROR_KEYS[code];
			return key !== void 0 ? t(key) : t("analysis.errGeneric", { code });
		}
		/** Retryable notice code → copy. */
		function noticeText(code) {
			if (code === "timeout") return t("analysis.noticeTimeout");
			if (code === "cancel_failed") return t("analysis.noticeCancelFailed");
			return t("analysis.noticeNetwork");
		}
		function Exchange(props) {
			const { exchange } = props;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: analysis_module_css_default["exchange"],
				"data-testid": "agent-sidecar-analysis-exchange",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: analysis_module_css_default["exchangeLabel"],
						children: exchange.question === null ? t("analysis.exchangeInitial") : t("analysis.followupLabel")
					}),
					exchange.question !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: analysis_module_css_default["question"],
						children: exchange.question
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", {
						className: analysis_module_css_default["summary"],
						children: exchange.summary === "" ? t("analysis.emptySummary") : exchange.summary
					}),
					exchange.truncated && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: analysis_module_css_default["truncated"],
						children: t("analysis.truncatedNotice")
					})
				]
			});
		}
		/** The analysis panel. Pure render over props (state machine lives in glue). */
		function AnalysisPanel(props) {
			const { enabled, state } = props;
			const [question, setQuestion] = (0, react.useState)("");
			const conversationLive = state.phase === "ready" || state.phase === "answering";
			const showStart = state.phase === "idle" || state.phase === "failed" || state.phase === "stopped";
			const submitFollowup = () => {
				const q = question.trim();
				if (q === "" || state.phase !== "ready") return;
				setQuestion("");
				props.onFollowup(q);
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				...surfaceProps("analysis-panel", analysis_module_css_default["panel"]),
				"data-testid": "agent-sidecar-analysis",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: analysis_module_css_default["head"],
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: analysis_module_css_default["title"],
								children: t("analysis.title")
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { className: analysis_module_css_default["spacer"] }),
							conversationLive && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
								type: "button",
								size: "sm",
								variant: "ghost",
								disabled: !enabled,
								onClick: props.onStop,
								"data-testid": "agent-sidecar-analysis-stop",
								children: t("analysis.stop")
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
								type: "button",
								size: "sm",
								variant: "ghost",
								onClick: props.onClose,
								children: t("analysis.close")
							})
						]
					}),
					!enabled && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: analysis_module_css_default["noteCard"],
						"data-testid": "agent-sidecar-analysis-disabled",
						children: t("analysis.disabledNote")
					}),
					state.exchanges.map((exchange, index) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Exchange, { exchange }, index)),
					state.phase === "failed" && state.errorCode !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: analysis_module_css_default["errorCard"],
						"data-testid": "agent-sidecar-analysis-error",
						children: errorText(state.errorCode)
					}),
					state.phase === "stopped" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: analysis_module_css_default["noteCard"],
						children: t("analysis.stopped")
					}),
					state.noticeCode !== null && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: analysis_module_css_default["noticeBar"],
						"data-testid": "agent-sidecar-analysis-notice",
						children: noticeText(state.noticeCode)
					}),
					enabled && showStart && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [state.phase === "idle" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: analysis_module_css_default["mutedLine"],
						children: t("analysis.idleHint")
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
						type: "button",
						size: "sm",
						variant: "primary",
						className: analysis_module_css_default["startButton"],
						onClick: props.onStart,
						"data-testid": "agent-sidecar-analysis-start",
						children: state.phase === "idle" ? t("analysis.start") : t("analysis.restart")
					})] }),
					state.phase === "requesting" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: analysis_module_css_default["mutedLine"],
						children: t("analysis.requesting")
					}),
					state.phase === "answering" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: analysis_module_css_default["mutedLine"],
						children: t("analysis.answering")
					}),
					conversationLive && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: analysis_module_css_default["followupForm"],
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Input, {
							className: analysis_module_css_default["followupInput"],
							value: question,
							placeholder: t("analysis.followupPlaceholder"),
							disabled: !enabled || state.phase !== "ready",
							onChange: (event) => {
								setQuestion(event.currentTarget.value);
							},
							onKeyDown: (event) => {
								if (event.key === "Enter") submitFollowup();
							},
							"data-testid": "agent-sidecar-analysis-question"
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
							type: "button",
							size: "sm",
							variant: "ghost",
							disabled: !enabled || state.phase !== "ready" || question.trim() === "",
							onClick: submitFollowup,
							"data-testid": "agent-sidecar-analysis-followup",
							children: t("analysis.followupSubmit")
						})]
					}),
					(conversationLive || state.exchanges.length > 0) && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: analysis_module_css_default["disclaimer"],
						"data-testid": "agent-sidecar-analysis-disclaimer",
						children: state.disclaimer ?? t("analysis.disclaimerFallback")
					})
				]
			});
		}
		//#endregion
		//#region \0dsh-css:src/client/inject/inject.module.css.mjs
		const css$4 = ".a2ZgPq_panel{border:1px solid var(--agsc-border-strong);border-radius:var(--agsc-radius-card);background:var(--agsc-bg);flex-direction:column;max-width:560px;display:flex}.a2ZgPq_header{border-bottom:1px solid var(--agsc-border-strong);align-items:center;gap:12px;padding:14px 16px 10px;display:flex}.a2ZgPq_title{min-width:0;color:var(--agsc-fg);flex:1;margin:0;font-size:15px;font-weight:600;line-height:1.4}.a2ZgPq_body{flex-direction:column;gap:10px;padding:12px 16px 14px;display:flex}.a2ZgPq_capabilityOff,.a2ZgPq_noticeWarn,.a2ZgPq_noticeError,.a2ZgPq_warnBar,.a2ZgPq_auditNote,.a2ZgPq_resultOk,.a2ZgPq_resultFail,.a2ZgPq_resultUnknown{border-radius:var(--agsc-radius-control);background:var(--agsc-bg-raised);color:var(--agsc-fg-secondary);margin:0;padding:8px 10px;font-size:12px;line-height:1.6}.a2ZgPq_noticeWarn,.a2ZgPq_warnBar{border:1px solid var(--agsc-warn);color:var(--agsc-warn)}.a2ZgPq_noticeError,.a2ZgPq_resultFail{border:1px solid var(--agsc-err);color:var(--agsc-err)}.a2ZgPq_noticeDetail,.a2ZgPq_noTarget,.a2ZgPq_modeHint,.a2ZgPq_planKey,.a2ZgPq_observedNote,.a2ZgPq_resultDetail{color:var(--agsc-fg-dimmed)}.a2ZgPq_targetRow,.a2ZgPq_planRow{align-items:baseline;gap:10px;min-width:0;display:flex}.a2ZgPq_noTarget,.a2ZgPq_planKey,.a2ZgPq_modeHint,.a2ZgPq_resultDetail{font-size:12px;line-height:1.5}.a2ZgPq_agentTag{flex:none}.a2ZgPq_planTitle{min-width:0;color:var(--agsc-fg);text-overflow:ellipsis;white-space:nowrap;font-size:13px;line-height:1.5;overflow:hidden}.a2ZgPq_field,.a2ZgPq_modeText,.a2ZgPq_planPreview{flex-direction:column;display:flex}.a2ZgPq_field{gap:6px}.a2ZgPq_label,.a2ZgPq_modeLabel{color:var(--agsc-fg);font-size:13px;line-height:1.5}.a2ZgPq_textarea{appearance:none;resize:vertical;border:1px solid var(--agsc-border-strong);border-radius:var(--agsc-radius-control);background:var(--agsc-bg-raised);min-height:96px;color:var(--agsc-fg);font:inherit;padding:8px 10px;font-size:13px;line-height:1.6}.a2ZgPq_textarea:focus-visible{border-color:var(--agsc-accent);outline:none}.a2ZgPq_textarea::placeholder{color:var(--agsc-fg-dimmed)}.a2ZgPq_textarea:disabled{cursor:default;opacity:.5}.a2ZgPq_byteRow{align-items:center;gap:10px;display:flex}.a2ZgPq_byteBar{background:var(--agsc-border);border-radius:2px;flex:1;height:3px;overflow:hidden}.a2ZgPq_byteFill{background:var(--agsc-accent);border-radius:2px;height:100%;transition:width .12s}.a2ZgPq_byteFillOver{background:var(--agsc-err)}.a2ZgPq_byteText,.a2ZgPq_byteTextOver{color:var(--agsc-fg-dimmed);font-variant-numeric:tabular-nums;flex:none;font-size:11px;line-height:1.5}.a2ZgPq_byteTextOver,.a2ZgPq_invalid{color:var(--agsc-err)}.a2ZgPq_invalid{margin:0;font-size:12px;line-height:1.5}.a2ZgPq_modes{border:0;flex-direction:column;gap:8px;margin:0;padding:0;display:flex}.a2ZgPq_modeOption{cursor:pointer;align-items:flex-start;gap:10px;display:flex}.a2ZgPq_modeOption input{width:14px;height:14px;accent-color:var(--agsc-accent);cursor:pointer;margin:3px 0 0}.a2ZgPq_modeOption input:disabled{cursor:default}.a2ZgPq_modeText{gap:2px;min-width:0}.a2ZgPq_auditNote{padding:8px 10px}.a2ZgPq_planBox{border:1px solid var(--agsc-border-strong);border-radius:var(--agsc-radius-control);background:var(--agsc-bg-raised);flex-direction:column;gap:6px;padding:10px 12px;display:flex}.a2ZgPq_planKey{flex:none}.a2ZgPq_planValue{min-width:0;color:var(--agsc-fg);align-items:baseline;gap:8px;font-size:13px;line-height:1.5;display:flex}.a2ZgPq_observedNote{margin:0;font-size:11px;line-height:1.5}.a2ZgPq_planPreview{gap:4px;min-width:0}.a2ZgPq_preview{border:1px solid var(--agsc-border-strong);background:var(--agsc-bg);max-height:120px;color:var(--agsc-fg-secondary);white-space:pre-wrap;word-break:break-word;border-radius:6px;margin:0;padding:8px 10px;font-size:12px;line-height:1.6;overflow:auto}.a2ZgPq_countdown{color:var(--agsc-fg-secondary);font-variant-numeric:tabular-nums;margin:0;font-size:12px;line-height:1.5}.a2ZgPq_resultOk,.a2ZgPq_resultFail,.a2ZgPq_resultUnknown{color:var(--agsc-fg);padding:10px 12px;font-size:13px}.a2ZgPq_resultOk{border:1px solid var(--agsc-ok)}.a2ZgPq_resultUnknown{border:1px solid var(--agsc-fg-dimmed);color:var(--agsc-fg-secondary)}.a2ZgPq_resultDetail{margin:0;line-height:1.6}.a2ZgPq_footer{justify-content:flex-end;align-items:center;gap:8px;padding-top:4px;display:flex}.a2ZgPq_btnDanger{appearance:none;border:1px solid var(--agsc-err);border-radius:var(--agsc-radius-control);color:var(--agsc-err);font:inherit;cursor:pointer;background:0 0;padding:5px 14px;font-size:13px;font-weight:600;line-height:1.5}.a2ZgPq_btnDanger:hover:not(:disabled){background:var(--agsc-err);color:var(--agsc-bg-raised)}.a2ZgPq_btnDanger:disabled{cursor:default;opacity:.4}.a2ZgPq_btnDanger:focus-visible{outline:2px solid var(--agsc-accent);outline-offset:1px}";
		const tagId$4 = "@shendeguize/dsh-agent-sidecar/src/client/inject/inject.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$4, css$4);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$4) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$4;
				created = true;
			}
			tag.textContent = css$4;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var inject_module_css_default = {
			"agentTag": "a2ZgPq_agentTag",
			"auditNote": "a2ZgPq_auditNote",
			"body": "a2ZgPq_body",
			"btnDanger": "a2ZgPq_btnDanger",
			"byteBar": "a2ZgPq_byteBar",
			"byteFill": "a2ZgPq_byteFill",
			"byteFillOver": "a2ZgPq_byteFillOver",
			"byteRow": "a2ZgPq_byteRow",
			"byteText": "a2ZgPq_byteText",
			"byteTextOver": "a2ZgPq_byteTextOver",
			"capabilityOff": "a2ZgPq_capabilityOff",
			"countdown": "a2ZgPq_countdown",
			"field": "a2ZgPq_field",
			"footer": "a2ZgPq_footer",
			"header": "a2ZgPq_header",
			"invalid": "a2ZgPq_invalid",
			"label": "a2ZgPq_label",
			"modeHint": "a2ZgPq_modeHint",
			"modeLabel": "a2ZgPq_modeLabel",
			"modeOption": "a2ZgPq_modeOption",
			"modeText": "a2ZgPq_modeText",
			"modes": "a2ZgPq_modes",
			"noTarget": "a2ZgPq_noTarget",
			"noticeDetail": "a2ZgPq_noticeDetail",
			"noticeError": "a2ZgPq_noticeError",
			"noticeWarn": "a2ZgPq_noticeWarn",
			"observedNote": "a2ZgPq_observedNote",
			"panel": "a2ZgPq_panel",
			"planBox": "a2ZgPq_planBox",
			"planKey": "a2ZgPq_planKey",
			"planPreview": "a2ZgPq_planPreview",
			"planRow": "a2ZgPq_planRow",
			"planTitle": "a2ZgPq_planTitle",
			"planValue": "a2ZgPq_planValue",
			"preview": "a2ZgPq_preview",
			"resultDetail": "a2ZgPq_resultDetail",
			"resultFail": "a2ZgPq_resultFail",
			"resultOk": "a2ZgPq_resultOk",
			"resultUnknown": "a2ZgPq_resultUnknown",
			"targetRow": "a2ZgPq_targetRow",
			"textarea": "a2ZgPq_textarea",
			"title": "a2ZgPq_title",
			"warnBar": "a2ZgPq_warnBar"
		};
		//#endregion
		//#region src/client/inject/logic.ts
		/** Message byte cap; must mirror inject-gateway.ts (pinned by test). */
		const MAX_MESSAGE_BYTES = 16 * 1024;
		const utf8 = new TextEncoder();
		/**
		* UTF-8 byte size of a message. TextEncoder agrees with the host's
		* `Buffer.byteLength(message, 'utf8')` for every well-formed string, so the
		* 16 KiB verdict is identical on both sides.
		*/
		function messageBytes(message) {
			return utf8.encode(message).length;
		}
		/** Local pre-validation, same checks and order as the host gateway. */
		function validateMessage(message) {
			const bytes = messageBytes(message);
			if (bytes === 0) return {
				ok: false,
				code: "empty",
				bytes
			};
			if (message.includes("\0")) return {
				ok: false,
				code: "nul",
				bytes
			};
			if (bytes > 16384) return {
				ok: false,
				code: "too_large",
				bytes
			};
			return {
				ok: true,
				bytes
			};
		}
		/** Derive the byte counter view from a byte count. */
		function byteUsage(bytes) {
			return {
				bytes,
				limit: MAX_MESSAGE_BYTES,
				ratio: Math.min(1, Math.max(0, bytes / MAX_MESSAGE_BYTES)),
				over: bytes > MAX_MESSAGE_BYTES
			};
		}
		/**
		* Whether the process-list visibility warning applies. Only cursor-cli
		* qualifies: its upstream contract puts the prompt on the native
		* subprocess argv. claude / codex / dsh do not warn — after S4a the
		* sidecar's own argv (send --message-stdin) and the dsh in-process path
		* both keep the body off the process list.
		*/
		function showsProcessListWarning(agent) {
			return agent.trim().toLowerCase() === "cursor-cli";
		}
		/** Countdown view for a server-issued expiresAt (epoch ms). */
		function tokenCountdown(expiresAt, nowMs) {
			const remainingMs = Math.max(0, expiresAt - nowMs);
			return {
				remainingMs,
				seconds: Math.ceil(remainingMs / 1e3),
				expired: nowMs >= expiresAt
			};
		}
		/** Fresh machine state (factory, so callers can never share a mutable seed). */
		function initialPanelState() {
			return {
				phase: "idle",
				notice: null
			};
		}
		/**
		* Pure transition function. Events that do not apply to the current phase
		* return the state unchanged (same reference), which both makes stale
		* async callbacks harmless and gives React a free bail-out.
		*/
		function reducePanel(state, event) {
			switch (event.type) {
				case "PREPARE_START":
					if (state.phase !== "idle") return state;
					return {
						phase: "preparing",
						message: event.message,
						mode: event.mode
					};
				case "PREPARE_OK":
					if (state.phase !== "preparing") return state;
					return {
						phase: "confirm",
						message: state.message,
						mode: state.mode,
						requestId: event.response.requestId,
						confirmToken: event.response.confirmToken,
						plan: event.response.plan,
						expiresAt: event.response.expiresAt
					};
				case "PREPARE_REJECTED":
					if (state.phase !== "preparing") return state;
					return {
						phase: "idle",
						notice: {
							kind: "prepare_rejected",
							code: event.code,
							...event.detail !== void 0 ? { detail: event.detail } : {}
						}
					};
				case "PREPARE_ERROR":
					if (state.phase !== "preparing") return state;
					return {
						phase: "idle",
						notice: {
							kind: "prepare_error",
							code: event.code
						}
					};
				case "TICK":
					if (state.phase !== "confirm") return state;
					if (event.nowMs < state.expiresAt) return state;
					return {
						phase: "idle",
						notice: { kind: "token_expired" }
					};
				case "CANCEL":
					if (state.phase !== "confirm") return state;
					return {
						phase: "idle",
						notice: null
					};
				case "EXECUTE_START":
					if (state.phase !== "confirm") return state;
					return {
						phase: "executing",
						message: state.message,
						mode: state.mode,
						requestId: state.requestId,
						confirmToken: state.confirmToken,
						plan: state.plan
					};
				case "EXECUTE_RESULT":
					if (state.phase !== "executing") return state;
					return {
						phase: "result",
						result: event.result,
						plan: state.plan
					};
				case "RESET":
					if (state.phase !== "result") return state;
					if (state.result.outcome === "unknown") return state;
					return {
						phase: "idle",
						notice: null
					};
			}
		}
		const API_ERROR_KINDS = /* @__PURE__ */ new Set([
			"timeout",
			"aborted",
			"network",
			"http",
			"parse"
		]);
		/** Structural guard matching client/api.ts ApiError instances. */
		function isApiErrorLike(value) {
			if (typeof value !== "object" || value === null) return false;
			const v = value;
			return typeof v["kind"] === "string" && API_ERROR_KINDS.has(v["kind"]) && typeof v["reason"] === "string" && !("outcome" in v);
		}
		/**
		* Classify what `onPrepare` resolved with. An HTTP-kind error is a server
		* vocabulary rejection (the reason carries the errorCode routes mapped to
		* the status); any other kind is a transport failure — harmless for
		* prepare, which is side-effect-free beyond a token that expires on its
		* own, so the editor may simply offer another attempt.
		*/
		function classifyPrepareResponse(value) {
			if (isApiErrorLike(value)) return value.kind === "http" ? {
				type: "PREPARE_REJECTED",
				code: value.reason
			} : {
				type: "PREPARE_ERROR",
				code: value.reason
			};
			return {
				type: "PREPARE_OK",
				response: value
			};
		}
		/**
		* Classify what `onExecute` resolved with. An HTTP-kind error is the
		* routes-mapped failed outcome. Everything else (timeout/network/parse/
		* aborted) happened AFTER the execute may already have been dispatched, so
		* the honest verdict is `outcome: 'unknown'` — terminal, no retry (S6);
		* the user is pointed at the target session to verify.
		*/
		function classifyExecuteResponse(value) {
			if (isApiErrorLike(value)) {
				if (value.kind === "http") return {
					type: "EXECUTE_RESULT",
					result: {
						outcome: "failed",
						errorCode: value.reason
					}
				};
				return {
					type: "EXECUTE_RESULT",
					result: {
						outcome: "unknown",
						errorCode: value.reason
					}
				};
			}
			return {
				type: "EXECUTE_RESULT",
				result: value
			};
		}
		/**
		* Why (if at all) the prepare action is unavailable, in priority order:
		* capability off > no target > a phase already in flight > invalid message.
		*/
		function deriveEditorGate(input) {
			if (!input.injectEnabled) return {
				canPrepare: false,
				block: "inject_off"
			};
			if (!input.hasTarget) return {
				canPrepare: false,
				block: "no_target"
			};
			if (input.phase !== "idle") return {
				canPrepare: false,
				block: "busy"
			};
			if (!input.validation.ok) return {
				canPrepare: false,
				block: "invalid_message"
			};
			return {
				canPrepare: true,
				block: null
			};
		}
		/**
		* Map a keydown to a panel intent: Escape closes in every phase;
		* Cmd/Ctrl+Enter submits the PREPARE step only (editor phase with a valid
		* message). The confirm phase deliberately binds nothing — executing the
		* injection stays an explicit click (two-phase discipline, §5.3), so no
		* keyboard path can shortcut the confirmation.
		*/
		function classifyPanelKey(input) {
			if (input.key === "Escape") return "close";
			if (input.key === "Enter" && (input.metaKey || input.ctrlKey)) return input.phase === "idle" && input.canPrepare ? "prepare" : null;
			return null;
		}
		/** Which follow-up affordances a terminal outcome earns. */
		function resultActions(outcome) {
			return {
				canReprepare: outcome === "failed",
				showCheckSessionHint: outcome === "unknown"
			};
		}
		/**
		* True when an execute response reports the message actually reached the
		* target (UX-05 observation loop): a delivered outcome — including an
		* idempotent replay of one — never an error envelope. failed/unknown must
		* NOT trigger observation aids: unknown is a locked terminal state (S6)
		* and any follow-up activity could read as "retrying is fine".
		*/
		function isDeliveredResult(value) {
			return !isApiErrorLike(value) && value.outcome === "delivered";
		}
		/** Mode radio copy (label + semantics hint) per injection mode. */
		const MODE_COPY = {
			queue: {
				label: "inject.modeQueue",
				hint: "inject.modeQueueHint"
			},
			steer: {
				label: "inject.modeSteer",
				hint: "inject.modeSteerHint"
			}
		};
		/** Local validation verdict → copy key. */
		const MESSAGE_INVALID_COPY = {
			empty: "inject.msgEmpty",
			nul: "inject.msgNul",
			too_large: "inject.msgTooLarge"
		};
		/** Validation verdict → renderable copy (too_large carries bytes/limit). */
		function messageInvalidCopy(validation) {
			if (validation.code === "too_large") return {
				key: MESSAGE_INVALID_COPY.too_large,
				params: {
					bytes: validation.bytes,
					limit: MAX_MESSAGE_BYTES
				}
			};
			return { key: MESSAGE_INVALID_COPY[validation.code] };
		}
		/** Terminal outcome → headline copy key. */
		const RESULT_COPY = {
			delivered: "inject.resultDelivered",
			failed: "inject.resultFailed",
			unknown: "inject.resultUnknown"
		};
		/**
		* Error vocabulary → copy key: the gateway codes (inject-gateway.ts), plus
		* the data layer's transport reasons (api.ts). Unlisted codes fall back to
		* the generic template via {@link errorCopy}.
		*/
		const ERROR_COPY = {
			inject_disabled: "inject.errInjectDisabled",
			invalid_message: "inject.errInvalidMessage",
			target_not_found: "inject.errTargetNotFound",
			target_dead: "inject.errTargetDead",
			too_many_pending: "inject.errTooManyPending",
			token_missing: "inject.errTokenMissing",
			token_expired: "inject.errTokenExpired",
			token_reused: "inject.errTokenReused",
			token_mismatch: "inject.errTokenMismatch",
			unsupported_agent: "inject.errUnsupportedAgent",
			executor_error: "inject.errExecutorError",
			request_timeout: "inject.errTimeout",
			request_aborted: "inject.errAborted",
			network_error: "inject.errNetwork",
			invalid_json: "inject.errParse"
		};
		/** Error code → renderable copy; unknown codes get the generic template. */
		function errorCopy(code) {
			const key = ERROR_COPY[code];
			return key === void 0 ? {
				key: "inject.errGeneric",
				params: { code }
			} : { key };
		}
		/** Editor notice → renderable copy. */
		function noticeCopy(notice) {
			if (notice.kind === "token_expired") return { key: "inject.tokenExpired" };
			return errorCopy(notice.code);
		}
		//#endregion
		//#region src/client/inject/InjectPanel.tsx
		/** Countdown re-render cadence while a confirm token is live. */
		const TICK_MS = 500;
		function renderCopy(t, copy) {
			return t(copy.key, copy.params);
		}
		/** cursor-cli process-list warning (gated) + the always-on audit note. */
		function Warnings(props) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [props.agent !== null && showsProcessListWarning(props.agent) ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
				className: inject_module_css_default["warnBar"],
				role: "alert",
				children: props.t("inject.argvWarning")
			}) : null, /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
				className: inject_module_css_default["auditNote"],
				children: props.t("inject.auditNote")
			})] });
		}
		/** Confirm-phase plan: live target snapshot + message digest. */
		function PlanBox(props) {
			const { t, plan } = props;
			const status = plan.targetStatus;
			const targetName = status.title !== void 0 && status.title !== "" ? status.title : plan.target.sessionId;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: inject_module_css_default["planBox"],
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: inject_module_css_default["planRow"],
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: inject_module_css_default["planKey"],
							children: t("inject.planTargetLabel")
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: inject_module_css_default["planValue"],
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
								className: inject_module_css_default["agentTag"],
								children: plan.target.agent
							}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: inject_module_css_default["planTitle"],
								title: plan.target.sessionId,
								children: targetName
							})]
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: inject_module_css_default["planRow"],
						children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: inject_module_css_default["planKey"],
							children: t("inject.planStatus", { status: status.status })
						})
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: inject_module_css_default["observedNote"],
						children: t("inject.statusObservedNote")
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: inject_module_css_default["planRow"],
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: inject_module_css_default["planKey"],
							children: t("inject.planModeLabel")
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: inject_module_css_default["planValue"],
							children: t(MODE_COPY[plan.mode].label)
						})]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: inject_module_css_default["planPreview"],
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: inject_module_css_default["planKey"],
							children: t("inject.planPreviewLabel", { bytes: plan.messagePreview.bytes })
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("pre", {
							className: inject_module_css_default["preview"],
							children: plan.messagePreview.head
						})]
					})
				]
			});
		}
		function InjectPanel(props) {
			const t$1 = props.t ?? t;
			const now = props.nowMs ?? Date.now;
			const [state, dispatch] = (0, react.useReducer)(reducePanel, void 0, initialPanelState);
			const [draft, setDraft] = (0, react.useState)("");
			const [mode, setMode] = (0, react.useState)(props.defaultMode);
			const [clock, setClock] = (0, react.useState)(() => now());
			const textareaId = (0, react.useId)();
			const modeGroup = (0, react.useId)();
			const inConfirm = state.phase === "confirm";
			(0, react.useEffect)(() => {
				if (!inConfirm) return;
				setClock(now());
				const id = setInterval(() => {
					const nowValue = now();
					setClock(nowValue);
					dispatch({
						type: "TICK",
						nowMs: nowValue
					});
				}, TICK_MS);
				return () => {
					clearInterval(id);
				};
			}, [inConfirm]);
			const validation = validateMessage(draft);
			const gate = deriveEditorGate({
				injectEnabled: props.capability.inject,
				hasTarget: props.target !== null,
				phase: state.phase,
				validation
			});
			const handlePrepare = async () => {
				const target = props.target;
				if (target === null || !gate.canPrepare) return;
				const message = draft;
				dispatch({
					type: "PREPARE_START",
					message,
					mode
				});
				let event;
				try {
					event = classifyPrepareResponse(await props.onPrepare({
						target: {
							agent: target.agent,
							sessionId: target.sessionId
						},
						mode,
						message
					}));
				} catch {
					event = {
						type: "PREPARE_ERROR",
						code: "unexpected_error"
					};
				}
				dispatch(event);
			};
			const handleExecute = async () => {
				if (state.phase !== "confirm") return;
				const { requestId, confirmToken, message } = state;
				dispatch({ type: "EXECUTE_START" });
				let event;
				try {
					event = classifyExecuteResponse(await props.onExecute({
						requestId,
						confirmToken,
						message
					}));
				} catch {
					event = {
						type: "EXECUTE_RESULT",
						result: {
							outcome: "unknown",
							errorCode: "unexpected_error"
						}
					};
				}
				dispatch(event);
			};
			const handleKeyDown = (event) => {
				const intent = classifyPanelKey({
					key: event.key,
					metaKey: event.metaKey,
					ctrlKey: event.ctrlKey,
					phase: state.phase,
					canPrepare: gate.canPrepare
				});
				if (intent === "close" && props.onClose !== void 0) {
					event.stopPropagation();
					props.onClose();
				} else if (intent === "prepare") {
					event.preventDefault();
					handlePrepare();
				}
			};
			const panelSurface = surfaceProps("inject-panel", inject_module_css_default["panel"]);
			const closeButton = props.onClose !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
				type: "button",
				size: "sm",
				variant: "outline",
				onClick: props.onClose,
				children: t$1("inject.close")
			}) : null;
			if (!props.capability.inject) return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				...panelSurface,
				"aria-label": t$1("inject.title"),
				onKeyDown: handleKeyDown,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("header", {
					className: inject_module_css_default["header"],
					children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("h3", {
						className: inject_module_css_default["title"],
						children: t$1("inject.title")
					})
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: inject_module_css_default["body"],
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: inject_module_css_default["capabilityOff"],
						role: "status",
						children: t$1("inject.capabilityOff")
					}), closeButton !== null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: inject_module_css_default["footer"],
						children: closeButton
					}) : null]
				})]
			});
			if (state.phase === "result") {
				const { result } = state;
				const actions = resultActions(result.outcome);
				const toneClass = result.outcome === "delivered" ? inject_module_css_default["resultOk"] : result.outcome === "failed" ? inject_module_css_default["resultFail"] : inject_module_css_default["resultUnknown"];
				return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
					...panelSurface,
					"aria-label": t$1("inject.title"),
					onKeyDown: handleKeyDown,
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("header", {
						className: inject_module_css_default["header"],
						children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("h3", {
							className: inject_module_css_default["title"],
							children: t$1("inject.title")
						})
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: inject_module_css_default["body"],
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: toneClass,
								role: "status",
								children: t$1(RESULT_COPY[result.outcome])
							}),
							result.outcome === "failed" && result.errorCode !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: inject_module_css_default["resultDetail"],
								children: renderCopy(t$1, errorCopy(result.errorCode))
							}) : null,
							result.replayed === true ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: inject_module_css_default["resultDetail"],
								children: t$1("inject.resultReplayed")
							}) : null,
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: inject_module_css_default["footer"],
								children: [
									closeButton,
									isDeliveredResult(result) && props.onObserve !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
										type: "button",
										size: "sm",
										variant: "primary",
										onClick: props.onObserve,
										"data-testid": "agent-sidecar-inject-observe",
										children: t$1("inject.observeListen")
									}) : null,
									actions.canReprepare ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
										type: "button",
										size: "sm",
										variant: "primary",
										onClick: () => {
											dispatch({ type: "RESET" });
										},
										children: t$1("inject.reprepare")
									}) : null,
									result.outcome === "delivered" && props.onClose === void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
										type: "button",
										size: "sm",
										variant: "primary",
										onClick: () => {
											dispatch({ type: "RESET" });
										},
										children: t$1("inject.done")
									}) : null
								]
							})
						]
					})]
				});
			}
			if (state.phase === "confirm" || state.phase === "executing") {
				const executing = state.phase === "executing";
				const countdown = state.phase === "confirm" ? tokenCountdown(state.expiresAt, clock) : null;
				return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
					...panelSurface,
					"aria-label": t$1("inject.confirmTitle"),
					onKeyDown: handleKeyDown,
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("header", {
						className: inject_module_css_default["header"],
						children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("h3", {
							className: inject_module_css_default["title"],
							children: t$1("inject.confirmTitle")
						})
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: inject_module_css_default["body"],
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)(PlanBox, {
								t: t$1,
								plan: state.plan
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Warnings, {
								t: t$1,
								agent: state.plan.target.agent
							}),
							countdown !== null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
								className: inject_module_css_default["countdown"],
								role: "timer",
								children: t$1("inject.countdown", { seconds: countdown.seconds })
							}) : null,
							/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
								className: inject_module_css_default["footer"],
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
									type: "button",
									size: "sm",
									variant: "outline",
									disabled: executing,
									onClick: () => {
										dispatch({ type: "CANCEL" });
									},
									children: t$1("inject.cancel")
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
									type: "button",
									className: inject_module_css_default["btnDanger"],
									disabled: executing,
									onClick: () => {
										handleExecute();
									},
									children: t$1(executing ? "inject.executing" : "inject.confirmExecute")
								})]
							})
						]
					})]
				});
			}
			const preparing = state.phase === "preparing";
			const canEdit = props.target !== null && !preparing;
			const usage = byteUsage(validation.bytes);
			const notice = state.phase === "idle" ? state.notice : null;
			const showInvalid = !validation.ok && validation.code !== "empty";
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				...panelSurface,
				"aria-label": t$1("inject.title"),
				onKeyDown: handleKeyDown,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("header", {
					className: inject_module_css_default["header"],
					children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("h3", {
						className: inject_module_css_default["title"],
						children: t$1("inject.title")
					})
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: inject_module_css_default["body"],
					children: [
						notice !== null ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("p", {
							className: notice.kind === "token_expired" ? inject_module_css_default["noticeWarn"] : inject_module_css_default["noticeError"],
							role: "alert",
							children: [renderCopy(t$1, noticeCopy(notice)), notice.kind === "prepare_rejected" && notice.detail !== void 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
								className: inject_module_css_default["noticeDetail"],
								children: [" ", notice.detail]
							}) : null]
						}) : null,
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: inject_module_css_default["targetRow"],
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: inject_module_css_default["planKey"],
								children: t$1("inject.targetLabel")
							}), props.target !== null ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
								className: inject_module_css_default["planValue"],
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Pill, {
									className: inject_module_css_default["agentTag"],
									children: props.target.agent
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: inject_module_css_default["planTitle"],
									title: props.target.sessionId,
									children: props.target.title !== void 0 && props.target.title !== "" ? props.target.title : props.target.sessionId
								})]
							}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: inject_module_css_default["noTarget"],
								children: t$1("inject.noTarget")
							})]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: inject_module_css_default["field"],
							children: [
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("label", {
									className: inject_module_css_default["label"],
									htmlFor: textareaId,
									children: t$1("inject.messageLabel")
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("textarea", {
									id: textareaId,
									className: inject_module_css_default["textarea"],
									value: draft,
									placeholder: t$1("inject.messagePlaceholder"),
									disabled: !canEdit,
									rows: 5,
									autoFocus: true,
									onChange: (event) => {
										setDraft(event.target.value);
									}
								}),
								/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
									className: inject_module_css_default["byteRow"],
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
										className: inject_module_css_default["byteBar"],
										"aria-hidden": true,
										children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
											className: usage.over ? `${inject_module_css_default["byteFill"]} ${inject_module_css_default["byteFillOver"]}` : inject_module_css_default["byteFill"],
											style: { width: `${usage.ratio * 100}%` }
										})
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: usage.over ? inject_module_css_default["byteTextOver"] : inject_module_css_default["byteText"],
										children: t$1("inject.byteCount", {
											bytes: usage.bytes,
											limit: usage.limit
										})
									})]
								}),
								showInvalid && !validation.ok ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
									className: inject_module_css_default["invalid"],
									role: "alert",
									children: renderCopy(t$1, messageInvalidCopy(validation))
								}) : null
							]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("fieldset", {
							className: inject_module_css_default["modes"],
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("legend", {
								className: inject_module_css_default["label"],
								children: t$1("inject.modeLabel")
							}), ["queue", "steer"].map((option) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("label", {
								className: inject_module_css_default["modeOption"],
								children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
									type: "radio",
									name: modeGroup,
									value: option,
									checked: mode === option,
									disabled: !canEdit,
									onChange: () => {
										setMode(option);
									}
								}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
									className: inject_module_css_default["modeText"],
									children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: inject_module_css_default["modeLabel"],
										children: t$1(MODE_COPY[option].label)
									}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
										className: inject_module_css_default["modeHint"],
										children: t$1(MODE_COPY[option].hint)
									})]
								})]
							}, option))]
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)(Warnings, {
							t: t$1,
							agent: props.target?.agent ?? null
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
							className: inject_module_css_default["footer"],
							children: [closeButton, /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
								type: "button",
								size: "sm",
								variant: "primary",
								disabled: !gate.canPrepare,
								onClick: () => {
									handlePrepare();
								},
								children: t$1(preparing ? "inject.preparing" : "inject.prepare")
							})]
						})
					]
				})]
			});
		}
		//#endregion
		//#region src/client/detail/transport.ts
		/**
		* Transport helpers for the detail view's M3 read endpoints. api.ts (T2.1)
		* predates M3: its `fetchSession` is typed for the M1 placeholder response
		* and it has no timeline-pagination call — and per the task boundary its
		* existing exports must not change (the S7 integration wave unifies the
		* data layer). So this module carries the missing calls with the same
		* posture as api.ts (same-origin relative paths, bounded timeout,
		* normalized ApiError, injectable primitives), reusing api.ts's exported
		* building blocks instead of redefining them.
		*
		* Read-only surface, no retry policy here (transport, not policy).
		*
		* @module
		*/
		const defaultSetTimeout$1 = (fn, ms) => globalThis.setTimeout(fn, ms);
		const defaultClearTimeout$1 = (handle) => {
			globalThis.clearTimeout(handle);
		};
		const defaultCreateAbortController$1 = () => new AbortController();
		function resolveFetch$1(opts) {
			if (opts.fetch !== void 0) return opts.fetch;
			return globalThis.fetch;
		}
		async function getJson$1(path, opts) {
			const doFetch = resolveFetch$1(opts);
			const controller = (opts.createAbortController ?? defaultCreateAbortController$1)();
			const setT = opts.setTimeout ?? defaultSetTimeout$1;
			const clearT = opts.clearTimeout ?? defaultClearTimeout$1;
			let timedOut = false;
			let externallyAborted = false;
			const timer = setT(() => {
				timedOut = true;
				controller.abort();
			}, opts.timeoutMs ?? 15e3);
			const external = opts.signal;
			const onExternalAbort = () => {
				externallyAborted = true;
				controller.abort();
			};
			if (external !== void 0) if (external.aborted) onExternalAbort();
			else external.addEventListener("abort", onExternalAbort);
			try {
				let res;
				try {
					res = await doFetch(path, {
						method: "GET",
						signal: controller.signal
					});
				} catch (err) {
					if (timedOut) throw new ApiError("timeout", "request_timeout", null, err);
					if (externallyAborted) throw new ApiError("aborted", "request_aborted", null, err);
					throw new ApiError("network", "network_error", null, err);
				}
				if (!res.ok) {
					let reason = `http_${res.status}`;
					try {
						const body = await res.json();
						if (typeof body === "object" && body !== null) {
							const value = body["reason"];
							if (typeof value === "string" && value !== "") reason = value;
						}
					} catch {}
					throw new ApiError("http", reason, res.status);
				}
				try {
					return await res.json();
				} catch (err) {
					if (timedOut) throw new ApiError("timeout", "request_timeout", null, err);
					throw new ApiError("parse", "invalid_json", res.status, err);
				}
			} finally {
				clearT(timer);
				if (external !== void 0) external.removeEventListener("abort", onExternalAbort);
			}
		}
		/**
		* `GET <prefix>/session/<id>` typed for the M3 body. Unknown ids reject
		* with an ApiError carrying the server's `session_not_found` reason; a
		* pre-M3 host answers `timeline: null` (the caller degrades honestly).
		*/
		async function fetchSessionDetail(sessionId, opts = {}) {
			return await getJson$1(`${API_PREFIX}/session/${encodeURIComponent(sessionId)}`, opts);
		}
		/**
		* `GET <prefix>/session/<id>/timeline?cursor=&limit=` — one older history
		* page. `cursor` is the opaque `nextCursor` token from a previous page,
		* passed through verbatim (the server rejects tampered tokens with 400
		* `invalid_cursor`); omit it for the newest window (listen-mode refetch).
		*/
		async function fetchTimelinePage(sessionId, opts = {}) {
			const params = new URLSearchParams();
			if (opts.cursor !== void 0 && opts.cursor !== null && opts.cursor !== "") params.set("cursor", opts.cursor);
			if (opts.limit !== void 0) params.set("limit", String(opts.limit));
			const query = params.toString();
			return await getJson$1(`${API_PREFIX}/session/${encodeURIComponent(sessionId)}/timeline${query === "" ? "" : `?${query}`}`, opts);
		}
		//#endregion
		//#region src/client/m3-transport.ts
		/**
		* Transport helpers for the M3 deep-query read endpoints (lineage /
		* search / projects), completing what detail/transport.ts started for
		* `session/<id>` + timeline. Same posture as api.ts: same-origin relative
		* paths under {@link API_PREFIX}, bounded timeout, normalized ApiError,
		* injectable browser primitives so node tests run without a DOM.
		*
		* Response typing reuses the components' own wire mirrors
		* (dsh-tools/logic.ts, board/project-view-logic.ts) so the transport and
		* the render pipelines can never disagree about a shape.
		*
		* Read-only surface, no retry policy here (transport, not policy).
		*
		* @module
		*/
		const defaultSetTimeout = (fn, ms) => globalThis.setTimeout(fn, ms);
		const defaultClearTimeout = (handle) => {
			globalThis.clearTimeout(handle);
		};
		const defaultCreateAbortController = () => new AbortController();
		function resolveFetch(opts) {
			if (opts.fetch !== void 0) return opts.fetch;
			return globalThis.fetch;
		}
		async function getJson(path, opts) {
			const doFetch = resolveFetch(opts);
			const controller = (opts.createAbortController ?? defaultCreateAbortController)();
			const setT = opts.setTimeout ?? defaultSetTimeout;
			const clearT = opts.clearTimeout ?? defaultClearTimeout;
			let timedOut = false;
			let externallyAborted = false;
			const timer = setT(() => {
				timedOut = true;
				controller.abort();
			}, opts.timeoutMs ?? 15e3);
			const external = opts.signal;
			const onExternalAbort = () => {
				externallyAborted = true;
				controller.abort();
			};
			if (external !== void 0) if (external.aborted) onExternalAbort();
			else external.addEventListener("abort", onExternalAbort);
			try {
				let res;
				try {
					res = await doFetch(path, {
						method: "GET",
						signal: controller.signal
					});
				} catch (err) {
					if (timedOut) throw new ApiError("timeout", "request_timeout", null, err);
					if (externallyAborted) throw new ApiError("aborted", "request_aborted", null, err);
					throw new ApiError("network", "network_error", null, err);
				}
				if (!res.ok) {
					let reason = `http_${res.status}`;
					try {
						const body = await res.json();
						if (typeof body === "object" && body !== null) {
							const value = body["reason"];
							if (typeof value === "string" && value !== "") reason = value;
						}
					} catch {}
					throw new ApiError("http", reason, res.status);
				}
				try {
					return await res.json();
				} catch (err) {
					if (timedOut) throw new ApiError("timeout", "request_timeout", null, err);
					throw new ApiError("parse", "invalid_json", res.status, err);
				}
			} finally {
				clearT(timer);
				if (external !== void 0) external.removeEventListener("abort", onExternalAbort);
			}
		}
		/**
		* `GET <prefix>/lineage/<id>` — dsh lineage trace. Degradation
		* (sessionQuery absent / trace failed) is DATA: the host answers 200 with
		* `{available:false, reason}`, so only transport/HTTP failures reject
		* (e.g. 501 `fusion_not_wired` on a pre-M3 host).
		*/
		async function fetchLineage(sessionId, opts = {}) {
			return await getJson(`${API_PREFIX}/lineage/${encodeURIComponent(sessionId)}`, opts);
		}
		/**
		* `GET <prefix>/search?q=&project=&limit=` — cross-agent session search.
		* At least one of `q` / `project` must be non-blank (the host answers 400
		* `invalid_request` otherwise — callers gate before dialing). The response
		* echoes the mode; `filter-only` is the honest degradation, not an error.
		*/
		async function fetchSearch(opts = {}) {
			const params = new URLSearchParams();
			if (opts.q !== void 0 && opts.q.trim() !== "") params.set("q", opts.q);
			if (opts.project !== void 0 && opts.project !== null && opts.project.trim() !== "") params.set("project", opts.project);
			if (opts.limit !== void 0) params.set("limit", String(opts.limit));
			return await getJson(`${API_PREFIX}/search?${params.toString()}`, opts);
		}
		/** `GET <prefix>/projects` — cross-agent project groups. */
		async function fetchProjects(opts = {}) {
			return await getJson(`${API_PREFIX}/projects`, opts);
		}
		//#endregion
		//#region src/client/detail-glue.ts
		/**
		* Session-detail data glue (T5.10b): one framework-free store per opened
		* detail view, feeding the controlled SessionDetail / LineageTree
		* components. Owns transport orchestration only — accumulation and
		* presentation stay in detail/logic.ts and dsh-tools/logic.ts:
		*
		* - initial load via detail/transport.ts `fetchSessionDetail` (header +
		*   newest timeline page in one round-trip);
		* - older-history pagination via `fetchTimelinePage(cursor)`;
		* - listen mode: the controller's SSE `state` frames carry no per-session
		*   events (ADR-2/ADR-3), so the integration calls {@link
		*   DetailStore.notifySnapshot} on every frame and the store refetches the
		*   newest window — coalesced so at most one refetch is in flight and at
		*   most one more is queued;
		* - dsh-exclusive lineage: fetched only for dsh sessions; non-dsh agents
		*   get the client-minted `not_dsh_session` degradation without dialing
		*   (dsh-tools/logic.ts `externalLineageFallback`).
		*
		* Same store discipline as controller.ts: subscribe/getState for
		* `useSyncExternalStore`, immutable state snapshots, `dispose()` makes
		* late settlements no-ops. All transport is injectable for node tests.
		*
		* @module
		*/
		const EMPTY_HEADER = {
			agent: "",
			title: "",
			project: "",
			status: ""
		};
		const INITIAL_LINEAGE = {
			loading: true,
			error: null,
			available: false,
			reason: null,
			detail: null,
			trace: null
		};
		/** Map any settlement failure to a stable machine reason code. */
		function reasonOf(err) {
			return isApiError(err) ? err.reason : "network_error";
		}
		var DetailStore = class {
			state;
			listeners = /* @__PURE__ */ new Set();
			fetchDetailFn;
			fetchPageFn;
			fetchLineageFn;
			listenLimit;
			disposed = false;
			opened = false;
			paging = false;
			listenInFlight = false;
			listenQueued = false;
			refreshInFlight = false;
			lineageStarted = false;
			constructor(sessionId, options = {}) {
				this.fetchDetailFn = options.fetchDetailFn ?? fetchSessionDetail;
				this.fetchPageFn = options.fetchPageFn ?? fetchTimelinePage;
				this.fetchLineageFn = options.fetchLineageFn ?? fetchLineage;
				this.listenLimit = options.listenLimit;
				this.state = {
					sessionId,
					header: options.hint ?? EMPTY_HEADER,
					timeline: createTimelineVM(sessionId),
					loading: false,
					error: null,
					hasMore: false,
					listening: false,
					refreshing: false,
					ready: false,
					lineage: INITIAL_LINEAGE
				};
			}
			subscribe = (fn) => {
				this.listeners.add(fn);
				return () => {
					this.listeners.delete(fn);
				};
			};
			getState = () => this.state;
			setState(patch) {
				if (this.disposed) return;
				this.state = {
					...this.state,
					...patch
				};
				for (const fn of [...this.listeners]) fn();
			}
			/** Initial load: header + newest timeline page, then lineage. Idempotent. */
			async open() {
				if (this.opened || this.disposed) return;
				this.opened = true;
				this.setState({
					loading: true,
					error: null
				});
				try {
					const wire = await this.fetchDetailFn(this.state.sessionId);
					if (this.disposed) return;
					const header = headerFromDetailWire(wire) ?? this.state.header;
					if (wire.timeline === null) this.setState({
						header,
						loading: false,
						error: "fusion_not_wired"
					});
					else {
						const timeline = applyTimelinePage(createTimelineVM(this.state.sessionId), wire.timeline);
						this.setState({
							header,
							timeline,
							loading: false,
							error: null,
							hasMore: timeline.nextCursor !== null,
							ready: true
						});
					}
					this.loadLineage(header.agent);
				} catch (err) {
					if (this.disposed) return;
					this.setState({
						loading: false,
						error: reasonOf(err)
					});
					this.loadLineage(this.state.header.agent);
				}
			}
			/** Fetch one older history page via the accumulated cursor. */
			async loadMore() {
				const { timeline } = this.state;
				if (this.disposed || this.paging || !this.state.ready) return;
				if (timeline.nextCursor === null) return;
				this.paging = true;
				this.setState({
					loading: true,
					error: null
				});
				try {
					const page = await this.fetchPageFn(this.state.sessionId, { cursor: timeline.nextCursor });
					if (this.disposed) return;
					const next = applyTimelinePage(this.state.timeline, page);
					this.setState({
						timeline: next,
						loading: false,
						hasMore: next.nextCursor !== null
					});
				} catch (err) {
					if (this.disposed) return;
					this.setState({
						loading: false,
						error: reasonOf(err)
					});
				} finally {
					this.paging = false;
				}
			}
			/** Flip listen mode; turning it on refetches the newest window at once. */
			toggleListen() {
				const listening = !this.state.listening;
				this.setState({ listening });
				if (listening) this.scheduleListenRefetch();
			}
			/**
			* Manual newest-window refetch with visible feedback (UX-07), also fired
			* once after a delivered injection (UX-05 observation loop). Unlike the
			* silent best-effort listen refetch, it reports in-flight state and
			* surfaces a failure reason (rendered as the inline banner). Appended
			* entries get the listen-merge highlight. Coalesced: at most one manual
			* refresh in flight, extra calls are dropped.
			*/
			async refreshNewest() {
				if (this.disposed || !this.state.ready || this.refreshInFlight) return;
				this.refreshInFlight = true;
				this.setState({
					refreshing: true,
					error: null
				});
				try {
					const page = await this.fetchPageFn(this.state.sessionId, { ...this.listenLimit !== void 0 ? { limit: this.listenLimit } : {} });
					if (this.disposed) return;
					this.setState({
						timeline: applyListenPage(this.state.timeline, page),
						refreshing: false
					});
				} catch (err) {
					if (this.disposed) return;
					this.setState({
						refreshing: false,
						error: reasonOf(err)
					});
				} finally {
					this.refreshInFlight = false;
				}
			}
			/**
			* SSE `state` frame hook (one call per controller notification). Refreshes
			* the header from the live board card when given, and in listen mode
			* triggers a coalesced newest-window refetch.
			*/
			notifySnapshot(card) {
				if (this.disposed) return;
				if (card !== null) {
					const h = this.state.header;
					if (card.agent !== h.agent || card.title !== h.title || card.project !== h.project || card.status !== h.status) this.setState({ header: card });
				}
				if (this.state.listening && this.state.ready) this.scheduleListenRefetch();
			}
			/** At most one refetch in flight; at most one more queued (idempotence). */
			scheduleListenRefetch() {
				if (this.disposed || !this.state.ready) return;
				if (this.listenInFlight) {
					this.listenQueued = true;
					return;
				}
				this.listenInFlight = true;
				this.runListenRefetch();
			}
			async runListenRefetch() {
				try {
					const page = await this.fetchPageFn(this.state.sessionId, { ...this.listenLimit !== void 0 ? { limit: this.listenLimit } : {} });
					if (this.disposed) return;
					this.setState({ timeline: applyListenPage(this.state.timeline, page) });
				} catch {} finally {
					this.listenInFlight = false;
					if (this.listenQueued && !this.disposed) {
						this.listenQueued = false;
						this.scheduleListenRefetch();
					}
				}
			}
			/** Resolve the lineage slice once (dsh-only capability; see module doc). */
			async loadLineage(agent) {
				if (this.lineageStarted || this.disposed) return;
				if (agent.trim() === "") {
					this.lineageStarted = true;
					this.setState({ lineage: {
						loading: false,
						error: null,
						available: false,
						reason: "not_dsh_session",
						detail: null,
						trace: null
					} });
					return;
				}
				this.lineageStarted = true;
				const fallback = externalLineageFallback(agent);
				if (fallback !== null) {
					this.setState({ lineage: {
						loading: false,
						error: null,
						available: fallback.available,
						reason: fallback.reason,
						detail: null,
						trace: fallback.trace
					} });
					return;
				}
				try {
					const body = await this.fetchLineageFn(this.state.sessionId);
					if (this.disposed) return;
					this.setState({ lineage: {
						loading: false,
						error: null,
						available: body.available,
						reason: body.reason,
						detail: body.detail ?? null,
						trace: body.trace
					} });
				} catch (err) {
					if (this.disposed) return;
					this.setState({ lineage: {
						...INITIAL_LINEAGE,
						loading: false,
						error: reasonOf(err)
					} });
				}
			}
			/** Late settlements become no-ops; subscribers are dropped. Idempotent. */
			dispose() {
				this.disposed = true;
				this.listeners.clear();
			}
		};
		/**
		* Authoritative header from the M3 detail body: the fused row wins (it
		* merges both sources), the sidecar board row covers fusion-less hosts;
		* null when the body carries neither (caller keeps its hint).
		*/
		function headerFromDetailWire(wire) {
			if (wire.unified !== null) return {
				agent: wire.unified.agent,
				title: wire.unified.title,
				project: wire.unified.project,
				status: wire.unified.status
			};
			if (wire.session !== null) return {
				agent: wire.session.agent,
				title: wire.session.title,
				project: wire.session.project,
				status: wire.session.status
			};
			return null;
		}
		/**
		* Find the live board card of a session (controller SessionCardVM rows) →
		* header hint, or null when off-board.
		*/
		function findCardHint(sessions, sessionId) {
			const card = sessions.find((s) => s.sessionId === sessionId);
			if (card === void 0) return null;
			return {
				agent: card.agent,
				title: card.title,
				project: card.project,
				status: card.status
			};
		}
		//#endregion
		//#region \0dsh-css:src/client/detail-view.module.css.mjs
		const css$3 = ".T5Nmwa_switcherBar{align-items:center;gap:4px;padding:8px 12px 0;display:flex}.T5Nmwa_switcherButton{color:var(--dsw-alias-label-secondary);cursor:pointer;background:0 0;border:1px solid #0000;border-radius:999px;padding:3px 10px;font-size:12px}.T5Nmwa_switcherButton:hover{background:var(--dsw-alias-bg-layer-1)}.T5Nmwa_switcherButton[data-active=true]{color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-layer-2);border-color:var(--dsw-alias-border-l2);font-weight:600}.T5Nmwa_detailRoot{box-sizing:border-box;flex-direction:column;gap:12px;min-width:0;height:100%;padding:12px;display:flex;overflow-y:auto}.T5Nmwa_actionsRow{flex-wrap:wrap;align-items:center;gap:8px;display:flex}.T5Nmwa_analysisDisabledReason{max-width:360px;color:var(--agsc-fg-secondary);font-size:11px;line-height:16px}.T5Nmwa_toolsSection{border:1px solid var(--agsc-border);border-radius:var(--agsc-radius-control);flex-direction:column;gap:16px;padding:8px 12px;display:flex}.T5Nmwa_toolsToggle{align-self:flex-start}.T5Nmwa_toolsToggleGlyph{color:var(--agsc-fg-dimmed);flex:none;font-size:10px}.T5Nmwa_injectDialog{gap:0;width:min(560px,100%);max-height:min(85vh,720px);padding:0;overflow:auto}.T5Nmwa_injectDialog>[data-dsh-part=inject-panel]{border-radius:inherit;background:0 0;border:0;width:100%;max-width:none}";
		const tagId$3 = "@shendeguize/dsh-agent-sidecar/src/client/detail-view.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$3, css$3);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$3) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$3;
				created = true;
			}
			tag.textContent = css$3;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var detail_view_module_css_default = {
			"actionsRow": "T5Nmwa_actionsRow",
			"analysisDisabledReason": "T5Nmwa_analysisDisabledReason",
			"detailRoot": "T5Nmwa_detailRoot",
			"injectDialog": "T5Nmwa_injectDialog",
			"switcherBar": "T5Nmwa_switcherBar",
			"switcherButton": "T5Nmwa_switcherButton",
			"toolsSection": "T5Nmwa_toolsSection",
			"toolsToggle": "T5Nmwa_toolsToggle",
			"toolsToggleGlyph": "T5Nmwa_toolsToggleGlyph"
		};
		//#endregion
		//#region src/client/detail-view.tsx
		/**
		* Session-detail React container. It composes the timeline, lineage,
		* search, analysis, and optional injection surfaces over stores supplied
		* by {@link DetailUiPort}.
		*
		* Stores are scoped to one opened session and disposed on unmount. Each
		* controller state frame refreshes the header hint and drives the
		* detail store's bounded listen-mode refetch.
		*
		* @module
		*/
		const ANALYSIS_DISABLED_REASON_ID = "agent-sidecar-analysis-disabled-reason";
		/**
		* The detail view. Owner remounts it per session id (`key={sessionId}`),
		* so every store below is scoped to exactly one session.
		*/
		function SidecarDetailView(props) {
			const { controller, integration, sessionId } = props;
			const [detailStore] = (0, react.useState)(() => integration.createDetailStore(sessionId, props.hint));
			const [searchStore] = (0, react.useState)(() => integration.createSearchStore());
			const [analysisStore] = (0, react.useState)(() => integration.createAnalysisStore());
			const [injectOpen, setInjectOpen] = (0, react.useState)(false);
			const [analysisOpen, setAnalysisOpen] = (0, react.useState)(false);
			const [toolsOpen, setToolsOpen] = (0, react.useState)(false);
			(0, react.useEffect)(() => {
				detailStore.open();
				const unsubscribe = controller.subscribe(() => {
					detailStore.notifySnapshot(findCardHint(controller.getState().sessions, sessionId));
				});
				return () => {
					unsubscribe();
					detailStore.dispose();
					searchStore.dispose();
					analysisStore.dispose();
				};
			}, [
				controller,
				detailStore,
				searchStore,
				analysisStore,
				sessionId
			]);
			const detail = (0, react.useSyncExternalStore)(detailStore.subscribe, detailStore.getState, detailStore.getState);
			const search = (0, react.useSyncExternalStore)(searchStore.subscribe, searchStore.getState, searchStore.getState);
			const analysis = (0, react.useSyncExternalStore)(analysisStore.subscribe, analysisStore.getState, analysisStore.getState);
			const view = (0, react.useSyncExternalStore)((cb) => controller.subscribe(cb), () => controller.getState(), () => controller.getState());
			const analysisEnabled = integration.getAnalysisEnabled();
			const analysisDisabledHint = analysisEnabled ? void 0 : t("detail.actions.analyzeDisabledHint");
			const injectIntegration = props.integration.inject;
			const closeInject = () => {
				setInjectOpen(false);
			};
			const title = detail.header.title.trim();
			const injectActions = injectIntegration === void 0 ? void 0 : {
				onPrepare: injectIntegration.actions.onPrepare,
				onExecute: async (req) => {
					const result = await injectIntegration.actions.onExecute(req);
					if (isDeliveredResult(result)) detailStore.refreshNewest();
					return result;
				}
			};
			const observeReaction = () => {
				if (!detailStore.getState().listening) detailStore.toggleListen();
				closeInject();
			};
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				...surfaceProps("detail", detail_view_module_css_default["detailRoot"]),
				"data-testid": "agent-sidecar-detail-view",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						className: detail_view_module_css_default["actionsRow"],
						children: [
							injectIntegration !== void 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
								size: "sm",
								variant: "outline",
								onClick: () => {
									setInjectOpen(true);
								},
								"data-testid": "agent-sidecar-detail-inject",
								children: t("detail.actions.inject")
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Button, {
								size: "sm",
								variant: "outline",
								disabled: !analysisEnabled,
								title: analysisDisabledHint,
								"aria-describedby": analysisEnabled ? void 0 : ANALYSIS_DISABLED_REASON_ID,
								onClick: () => {
									setAnalysisOpen(true);
								},
								"data-testid": "agent-sidecar-detail-analyze",
								children: t("detail.actions.analyze")
							}),
							!analysisEnabled && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								id: ANALYSIS_DISABLED_REASON_ID,
								className: detail_view_module_css_default["analysisDisabledReason"],
								children: analysisDisabledHint
							})
						]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
						...surfaceProps("dsh-tools", detail_view_module_css_default["toolsSection"]),
						"data-testid": "agent-sidecar-detail-tools",
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Button, {
							size: "sm",
							variant: "ghost",
							className: detail_view_module_css_default["toolsToggle"],
							"aria-expanded": toolsOpen,
							onClick: () => {
								setToolsOpen((open) => !open);
							},
							children: [
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: detail_view_module_css_default["toolsToggleGlyph"],
									"aria-hidden": true,
									children: toolsOpen ? "▾" : "▸"
								}),
								t("detail.tools.title"),
								/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
									className: detail_view_module_css_default["toolsToggleGlyph"],
									children: toolsOpen ? t("detail.tools.hide") : t("detail.tools.show")
								})
							]
						}), toolsOpen && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(LineageTree, {
							trace: detail.lineage.trace,
							available: detail.lineage.available,
							reason: detail.lineage.reason,
							detail: detail.lineage.detail,
							currentSessionId: sessionId,
							onSelectSession: props.onSelectSession,
							loading: detail.lineage.loading,
							error: detail.lineage.error
						}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)(SearchPanel, {
							query: search.query,
							project: search.project,
							mode: search.mode,
							items: search.items,
							loading: search.loading,
							error: search.error,
							onQueryChange: (query) => {
								searchStore.setQuery(query);
							},
							onSubmit: () => {
								searchStore.submit();
							},
							onSelectSession: props.onSelectSession
						})] })]
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)(SessionDetail, {
						sessionId,
						header: detail.header,
						timeline: detail.timeline,
						loading: detail.loading,
						error: detail.error,
						hasMore: detail.hasMore,
						listening: detail.listening,
						refreshing: detail.refreshing,
						onLoadMore: () => {
							detailStore.loadMore();
						},
						onToggleListen: () => {
							detailStore.toggleListen();
						},
						onRefresh: () => {
							detailStore.refreshNewest();
						},
						onClose: props.onClose
					}),
					analysisOpen && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(AnalysisPanel, {
						enabled: analysisEnabled,
						state: analysis,
						onStart: () => {
							analysisStore.start({
								targetKind: "session",
								targetId: sessionId
							});
						},
						onFollowup: (question) => {
							analysisStore.followup(question);
						},
						onStop: () => {
							analysisStore.stop();
						},
						onClose: () => {
							setAnalysisOpen(false);
						}
					}),
					injectIntegration !== void 0 && injectActions !== void 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Modal, {
						open: injectOpen,
						onClose: closeInject,
						title: t("inject.title"),
						closeLabel: t("inject.close"),
						className: detail_view_module_css_default["injectDialog"],
						headless: true,
						children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)(InjectPanel, {
							capability: { inject: view.injectCapability },
							target: {
								agent: detail.header.agent,
								sessionId,
								...title !== "" ? { title } : {}
							},
							defaultMode: injectIntegration.getDefaultMode(),
							onPrepare: injectActions.onPrepare,
							onExecute: injectActions.onExecute,
							onClose: closeInject,
							onObserve: observeReaction
						})
					})
				]
			});
		}
		var ProjectsStore = class {
			state = {
				groups: [],
				loading: false,
				error: null,
				loadedAt: null
			};
			listeners = /* @__PURE__ */ new Set();
			fetchProjectsFn;
			minRefreshMs;
			now;
			disposed = false;
			inFlight = false;
			lastAttemptAt = null;
			constructor(options = {}) {
				this.fetchProjectsFn = options.fetchProjectsFn ?? fetchProjects;
				this.minRefreshMs = options.minRefreshMs ?? 5e3;
				this.now = options.now ?? Date.now;
			}
			subscribe = (fn) => {
				this.listeners.add(fn);
				return () => {
					this.listeners.delete(fn);
				};
			};
			getState = () => this.state;
			setState(patch) {
				if (this.disposed) return;
				this.state = {
					...this.state,
					...patch
				};
				for (const fn of [...this.listeners]) fn();
			}
			/** Fetch the groups now (deduped while one call is in flight). */
			async refresh() {
				if (this.disposed || this.inFlight) return;
				this.inFlight = true;
				this.lastAttemptAt = this.now();
				if (this.state.loadedAt === null) this.setState({
					loading: true,
					error: null
				});
				try {
					const body = await this.fetchProjectsFn();
					if (this.disposed) return;
					this.setState({
						groups: body.groups,
						loading: false,
						error: null,
						loadedAt: this.now()
					});
				} catch (err) {
					if (this.disposed) return;
					this.setState({
						loading: false,
						error: isApiError(err) ? err.reason : "network_error"
					});
				} finally {
					this.inFlight = false;
				}
			}
			/** SSE `state` frame hook: throttled refresh. */
			notifySnapshot() {
				if (this.disposed || this.inFlight) return;
				if (this.lastAttemptAt !== null && this.now() - this.lastAttemptAt < this.minRefreshMs) return;
				this.refresh();
			}
			dispose() {
				this.disposed = true;
				this.listeners.clear();
			}
		};
		/**
		* Find a session inside loaded project groups → header hint for the
		* detail view (project rows carry no board card), or null when unknown.
		*/
		function findProjectSessionHint(groups, sessionId) {
			for (const group of groups) for (const session of group.sessions) if (session.sessionId === sessionId) return {
				agent: session.agent,
				title: session.title,
				project: group.project,
				status: session.status
			};
			return null;
		}
		//#endregion
		//#region src/client/locales/react.ts
		/**
		* Subscribe the calling React root to the module-owned active locale.
		* Reading translations remains late-bound through `t` and locale facades.
		*/
		function useActiveLocale() {
			return (0, react.useSyncExternalStore)(subscribeLocale, getLocale, getLocale);
		}
		//#endregion
		//#region \0dsh-css:src/client/navigation/center-overlay.module.css.mjs
		const css$2 = ".nkYc2a_dialog{border-color:var(--agsc-border-strong);border-radius:var(--agsc-radius-card);background:var(--agsc-bg);width:min(1180px,100vw - 48px);height:min(860px,100vh - 48px);max-height:calc(100vh - 48px);box-shadow:var(--agsc-shadow-card);gap:0;padding:0}.nkYc2a_content{height:100%;min-height:0;overflow:hidden}.nkYc2a_content>:last-child{flex:1;min-height:0;margin-top:0;padding:0;overflow:hidden}.nkYc2a_surface{min-width:0;height:100%;min-height:0;color:var(--agsc-fg);background:var(--agsc-bg);flex-direction:column;display:flex;overflow:hidden}.nkYc2a_surface>:last-child{flex:1;height:auto;min-height:0}@media (width<=720px){.nkYc2a_dialog{border-radius:var(--agsc-radius-control);width:calc(100vw - 16px);height:calc(100dvh - 16px);max-height:calc(100dvh - 16px)}}";
		const tagId$2 = "@shendeguize/dsh-agent-sidecar/src/client/navigation/center-overlay.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$2, css$2);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$2) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$2;
				created = true;
			}
			tag.textContent = css$2;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var center_overlay_module_css_default = {
			"content": "nkYc2a_content",
			"dialog": "nkYc2a_dialog",
			"surface": "nkYc2a_surface"
		};
		//#endregion
		//#region src/client/navigation/modal-isolation.ts
		const DIALOG_SELECTOR$1 = "[role=\"dialog\"][aria-modal=\"true\"]";
		const FOCUSABLE_SELECTOR = [
			"a[href],area[href],button:not([disabled])",
			"input:not([disabled]):not([type=\"hidden\"]),select:not([disabled])",
			"textarea:not([disabled]),iframe,[contenteditable=\"true\"],[tabindex]"
		].join(",");
		function focusableElements(dialog) {
			return Array.from(dialog.querySelectorAll(FOCUSABLE_SELECTOR)).filter((element) => element.tabIndex >= 0 && !element.hidden && element.closest("[inert],[aria-hidden=\"true\"]") === null && element.getClientRects().length > 0);
		}
		function restorableIn(element, dialog) {
			return element?.isConnected === true && dialog.contains(element) && !element.hidden && element.closest("[inert],[aria-hidden=\"true\"]") === null && element.getClientRects().length > 0;
		}
		/** Isolate the active official Modal, including sibling-portaled nested dialogs. */
		function useModalIsolation(open, surfaceRef) {
			(0, react.useEffect)(() => {
				if (!open || typeof document === "undefined" || typeof window === "undefined") return;
				const body = document.body;
				if (body === null || typeof window.MutationObserver === "undefined") return;
				const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
				const originalInert = /* @__PURE__ */ new Map();
				const dialogStack = [];
				let topDialog = null;
				let focusFrame = null;
				const getTopDialog = () => {
					const outer = surfaceRef.current?.closest(DIALOG_SELECTOR$1) ?? null;
					if (outer === null) return null;
					const dialogs = Array.from(body.querySelectorAll(DIALOG_SELECTOR$1));
					return dialogs.indexOf(outer) < 0 ? null : dialogs.at(-1) ?? null;
				};
				const bodyBranch = (dialog) => {
					let branch = dialog;
					while (branch.parentNode !== null && branch.parentNode !== body) branch = branch.parentNode;
					return branch instanceof HTMLElement ? branch : null;
				};
				const isolateAround = (dialog) => {
					const activeBranch = dialog === null ? null : bodyBranch(dialog);
					const next = new Set(Array.from(body.children).filter((element) => element instanceof HTMLElement && element !== activeBranch));
					for (const [element, wasInert] of originalInert) {
						if (next.has(element)) continue;
						element.inert = wasInert;
						originalInert.delete(element);
					}
					for (const element of next) {
						if (!originalInert.has(element)) originalInert.set(element, element.inert);
						element.inert = true;
					}
				};
				const queueFocus = (dialog, preferred = null) => {
					if (focusFrame !== null) window.cancelAnimationFrame(focusFrame);
					focusFrame = window.requestAnimationFrame(() => {
						focusFrame = null;
						if (getTopDialog() !== dialog) return;
						(restorableIn(preferred, dialog) ? preferred : focusableElements(dialog)[0])?.focus({ preventScroll: true });
					});
				};
				const sync = () => {
					const next = getTopDialog();
					const changed = next !== topDialog;
					let closed = [];
					if (changed && next !== null) {
						const index = dialogStack.findIndex((frame) => frame.dialog === next);
						if (index < 0) {
							const parent = dialogStack.at(-1);
							const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
							const opener = parent !== void 0 && restorableIn(active, parent.dialog) ? active : parent?.focus ?? null;
							dialogStack.push({
								dialog: next,
								opener,
								focus: null
							});
						} else closed = dialogStack.splice(index + 1);
					}
					topDialog = next;
					isolateAround(next);
					const opener = next === null ? null : closed.reverse().find((frame) => restorableIn(frame.opener, next))?.opener ?? null;
					if (next !== null && (changed || !next.contains(document.activeElement))) queueFocus(next, opener);
				};
				const onFocusIn = (event) => {
					if (!(event.target instanceof HTMLElement)) return;
					const dialog = event.target.closest(DIALOG_SELECTOR$1);
					const frame = dialogStack.find((item) => item.dialog === dialog);
					if (frame !== void 0) frame.focus = event.target;
				};
				const onKeyDown = (event) => {
					if (event.key !== "Tab" || event.defaultPrevented) return;
					const dialog = getTopDialog();
					if (dialog === null) return;
					if (dialog !== topDialog) sync();
					const focusable = focusableElements(dialog);
					if (focusable.length === 0) {
						event.preventDefault();
						return;
					}
					const activeIndex = focusable.indexOf(document.activeElement);
					const wrapsBackward = event.shiftKey && activeIndex <= 0;
					const wrapsForward = !event.shiftKey && activeIndex === focusable.length - 1;
					if (activeIndex < 0 || wrapsBackward || wrapsForward) {
						event.preventDefault();
						(event.shiftKey ? focusable.at(-1) : focusable[0])?.focus({ preventScroll: true });
					}
				};
				const observer = new window.MutationObserver(sync);
				observer.observe(body, {
					childList: true,
					subtree: true
				});
				document.addEventListener("focusin", onFocusIn);
				document.addEventListener("keydown", onKeyDown, true);
				sync();
				return () => {
					observer.disconnect();
					document.removeEventListener("focusin", onFocusIn);
					document.removeEventListener("keydown", onKeyDown, true);
					if (focusFrame !== null) window.cancelAnimationFrame(focusFrame);
					for (const [element, wasInert] of originalInert) element.inert = wasInert;
					if (previousFocus?.isConnected === true) previousFocus.focus({ preventScroll: true });
				};
			}, [open, surfaceRef]);
		}
		//#endregion
		//#region src/client/navigation/modal-surface-anchor.ts
		const DIALOG_SELECTOR = "[role=\"dialog\"][aria-modal=\"true\"]";
		const MODAL_SURFACE_OWNER = Symbol.for("@shendeguize/dsh-agent-sidecar/modal-surface-anchor-owner");
		/** Use a pre-paint effect in the browser without warning during SSR. */
		function selectModalSurfaceAnchorEffect(hasDocument) {
			return hasDocument ? react.useLayoutEffect : react.useEffect;
		}
		const useIsomorphicLayoutEffect = selectModalSurfaceAnchorEffect(typeof document !== "undefined");
		function ownerSlot(target) {
			return target;
		}
		/** Attach the public surface attributes with latest-owner-safe cleanup. */
		function attachModalSurfaceAnchor(target, attributes) {
			if (target === null) return () => {};
			const owner = Symbol("modal-surface-anchor");
			const slot = ownerSlot(target);
			slot[MODAL_SURFACE_OWNER] = owner;
			target.setAttribute("data-dsh-plugin", attributes["data-dsh-plugin"]);
			target.setAttribute("data-dsh-part", attributes["data-dsh-part"]);
			return () => {
				if (slot[MODAL_SURFACE_OWNER] !== owner) return;
				target.removeAttribute("data-dsh-plugin");
				target.removeAttribute("data-dsh-part");
				delete slot[MODAL_SURFACE_OWNER];
			};
		}
		/** Commit the public anchor onto the official Modal's real dialog element. */
		function useModalSurfaceAnchor(open, surfaceRef, attributes) {
			useIsomorphicLayoutEffect(() => {
				if (!open || typeof document === "undefined") return;
				return attachModalSurfaceAnchor(surfaceRef.current?.closest(DIALOG_SELECTOR) ?? null, attributes);
			}, [
				open,
				surfaceRef,
				attributes["data-dsh-plugin"],
				attributes["data-dsh-part"]
			]);
		}
		//#endregion
		//#region src/client/navigation/CenterOverlay.tsx
		/** Pure Agent Center presentation over the shell's frame-wide overlay seat. */
		function CenterOverlay(props) {
			const surfaceRef = (0, react.useRef)(null);
			const surface = surfaceProps("overlay", center_overlay_module_css_default["dialog"]);
			useModalIsolation(props.open, surfaceRef);
			useModalSurfaceAnchor(props.open, surfaceRef, surface);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Modal, {
				open: props.open,
				onClose: props.onClose,
				title: props.title,
				closeLabel: props.closeLabel,
				className: surface.className,
				contentClassName: center_overlay_module_css_default["content"],
				children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					ref: surfaceRef,
					className: center_overlay_module_css_default["surface"],
					children: props.children
				})
			});
		}
		//#endregion
		//#region src/client/settings-glue.ts
		/** Schema defaults of src/config.ts (fallback while the scope is not ready). */
		const DEFAULT_CONFIG_VIEW = {
			daemon: {
				policy: "adopt-or-host",
				backoffLimit: 5
			},
			sidecar: {
				command: ["agent-sidecar"],
				runtimeDir: ""
			},
			stream: {
				reconcileActiveMs: 2e3,
				reconcileIdleMs: 1e4
			},
			inject: {
				enabled: false,
				defaultMode: "queue"
			},
			analysis: {
				enabled: false,
				provider: "",
				model: ""
			},
			ui: {
				timeWindowHours: 24,
				showDead: false
			},
			skill: { provide: true }
		};
		/** Whitespace-join an argv for the card's single-line command field. */
		function joinCommand(argv) {
			return argv.join(" ");
		}
		/** Split the card's command line back into argv (whitespace, empty dropped). */
		function splitCommand(line) {
			return line.split(/\s+/).filter((part) => part !== "");
		}
		/** Config wire shape → the card's flat staged-form values. */
		function configToValues(config) {
			return {
				daemonPolicy: config.daemon.policy,
				daemonBackoffLimit: config.daemon.backoffLimit,
				sidecarCommand: joinCommand(config.sidecar.command),
				sidecarRuntimeDir: config.sidecar.runtimeDir,
				streamReconcileActiveMs: config.stream.reconcileActiveMs,
				streamReconcileIdleMs: config.stream.reconcileIdleMs,
				injectEnabled: config.inject.enabled,
				injectDefaultMode: config.inject.defaultMode,
				analysisEnabled: config.analysis.enabled,
				analysisProvider: config.analysis.provider,
				analysisModel: config.analysis.model,
				uiTimeWindowHours: config.ui.timeWindowHours,
				uiShowDead: config.ui.showDead,
				skillProvide: config.skill.provide
			};
		}
		/** Flat card values → the grouped config wire shape. */
		function valuesToConfigView(values) {
			return {
				daemon: {
					policy: values.daemonPolicy,
					backoffLimit: values.daemonBackoffLimit
				},
				sidecar: {
					command: splitCommand(values.sidecarCommand),
					runtimeDir: values.sidecarRuntimeDir
				},
				stream: {
					reconcileActiveMs: values.streamReconcileActiveMs,
					reconcileIdleMs: values.streamReconcileIdleMs
				},
				inject: {
					enabled: values.injectEnabled,
					defaultMode: values.injectDefaultMode
				},
				analysis: {
					enabled: values.analysisEnabled,
					provider: values.analysisProvider,
					model: values.analysisModel
				},
				ui: {
					timeWindowHours: values.uiTimeWindowHours,
					showDead: values.uiShowDead
				},
				skill: { provide: values.skillProvide }
			};
		}
		/** Field-wise equality over the flat card values (argv compared as joined text). */
		function cardValuesEqual(a, b) {
			return Object.keys(a).every((key) => a[key] === b[key]);
		}
		/**
		* Diff two card-value sets into per-group writes (see module doc for why the
		* write unit is a whole group). Order is the Config declaration order.
		*/
		function diffGroups(current, target) {
			const from = valuesToConfigView(current);
			const to = valuesToConfigView(target);
			const patches = [];
			for (const group of Object.keys(to)) if (JSON.stringify(from[group]) !== JSON.stringify(to[group])) patches.push({
				group,
				patch: { ...to[group] }
			});
			return patches;
		}
		//#endregion
		//#region src/client/mount.tsx
		/**
		* React bindings for the board tab, footer widget, and settings card.
		* Factories close over the controller so subscribe/getSnapshot identities
		* stay stable across renders. The board depends only on {@link BoardUiPort}
		* and passes its nested detail port to the detail container.
		* Each exported root factory's top-level component subscribes once to the
		* active locale, refreshing its complete descendant tree without leaf subscriptions.
		*
		* The settings card owns the staged-edit lifecycle over a bound
		* `SettingsScope` (browser mirror of the host settings namespace):
		* resolved values come from the scope snapshot, edits stage locally, save
		* writes one complete top-level group per changed group (see
		* settings-glue.ts for the write-granularity rationale), and success is
		* judged by comparing the post-write snapshot against the staged target —
		* `scope.set` settles without rejecting even when the host declines the
		* write (it recovers by reloading host state instead).
		*/
		/**
		* Project-correlation view bound to its store: refresh on entry, then
		* throttled SSE-driven refreshes for as long as the view is on screen.
		*/
		function ProjectsContainer(props) {
			const { controller, store } = props;
			(0, react.useEffect)(() => {
				store.refresh();
				return controller.subscribe(() => {
					store.notifySnapshot();
				});
			}, [controller, store]);
			const state = (0, react.useSyncExternalStore)(store.subscribe, store.getState, store.getState);
			return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ProjectView, {
				groups: state.groups,
				loading: state.loading,
				error: state.error === null ? null : detailErrorText(state.error),
				onSelectSession: props.onSelectSession
			});
		}
		/**
		* Cross-agent board content and its project/detail routes:
		*
		* - view 1: the session board, with a 「会话看板 / 项目视图」 switcher
		*   (ProjectView over `GET projects`);
		* - view 2: clicking a session card in EITHER view routes to the full-tab
		*   session-detail view (timeline + 注入 + AI 分析 + dsh 谱系/检索);
		*   detail-internal jumps (lineage nodes, search hits) re-route in place;
		* - view 3: the inject panel opens as a modal from the detail view.
		*
		* Without an integration the board renders read-only and inert (no detail
		* routing).
		*/
		function createBoardContent(controller, integration) {
			const subscribe = (cb) => controller.subscribe(cb);
			const getState = () => controller.getState();
			const getFilters = () => controller.getFilters();
			return function BoardContent() {
				const state = (0, react.useSyncExternalStore)(subscribe, getState, getState);
				const filters = (0, react.useSyncExternalStore)(subscribe, getFilters, getFilters);
				const [mainView, setMainView] = (0, react.useState)("board");
				const [detail, setDetail] = (0, react.useState)(null);
				const [projectsStore] = (0, react.useState)(() => integration?.createProjectsStore() ?? null);
				(0, react.useEffect)(() => () => {
					projectsStore?.dispose();
				}, [projectsStore]);
				const openDetail = (sessionId) => {
					if (integration === void 0) return;
					const hint = findCardHint(state.sessions, sessionId) ?? (projectsStore !== null ? findProjectSessionHint(projectsStore.getState().groups, sessionId) : null);
					setDetail({
						id: sessionId,
						hint
					});
				};
				if (integration !== void 0 && detail !== null) return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(SidecarDetailView, {
					sessionId: detail.id,
					hint: detail.hint,
					controller,
					integration: integration.detail,
					onClose: () => {
						setDetail(null);
					},
					onSelectSession: openDetail
				}, detail.id);
				return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(react_jsx_runtime.Fragment, { children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: detail_view_module_css_default["switcherBar"],
					"data-testid": "agent-sidecar-view-switcher",
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						className: detail_view_module_css_default["switcherButton"],
						"data-active": mainView === "board" || void 0,
						"aria-pressed": mainView === "board",
						onClick: () => {
							setMainView("board");
						},
						children: t("board.viewBoard")
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						className: detail_view_module_css_default["switcherButton"],
						"data-active": mainView === "projects" || void 0,
						"aria-pressed": mainView === "projects",
						onClick: () => {
							setMainView("projects");
						},
						children: t("board.viewProjects")
					})]
				}), mainView === "projects" && projectsStore !== null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ProjectsContainer, {
					controller,
					store: projectsStore,
					onSelectSession: openDetail
				}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)(Board, {
					daemonState: state.daemonState,
					...state.daemonDetail !== void 0 ? { daemonDetail: state.daemonDetail } : {},
					streamHealth: state.streamHealth,
					lastReconcileAtMs: state.lastReconcileAtMs,
					sessions: state.sessions,
					filters,
					onFiltersChange: (next) => {
						controller.setFilters(next);
					},
					onRefresh: () => controller.refresh(),
					onSelectSession: openDetail
				})] });
			};
		}
		/** Bind board content to its independent React root and locale subscription. */
		function createBoardTab(controller, integration) {
			const BoardContent = createBoardContent(controller, integration);
			return function SidecarBoardTab() {
				useActiveLocale();
				return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(BoardContent, {});
			};
		}
		/** Bind the shared navigation source to the shell overlay and existing board. */
		function createCenterOverlay(controller, integration, navigation) {
			const BoardContent = createBoardContent(controller, integration);
			return function SidecarCenterOverlay() {
				useActiveLocale();
				const open = (0, react.useSyncExternalStore)(navigation.subscribe, navigation.getSnapshot, navigation.getSnapshot);
				return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(CenterOverlay, {
					open,
					onClose: navigation.close,
					title: t("board.topbar.title"),
					closeLabel: t("inject.close"),
					children: open ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)(BoardContent, {}) : null
				});
			};
		}
		/** Footer connection dot + working counter bound to the controller. */
		function createFooterWidget(controller, onOpen) {
			const subscribe = (cb) => controller.subscribe(cb);
			const getState = () => controller.getState();
			return function SidecarFooterWidget() {
				useActiveLocale();
				const state = (0, react.useSyncExternalStore)(subscribe, getState, getState);
				return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(SidecarWidget, {
					connection: deriveWidgetConnection(state.daemonState, state.streamHealth),
					workingCount: countWorking(state.sessions),
					onOpen
				});
			};
		}
		/** Fallback card values while the scope snapshot is not ready. */
		const FALLBACK_VALUES = configToValues(DEFAULT_CONFIG_VIEW);
		/**
		* Settings card bound to the controller (daemon status row) and to the
		* namespace scope (values + persistence). See the module doc for the
		* staged-edit / save-verification contract.
		*/
		function createSettingsCardEntry(controller, scope) {
			const subscribeState = (cb) => controller.subscribe(cb);
			const getState = () => controller.getState();
			const subscribeScope = (cb) => scope.subscribe(cb);
			const getSnapshot = () => scope.getSnapshot();
			return function SidecarSettingsCardEntry() {
				useActiveLocale();
				const snapshot = (0, react.useSyncExternalStore)(subscribeScope, getSnapshot, getSnapshot);
				const state = (0, react.useSyncExternalStore)(subscribeState, getState, getState);
				const [staged, setStaged] = (0, react.useState)({});
				const [saving, setSaving] = (0, react.useState)(false);
				const [saveFailed, setSaveFailed] = (0, react.useState)(false);
				const resolved = snapshot.value !== void 0 ? configToValues(snapshot.value) : FALLBACK_VALUES;
				const values = {
					...resolved,
					...staged
				};
				const writable = snapshot.status === "ready" && snapshot.writable && snapshot.mode === "host";
				const dirty = !cardValuesEqual(values, resolved);
				const onChange = (field, value) => {
					setSaveFailed(false);
					setStaged((prev) => {
						const next = { ...prev };
						if (resolved[field] === value) delete next[field];
						else next[field] = value;
						return next;
					});
				};
				const onSave = () => {
					const target = {
						...resolved,
						...staged
					};
					setSaving(true);
					setSaveFailed(false);
					(async () => {
						try {
							for (const { group, patch } of diffGroups(resolved, target)) await scope.set(group, patch);
							const after = scope.getSnapshot().value;
							if (after !== void 0 && cardValuesEqual(configToValues(after), target)) setStaged({});
							else setSaveFailed(true);
						} catch (err) {
							console.error("agent-sidecar: settings save failed", err);
							setSaveFailed(true);
						} finally {
							setSaving(false);
						}
					})();
				};
				return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(SettingsCard, {
					values,
					onChange,
					onSave,
					onDiscard: () => {
						setStaged({});
						setSaveFailed(false);
					},
					writable,
					dirty,
					saving,
					saveFailed,
					daemon: {
						state: state.daemonState,
						...state.lastPing !== null ? {
							pid: state.lastPing.pid,
							version: state.lastPing.version
						} : {}
					}
				});
			};
		}
		//#endregion
		//#region src/client/navigation/center.ts
		/**
		* Create one DOM-free navigation source for every Agent Center entry point.
		* Opening is always accepted; a shell overlay that mounts later observes the
		* retained snapshot instead of losing the request.
		*/
		function createCenterNavigation() {
			let isOpen = false;
			const listeners = /* @__PURE__ */ new Set();
			const notify = () => {
				for (const listener of [...listeners]) listener();
			};
			const open = () => {
				if (!isOpen) {
					isOpen = true;
					notify();
				}
				return true;
			};
			return {
				open,
				close: () => {
					if (!isOpen) return;
					isOpen = false;
					notify();
				},
				subscribe: (listener) => {
					listeners.add(listener);
					return () => {
						listeners.delete(listener);
					};
				},
				getSnapshot: () => isOpen
			};
		}
		//#endregion
		//#region \0dsh-css:src/client/navigation/sidebar-entry.module.css.mjs
		const css$1 = ".D5hMGG_entry{box-sizing:border-box;border-radius:var(--agsc-radius-control);width:100%;height:36px;color:var(--agsc-fg-secondary);cursor:pointer;font:inherit;text-align:left;white-space:nowrap;background:0 0;border:0;align-items:center;gap:8px;padding:0 10px;font-size:13px;transition:background-color .12s,color .12s,transform .12s;display:flex}.D5hMGG_entry:hover{background:var(--dsw-alias-interactive-bg-hover);color:var(--agsc-fg)}.D5hMGG_entry:active{background:var(--dsw-alias-interactive-bg-active);color:var(--agsc-fg);transform:translateY(1px)}.D5hMGG_entry:focus-visible{outline:2px solid var(--agsc-accent);outline-offset:2px}.D5hMGG_entryIcon{flex:none;justify-content:center;align-items:center;width:24px;height:24px;display:inline-flex}.D5hMGG_entryIcon svg{width:16px;height:16px;display:block}.D5hMGG_entryLabel{text-overflow:ellipsis;overflow:hidden}[data-dsh-frame][data-sidebar-collapsed] .D5hMGG_entry{border-radius:50%;justify-content:center;width:36px;margin:0 auto 12px;padding:0}[data-dsh-frame][data-sidebar-collapsed] .D5hMGG_entryLabel{display:none}@media (prefers-reduced-motion:reduce){.D5hMGG_entry{transition:none}}";
		const tagId$1 = "@shendeguize/dsh-agent-sidecar/src/client/navigation/sidebar-entry.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId$1, css$1);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId$1) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId$1;
				created = true;
			}
			tag.textContent = css$1;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var sidebar_entry_module_css_default = {
			"entry": "D5hMGG_entry",
			"entryIcon": "D5hMGG_entryIcon",
			"entryLabel": "D5hMGG_entryLabel"
		};
		//#endregion
		//#region src/client/navigation/sidebar-entry.ts
		const SIDEBAR_ENTRY_SELECTOR = "[data-agent-sidecar-sidebar-entry]";
		const ENTRY_ATTRIBUTE = "data-agent-sidecar-sidebar-entry";
		const LABEL_ATTRIBUTE = "data-agent-sidecar-sidebar-entry-label";
		const SVG_NS = "http://www.w3.org/2000/svg";
		const ENTRY_BINDING = Symbol.for("@shendeguize/dsh-agent-sidecar/sidebar-entry-binding");
		const ENTRY_BRAND = Symbol.for("@shendeguize/dsh-agent-sidecar/sidebar-entry-brand");
		const ENTRY_DISPATCHER = Symbol.for("@shendeguize/dsh-agent-sidecar/sidebar-entry-dispatcher");
		const warnedForeignEntries = /* @__PURE__ */ new WeakSet();
		function applyCopy(target, copy) {
			target.label.textContent = copy.label;
			target.button.setAttribute("aria-label", copy.accessibilityLabel);
			target.button.title = copy.accessibilityLabel;
		}
		function bindSidebarEntryCopy(target, copy) {
			const update = () => {
				applyCopy(target, copy);
			};
			update();
			return copy.subscribe(update);
		}
		function bindingOf(button) {
			return button[ENTRY_BINDING];
		}
		function hasCurrentDispatcher(button) {
			const record = button;
			return (record[ENTRY_BRAND] === true || record[ENTRY_BINDING] !== void 0) && record[ENTRY_DISPATCHER] === true;
		}
		/** A pure, DOM-independent ownership check; the idempotency attribute alone is foreign. */
		function isOwnedSidebarEntry(candidate) {
			if (candidate === null || typeof candidate !== "object" && typeof candidate !== "function") return false;
			const record = candidate;
			if (record[ENTRY_BRAND] === true || record[ENTRY_BINDING] !== void 0) return true;
			const element = candidate;
			return element.tagName === "BUTTON" && element.getAttribute?.("data-dsh-plugin") === "agent-sidecar" && element.getAttribute?.("data-dsh-part") === "sidebar-entry" && element.querySelector?.(`[${LABEL_ATTRIBUTE}]`) != null;
		}
		function brandSidebarEntry(button) {
			button[ENTRY_BRAND] = true;
		}
		function installClickDispatcher(button) {
			const record = button;
			if (record[ENTRY_DISPATCHER] === true) return;
			button.addEventListener("click", () => {
				openBoundSidebarEntry(button);
			});
			record[ENTRY_DISPATCHER] = true;
		}
		function once(dispose) {
			let active = true;
			return () => {
				if (!active) return;
				active = false;
				dispose();
			};
		}
		/** Invoke the latest cross-bundle binding, never a captured HMR closure. */
		function openBoundSidebarEntry(button) {
			try {
				bindingOf(button)?.openCenter();
			} catch {}
		}
		/** Latest-owner-wins binding over one shared DOM button. */
		function bindSidebarEntryOwner(target, owner, openCenter, copy, startObserver) {
			const previous = bindingOf(target.button);
			previous?.observer?.disconnect();
			previous?.stopCopy();
			const binding = {
				owner,
				openCenter,
				stopCopy: () => {},
				observer: null
			};
			const sharedButton = target.button;
			sharedButton[ENTRY_BINDING] = binding;
			binding.stopCopy = once(bindSidebarEntryCopy(target, copy));
			binding.observer = startObserver();
			return () => {
				binding.observer?.disconnect();
				if (bindingOf(target.button)?.owner !== owner) return;
				binding.stopCopy();
				delete sharedButton[ENTRY_BINDING];
				target.button.remove();
			};
		}
		function createIcon() {
			const icon = document.createElementNS(SVG_NS, "svg");
			for (const [name, value] of Object.entries({
				viewBox: "0 0 16 16",
				width: "16",
				height: "16",
				fill: "none",
				stroke: "currentColor",
				"stroke-width": "1.4",
				"stroke-linecap": "round",
				"stroke-linejoin": "round",
				"aria-hidden": "true",
				focusable: "false"
			})) icon.setAttribute(name, value);
			const orbit = document.createElementNS(SVG_NS, "circle");
			orbit.setAttribute("cx", "8");
			orbit.setAttribute("cy", "8");
			orbit.setAttribute("r", "5.5");
			const center = document.createElementNS(SVG_NS, "circle");
			center.setAttribute("cx", "8");
			center.setAttribute("cy", "8");
			center.setAttribute("r", "1.75");
			const path = document.createElementNS(SVG_NS, "path");
			path.setAttribute("d", "M8 2.5v3.75M8 9.75v3.75M2.5 8h3.75M9.75 8h3.75");
			icon.append(orbit, center, path);
			return icon;
		}
		function createEntry() {
			const entry = document.createElement("button");
			brandSidebarEntry(entry);
			const surface = surfaceProps("sidebar-entry", sidebar_entry_module_css_default.entry);
			entry.type = "button";
			entry.className = surface.className;
			entry.setAttribute(ENTRY_ATTRIBUTE, "");
			entry.setAttribute("data-dsh-plugin", surface["data-dsh-plugin"]);
			entry.setAttribute("data-dsh-part", surface["data-dsh-part"]);
			const icon = document.createElement("span");
			icon.className = sidebar_entry_module_css_default.entryIcon ?? "";
			icon.setAttribute("aria-hidden", "true");
			icon.appendChild(createIcon());
			const label = document.createElement("span");
			label.className = sidebar_entry_module_css_default.entryLabel ?? "";
			label.setAttribute(LABEL_ATTRIBUTE, "");
			entry.append(icon, label);
			installClickDispatcher(entry);
			return {
				button: entry,
				label
			};
		}
		function existingEntry(button) {
			const label = button.querySelector(`[${LABEL_ATTRIBUTE}]`) ?? button.lastElementChild;
			return label === null ? null : {
				button,
				label
			};
		}
		function rejectLegacyEntry() {
			console.warn("[agent-sidecar] Sidebar legacy entry cannot be safely replaced; leaving it untouched.");
			return null;
		}
		function replaceLegacyEntry(button) {
			if (typeof button.cloneNode !== "function" || typeof button.replaceWith !== "function") return rejectLegacyEntry();
			let clone;
			try {
				clone = button.cloneNode(true);
			} catch {
				return rejectLegacyEntry();
			}
			const elements = clone.tagName === "BUTTON" && typeof clone.addEventListener === "function" ? existingEntry(clone) : null;
			if (elements === null) return rejectLegacyEntry();
			try {
				button.replaceWith(clone);
			} catch {
				return rejectLegacyEntry();
			}
			brandSidebarEntry(clone);
			installClickDispatcher(clone);
			return elements;
		}
		function sidebarRoot() {
			const column = document.querySelector("[data-pane=\"sidebar\"], [class*=\"sidebarCol\"]");
			if (column === null) return null;
			return column.querySelector("[class*=\"logoRow\"]")?.parentElement ?? column.firstElementChild;
		}
		function newSessionRow(root) {
			const button = root.querySelector("button[class*=\"newSession\"]") ?? Array.from(root.children).find((child) => child.tagName === "BUTTON");
			if (button === void 0) return null;
			const row = button.closest("[class*=\"logoRow\"]");
			if (row !== null && row.parentElement === root) return row;
			return button.parentElement === root ? button : null;
		}
		function placeEntry(entry) {
			const root = sidebarRoot();
			if (root === null) return false;
			const anchor = newSessionRow(root);
			if (anchor === null) return false;
			if (entry.parentElement !== root || anchor.nextElementSibling !== entry) root.insertBefore(entry, anchor.nextElementSibling);
			return true;
		}
		/**
		* Wait for the sidebar, restore the row after React rebuilds, and return full
		* cleanup. Overlapping applies synchronously adopt the existing row.
		*/
		function mountSidebarEntry(openCenter, copy) {
			if (typeof document === "undefined") return () => {};
			const candidate = document.querySelector(SIDEBAR_ENTRY_SELECTOR);
			if (candidate !== null && !isOwnedSidebarEntry(candidate)) {
				if (!warnedForeignEntries.has(candidate)) {
					warnedForeignEntries.add(candidate);
					console.warn("[agent-sidecar] Sidebar entry collision: refusing to modify a foreign [data-agent-sidecar-sidebar-entry] node.");
				}
				return () => {};
			}
			const elements = candidate === null ? createEntry() : hasCurrentDispatcher(candidate) ? existingEntry(candidate) : replaceLegacyEntry(candidate);
			if (elements === null) return () => {};
			brandSidebarEntry(elements.button);
			const entry = elements.button;
			const owner = {};
			let disposed = false;
			const ensurePlaced = () => {
				if (disposed) return;
				if (entry.isConnected) return;
				const existing = document.querySelector(SIDEBAR_ENTRY_SELECTOR);
				if (existing !== null && existing !== entry) return;
				placeEntry(entry);
			};
			ensurePlaced();
			const disposeBinding = bindSidebarEntryOwner(elements, owner, openCenter, copy, () => {
				if (typeof MutationObserver === "undefined") return null;
				const observer = new MutationObserver(ensurePlaced);
				observer.observe(document.body, {
					childList: true,
					subtree: true
				});
				return observer;
			});
			return () => {
				if (disposed) return;
				disposed = true;
				disposeBinding();
			};
		}
		//#endregion
		//#region \0dsh-css:src/client/sidebar/sidebar-tab.module.css.mjs
		const css = "._6whfGW_root{box-sizing:border-box;min-width:0;color:var(--agsc-fg);background:var(--agsc-bg);flex-direction:column;gap:8px;padding:10px 12px;font-size:12px;display:flex;overflow-y:auto}._6whfGW_header{align-items:center;min-width:0;display:flex}._6whfGW_counts{font-variant-numeric:tabular-nums;max-width:100%;color:var(--agsc-fg-secondary)}._6whfGW_counts>span{color:inherit}._6whfGW_sectionTitle{color:var(--agsc-fg-secondary);margin:0;font-size:11px;font-weight:600;line-height:16px}._6whfGW_sessionList{flex-direction:column;gap:2px;min-width:0;margin:0;padding:0;list-style:none;display:flex}._6whfGW_sessionItem{min-width:0;list-style:none}._6whfGW_sessionButton{width:100%;min-width:0;height:auto;color:var(--agsc-fg);text-align:left;justify-content:flex-start;align-items:center;gap:6px;padding:4px 6px;display:flex}._6whfGW_glyph{color:var(--agsc-accent);flex:none}._6whfGW_sessionTitle{min-width:0;color:var(--agsc-fg);text-overflow:ellipsis;white-space:nowrap;flex:1;overflow:hidden}._6whfGW_sessionMeta{color:var(--agsc-fg-dimmed);white-space:nowrap;flex:none;font-size:11px}._6whfGW_detail{overflow-wrap:anywhere;color:var(--agsc-fg-secondary);margin:0 6px 4px 20px;font-size:11px;line-height:16px}._6whfGW_muted{color:var(--agsc-fg-dimmed);margin:0}._6whfGW_hint{color:var(--agsc-fg-dimmed);margin:0;font-size:11px;line-height:16px}._6whfGW_icon{color:var(--agsc-fg-secondary);flex:none;display:block}._6whfGW_icon path,._6whfGW_icon circle{fill:currentColor}";
		const tagId = "@shendeguize/dsh-agent-sidecar/src/client/sidebar/sidebar-tab.module.css";
		globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest")].set(tagId, css);
		if (typeof document !== "undefined") {
			const selector = "style[data-plugin=\"@shendeguize/dsh-agent-sidecar\"][data-plugin-css=" + JSON.stringify(tagId) + "]";
			let tag = document.querySelector(selector);
			let created = false;
			if (tag === null) {
				tag = document.createElement("style");
				tag.dataset.plugin = "@shendeguize/dsh-agent-sidecar";
				tag.dataset.pluginCss = tagId;
				created = true;
			}
			tag.textContent = css;
			tag[Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner")] = globalThis[Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation")];
			if (created) document.head.appendChild(tag);
		}
		var sidebar_tab_module_css_default = {
			"counts": "_6whfGW_counts",
			"detail": "_6whfGW_detail",
			"glyph": "_6whfGW_glyph",
			"header": "_6whfGW_header",
			"hint": "_6whfGW_hint",
			"icon": "_6whfGW_icon",
			"muted": "_6whfGW_muted",
			"root": "_6whfGW_root",
			"sectionTitle": "_6whfGW_sectionTitle",
			"sessionButton": "_6whfGW_sessionButton",
			"sessionItem": "_6whfGW_sessionItem",
			"sessionList": "_6whfGW_sessionList",
			"sessionMeta": "_6whfGW_sessionMeta",
			"sessionTitle": "_6whfGW_sessionTitle"
		};
		//#endregion
		//#region src/client/sidebar/SidebarTab.tsx
		/** Presentation-only compact view for the optional better-sidebar surface.
		* Integration owns discovery and view-model derivation; this root subscribes
		* once to locale changes for its complete presentation subtree. */
		const STATUS_LABEL_KEY = {
			working: "detail.status.working",
			waiting: "detail.status.waiting",
			idle: "detail.status.idle",
			dead: "detail.status.dead",
			unknown: "detail.status.unknown"
		};
		const CONNECTION_DOT_STATE = {
			ok: "done",
			degraded: "warning",
			off: "error"
		};
		/** Icon renderer kept beside the view while preserving the descriptor callback API. */
		function SidebarTabIcon({ size }) {
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("svg", {
				className: sidebar_tab_module_css_default["icon"],
				width: size,
				height: size,
				viewBox: "0 0 24 24",
				"aria-hidden": true,
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("path", { d: "M12 2 22 12 12 22 2 12 12 2Zm0 3.4L5.4 12l6.6 6.6 6.6-6.6L12 5.4Z" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("circle", {
					cx: "12",
					cy: "12",
					r: "2.2"
				})]
			});
		}
		function SessionRow(props) {
			const { session, nowMs, expanded, detailId } = props;
			const status = normalizeStatus(session.status);
			const title = session.title.trim() === "" ? t("sidebar.untitled") : session.title;
			const lastEvent = session.lastEvent === null ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
				className: sidebar_tab_module_css_default["muted"],
				children: t("sidebar.noEvent")
			}) : `${session.lastEvent.kind}: ${session.lastEvent.text}`;
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("li", {
				className: sidebar_tab_module_css_default["sessionItem"],
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)(_deepseek_ai_dsh_client_ui_primitives.Button, {
					type: "button",
					size: "sm",
					variant: "ghost",
					className: sidebar_tab_module_css_default["sessionButton"],
					onClick: props.onToggle,
					"data-testid": "agent-sidecar-sidebar-session",
					"data-session-id": session.sessionId,
					"data-status": status,
					"aria-expanded": expanded,
					"aria-controls": detailId,
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: sidebar_tab_module_css_default["glyph"],
							"aria-hidden": true,
							children: agentGlyph$1(session.agent)
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: sidebar_tab_module_css_default["sessionTitle"],
							title: session.sessionId,
							children: title
						}),
						/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: sidebar_tab_module_css_default["sessionMeta"],
							children: [
								t(STATUS_LABEL_KEY[status]),
								" · ",
								formatRelativeTime$1(session.updatedAtMs, nowMs)
							]
						})
					]
				}), expanded && /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					id: detailId,
					className: sidebar_tab_module_css_default["detail"],
					role: "region",
					"aria-label": title,
					"data-testid": "agent-sidecar-sidebar-detail",
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", { children: projectDisplayName(session.project) }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", { children: lastEvent })]
				})]
			});
		}
		/** Compact better-sidebar body. No controller, service, or transport imports. */
		function SidebarTab({ vm, visible, nowMs = Date.now() }) {
			useActiveLocale();
			const [expandedId, setExpandedId] = (0, react.useState)(null);
			const recentTitleId = "agent-sidecar-sidebar-recent-title";
			let body;
			if (!vm.hasSnapshot) body = /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
				className: sidebar_tab_module_css_default["muted"],
				children: t("sidebar.connecting")
			});
			else if (vm.recent.length === 0) body = /* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
				className: sidebar_tab_module_css_default["muted"],
				children: t("sidebar.noSessions")
			});
			else body = /* @__PURE__ */ (0, react_jsx_runtime.jsx)("ul", {
				className: sidebar_tab_module_css_default["sessionList"],
				"aria-labelledby": recentTitleId,
				children: vm.recent.map((session, index) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)(SessionRow, {
					session,
					nowMs,
					expanded: expandedId === session.sessionId,
					detailId: `agent-sidecar-sidebar-detail-${index}`,
					onToggle: () => {
						setExpandedId((previous) => previous === session.sessionId ? null : session.sessionId);
					}
				}, session.sessionId))
			});
			return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("section", {
				...surfaceProps("sidebar-tab", sidebar_tab_module_css_default["root"]),
				"data-testid": "agent-sidecar-sidebar-tab",
				"data-visible": visible,
				"aria-label": t("sidebar.tabTitle"),
				"aria-hidden": !visible,
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("header", {
						className: sidebar_tab_module_css_default["header"],
						title: vm.connectionTitle,
						children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)(StaticPill, {
							className: sidebar_tab_module_css_default["counts"],
							"data-testid": "agent-sidecar-sidebar-counts",
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.StateDot, {
								state: CONNECTION_DOT_STATE[vm.connection],
								size: 8
							}), t("sidebar.countsRow", {
								working: vm.workingCount,
								waiting: vm.waitingCount
							})]
						})
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("h2", {
						id: recentTitleId,
						className: sidebar_tab_module_css_default["sectionTitle"],
						children: t("sidebar.recentTitle")
					}),
					body,
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("p", {
						className: sidebar_tab_module_css_default["hint"],
						children: t("sidebar.boardHint")
					})
				]
			});
		}
		/** Count of sessions currently observed as waiting. */
		function countWaiting(sessions) {
			let count = 0;
			for (const session of sessions) if (normalizeStatus(session.status) === "waiting") count += 1;
			return count;
		}
		/**
		* Return non-dead sessions by descending update time, capped for the compact
		* view. Ties break by session id for stability.
		*/
		function recentActiveSessions(sessions, limit = 5) {
			return sessions.filter((session) => normalizeStatus(session.status) !== "dead").sort((a, b) => b.updatedAtMs !== a.updatedAtMs ? b.updatedAtMs - a.updatedAtMs : a.sessionId.localeCompare(b.sessionId)).slice(0, limit);
		}
		/** Fold the shared view state into the mini tab's view model. */
		function deriveMiniVM(state) {
			const connection = deriveWidgetConnection(state.daemonState, state.streamHealth);
			const workingCount = countWorking(state.sessions);
			return {
				connection,
				connectionTitle: widgetTitle(connection, workingCount),
				workingCount,
				waitingCount: countWaiting(state.sessions),
				recent: recentActiveSessions(state.sessions),
				hasSnapshot: state.hasSnapshot
			};
		}
		//#endregion
		//#region src/client/sidebar-tab.tsx
		/**
		* Soft dsh-better-sidebar integration (design §5.2 / ADR-5 option C).
		* The dependency stays duck-typed: absence parks a zero-resource inject
		* fiber, presence registers one compact tab over the shared controller.
		* `visible=false` drops only this view's subscription; the plugin-lifetime
		* controller remains untouched.
		*/
		/** Registered tab type id (design §5.2 names it verbatim). */
		const SIDEBAR_TAB_ID = "agent-sidecar:monitor";
		/**
		* Duck-type a candidate service value: any object exposing a callable
		* `registerTab` qualifies (the registry contract is stable since
		* better-sidebar v0.4.0). Anything else — absent service, or a foreign
		* object squatting on the service name — reads as "not installed".
		*/
		function probeBetterSidebar(candidate) {
			if (typeof candidate !== "object" || candidate === null) return null;
			return typeof candidate.registerTab === "function" ? candidate : null;
		}
		/**
		* A pausable read-through store between the shared controller and one tab
		* instance. While visible it mirrors the controller (subscribed, snapshots
		* flow, listeners notified); while hidden it holds NO controller
		* subscription — upstream notifications cost this view nothing — and serves
		* the last seen snapshot. Turning visible again resubscribes and catches up
		* once. Methods are bound fields so uSES sees stable identities.
		*/
		var VisibleGatedStore = class {
			source;
			listeners = /* @__PURE__ */ new Set();
			unsubscribe = null;
			snapshot;
			disposed = false;
			constructor(source) {
				this.source = source;
				this.snapshot = source.getState();
			}
			/** Whether the upstream controller subscription is currently held. */
			get subscribed() {
				return this.unsubscribe !== null;
			}
			subscribe = (listener) => {
				this.listeners.add(listener);
				return () => {
					this.listeners.delete(listener);
				};
			};
			getState = () => this.snapshot;
			/** Idempotent visibility switch: subscribe + catch up, or unsubscribe. */
			setVisible(visible) {
				if (this.disposed || visible === this.subscribed) return;
				if (visible) {
					this.unsubscribe = this.source.subscribe(() => {
						this.pull();
					});
					this.pull();
				} else {
					this.unsubscribe?.();
					this.unsubscribe = null;
				}
			}
			/** Terminal teardown (component unmount): drop upstream and listeners. */
			dispose() {
				this.disposed = true;
				this.unsubscribe?.();
				this.unsubscribe = null;
				this.listeners.clear();
			}
			pull() {
				const next = this.source.getState();
				if (next === this.snapshot) return;
				this.snapshot = next;
				for (const fn of [...this.listeners]) fn();
			}
		};
		/**
		* Bind the presentation-only tab to the shared controller. One
		* {@link VisibleGatedStore} per mounted tab instance: the `visible` prop is
		* synced into it by effect, unmount disposes it.
		*/
		function createSidebarTabComponent(controller) {
			return function SidecarSidebarTab({ visible }) {
				const [gate] = (0, react.useState)(() => new VisibleGatedStore(controller));
				(0, react.useEffect)(() => () => {
					gate.dispose();
				}, [gate]);
				(0, react.useEffect)(() => {
					gate.setVisible(visible);
				}, [gate, visible]);
				return (0, react.createElement)(SidebarTab, {
					vm: deriveMiniVM((0, react.useSyncExternalStore)(gate.subscribe, gate.getState, gate.getState)),
					visible
				});
			};
		}
		/**
		* Park the better-sidebar integration on the optional service. Not
		* installed → the inject fiber never activates (silent skip, one debug
		* line, zero resources). Installed (before or after this plugin, order
		* does not matter) → duck-type the service and register the mini tab
		* inside `ctx.effect`, so plugin unload / HMR unregisters it. During overlap,
		* the new fiber waits briefly for the old tab to leave, then contributes its
		* own component closure. Foreign collisions time out without eviction.
		*/
		function mountSidebarTab(ctx, controller) {
			const mount = ctx;
			if (probeBetterSidebar(mount.get("betterSidebar")) === null) console.debug("agent-sidecar: better-sidebar not detected; the optional sidebar tab stays idle");
			mount.inject(["betterSidebar"], (injected) => {
				const bctx = injected;
				const service = probeBetterSidebar(bctx.get("betterSidebar"));
				if (service === null) {
					console.debug("agent-sidecar: betterSidebar service lacks registerTab; skipping tab");
					return;
				}
				const component = createSidebarTabComponent(controller);
				bctx.effect(() => acquireWithHandoff(() => service.registerTab({
					id: SIDEBAR_TAB_ID,
					title: () => t("sidebar.tabTitle"),
					icon: (size) => (0, react.createElement)(SidebarTabIcon, { size }),
					order: 60,
					single: true,
					component
				}), {
					isCollision: isRegistrationCollision,
					onError: (error) => {
						console.error("agent-sidecar: better-sidebar tab registration failed", error);
					},
					onTimeout: () => {
						console.error("agent-sidecar: better-sidebar tab handoff timed out");
					}
				}), "agent-sidecar: better-sidebar tab");
			});
		}
		//#endregion
		//#region src/client/analysis-glue.ts
		/**
		* AI bypass-analysis data glue (T5.10b, design §4.e.3 / §5.1): a
		* framework-free store per analysis conversation, driving the controlled
		* AnalysisPanel over the `POST action` `analysis.*` trio (host contract:
		* routes.ts handleAnalysis* over analysis.ts AnalysisEngine).
		*
		* Wire semantics this store encodes:
		* - a settled engine result comes back with the HTTP status derived from
		*   its `errorCode` (timeout→504, too_many_active→429, create_failed→502,
		*   cancelled→200); api.ts postAction folds non-200 bodies into ApiError
		*   with `reason` = that code — so failures arrive here as ApiError and
		*   the cancelled-terminal arrives as a 200 body;
		* - the write gate answers 403 `analysis_disabled` (fail-closed), an
		*   agents-less composition answers 501 `analysis_unavailable`, and a
		*   pre-analysis host answers 400 `unknown_action` (mapped to the same
		*   honest "unavailable" terminal);
		* - a request timeout DISPOSES the engine session (terminal), a followup
		*   timeout KEEPS it (non-terminal notice; analysis.ts contract).
		*
		* Token honesty: this store dials analysis turns only on explicit user
		* intent (start / followup / stop) — never automatically. The one
		* automatic call is dispose()'s fire-and-forget cancel, which spends no
		* tokens and only releases the engine session slot (F2).
		*
		* @module
		*/
		function analysisRequestEnvelope(target) {
			return {
				type: "analysis.request",
				targetKind: target.targetKind,
				...target.targetId !== void 0 ? { targetId: target.targetId } : {},
				...target.question !== void 0 && target.question.trim() !== "" ? { question: target.question } : {}
			};
		}
		function analysisFollowupEnvelope(analysisSessionId, question) {
			return {
				type: "analysis.followup",
				analysisSessionId,
				question
			};
		}
		function analysisCancelEnvelope(analysisSessionId) {
			return {
				type: "analysis.cancel",
				analysisSessionId
			};
		}
		const INITIAL_STATE$1 = {
			phase: "idle",
			analysisSessionId: null,
			exchanges: [],
			disclaimer: null,
			errorCode: null,
			noticeCode: null
		};
		/** ApiError reason codes that keep a live followup session usable. */
		const RETRYABLE_FOLLOWUP_CODES = /* @__PURE__ */ new Set([
			"timeout",
			"request_timeout",
			"network_error"
		]);
		function failureCode(err) {
			if (isApiError(err)) return err.reason === "unknown_action" ? "analysis_unavailable" : err.reason;
			return "network_error";
		}
		var AnalysisStore = class {
			state = INITIAL_STATE$1;
			listeners = /* @__PURE__ */ new Set();
			postActionFn;
			timeoutMs;
			disposed = false;
			constructor(options = {}) {
				this.postActionFn = options.postActionFn ?? postAction;
				this.timeoutMs = options.timeoutMs ?? 75e3;
			}
			subscribe = (fn) => {
				this.listeners.add(fn);
				return () => {
					this.listeners.delete(fn);
				};
			};
			getState = () => this.state;
			setState(patch) {
				if (this.disposed) return;
				this.state = {
					...this.state,
					...patch
				};
				for (const fn of [...this.listeners]) fn();
			}
			post(body) {
				const opts = { timeoutMs: this.timeoutMs };
				return this.postActionFn(body, opts);
			}
			/** Start one analysis (allowed from idle and from the terminal phases). */
			async start(target) {
				const { phase } = this.state;
				if (this.disposed || phase === "requesting" || phase === "ready" || phase === "answering") return;
				this.setState({
					...INITIAL_STATE$1,
					phase: "requesting"
				});
				try {
					const result = await this.post(analysisRequestEnvelope(target));
					if (this.disposed) {
						const id = result?.analysisSessionId;
						if (result?.outcome === "completed" && typeof id === "string" && id !== "") this.fireCancel(id);
						return;
					}
					if (this.state.phase !== "requesting") return;
					this.adoptResult(null, result);
				} catch (err) {
					if (this.disposed || this.state.phase !== "requesting") return;
					this.setState({
						phase: "failed",
						errorCode: failureCode(err)
					});
				}
			}
			/** Ask a follow-up in the live analysis session. */
			async followup(question) {
				const id = this.state.analysisSessionId;
				const q = question.trim();
				if (this.disposed || this.state.phase !== "ready" || id === null || q === "") return;
				this.setState({
					phase: "answering",
					noticeCode: null
				});
				const phaseNow = () => this.state.phase;
				try {
					const result = await this.post(analysisFollowupEnvelope(id, q));
					if (this.disposed || phaseNow() !== "answering") return;
					this.adoptResult(q, result);
				} catch (err) {
					if (this.disposed || phaseNow() !== "answering") return;
					const code = failureCode(err);
					if (RETRYABLE_FOLLOWUP_CODES.has(code)) this.setState({
						phase: "ready",
						noticeCode: code
					});
					else this.setState({
						phase: "failed",
						errorCode: code
					});
				}
			}
			/** Release the analysis session (idempotent on the engine side). */
			async stop() {
				const id = this.state.analysisSessionId;
				const { phase } = this.state;
				if (this.disposed || id === null || phase !== "ready" && phase !== "answering") return;
				try {
					await this.post(analysisCancelEnvelope(id));
					if (this.disposed) return;
					this.setState({
						phase: "stopped",
						noticeCode: null
					});
				} catch {
					if (this.disposed) return;
					this.setState({ noticeCode: "cancel_failed" });
				}
			}
			/** Fold one settled 200 result into the conversation. */
			adoptResult(question, result) {
				if (result.outcome === "completed") {
					this.setState({
						phase: "ready",
						analysisSessionId: result.analysisSessionId ?? this.state.analysisSessionId,
						exchanges: [...this.state.exchanges, {
							question,
							summary: result.summary ?? "",
							truncated: result.truncated,
							tokensHint: result.tokensHint ?? null
						}],
						disclaimer: result.disclaimer,
						errorCode: null,
						noticeCode: null
					});
					return;
				}
				this.setState({
					phase: result.errorCode === "cancelled" || result.outcome === "cancelled" ? "stopped" : "failed",
					errorCode: result.errorCode ?? result.outcome,
					disclaimer: result.disclaimer
				});
			}
			/**
			* Late settlements become no-ops; subscribers are dropped. Idempotent.
			* A live engine session is released with a fire-and-forget cancel so
			* unmount / session-switch remounts cannot strand `maxActiveSessions`
			* slots until plugin unload (F2). Cancel is idempotent on the engine
			* side; a transport failure is silently ignored (nothing to surface —
			* the store is gone).
			*/
			dispose() {
				if (this.disposed) return;
				const { analysisSessionId, phase } = this.state;
				this.disposed = true;
				this.listeners.clear();
				if (analysisSessionId !== null && (phase === "ready" || phase === "answering")) this.fireCancel(analysisSessionId);
			}
			/** Fire-and-forget analysis.cancel (failures are deliberately silent). */
			fireCancel(analysisSessionId) {
				this.post(analysisCancelEnvelope(analysisSessionId)).catch(() => {});
			}
		};
		//#endregion
		//#region src/client/search-glue.ts
		/**
		* Cross-agent search data glue (T5.10b): a framework-free store feeding
		* the controlled SearchPanel. Normalization (matchedBy vocabulary, snippet
		* highlighting) stays in dsh-tools/logic.ts — this store owns the query
		* box state and transport orchestration only.
		*
		* Submit model: explicit user submit (no as-you-type dialing). A blank
		* query with no project filter clears the results locally — the host
		* answers 400 `invalid_request` for it, so the store never dials that.
		* Stale results are replaced per settle; a failed submit keeps the last
		* results visible with an error banner (SearchPanel renders error-first).
		*
		* Same store discipline as controller.ts: subscribe/getState for
		* `useSyncExternalStore`, immutable snapshots, dispose() = late no-ops.
		* Out-of-order settles are ignored via a submit ticket.
		*
		* @module
		*/
		const INITIAL_STATE = {
			query: "",
			submittedQuery: "",
			mode: "full-text",
			items: [],
			loading: false,
			error: null,
			project: null
		};
		var SearchStore = class {
			state;
			listeners = /* @__PURE__ */ new Set();
			fetchSearchFn;
			limit;
			disposed = false;
			ticket = 0;
			constructor(options = {}) {
				this.fetchSearchFn = options.fetchSearchFn ?? fetchSearch;
				this.limit = options.limit;
				this.state = {
					...INITIAL_STATE,
					project: options.project ?? null
				};
			}
			subscribe = (fn) => {
				this.listeners.add(fn);
				return () => {
					this.listeners.delete(fn);
				};
			};
			getState = () => this.state;
			setState(patch) {
				if (this.disposed) return;
				this.state = {
					...this.state,
					...patch
				};
				for (const fn of [...this.listeners]) fn();
			}
			/** Controlled input change (no dialing). */
			setQuery(query) {
				this.setState({ query });
			}
			/** Submit the current query; blank + no project filter clears locally. */
			async submit() {
				if (this.disposed) return;
				const q = this.state.query.trim();
				const project = this.state.project;
				if (q === "" && (project === null || project.trim() === "")) {
					this.ticket += 1;
					this.setState({
						items: [],
						submittedQuery: "",
						loading: false,
						error: null
					});
					return;
				}
				const ticket = this.ticket += 1;
				this.setState({
					loading: true,
					error: null
				});
				try {
					const response = await this.fetchSearchFn({
						q,
						project,
						...this.limit !== void 0 ? { limit: this.limit } : {}
					});
					if (this.disposed || ticket !== this.ticket) return;
					this.adoptResponse(response);
				} catch (err) {
					if (this.disposed || ticket !== this.ticket) return;
					this.setState({
						loading: false,
						error: isApiError(err) ? err.reason : "network_error"
					});
				}
			}
			/** Apply one settled wire response (public seam for tests/materialize). */
			adoptResponse(response) {
				this.setState({
					items: normalizeSearchItems(response),
					mode: response.mode,
					submittedQuery: response.query,
					project: response.project,
					loading: false,
					error: null
				});
			}
			dispose() {
				this.disposed = true;
				this.listeners.clear();
			}
		};
		//#endregion
		//#region src/client/ui-integration.ts
		/**
		* UI composition ports for the board and session-detail surfaces.
		*
		* Views depend on these narrow factory seams instead of owning the
		* application assembly contract. The default factory binds the existing
		* stores; callers can provide alternate port implementations in tests or
		* other materializations.
		*
		* @module
		*/
		/** Bind the production store implementations at the client composition root. */
		function createDefaultIntegration(base) {
			return {
				detail: {
					...base,
					createDetailStore: (sessionId, hint) => new DetailStore(sessionId, { hint }),
					createSearchStore: () => new SearchStore(),
					createAnalysisStore: () => new AnalysisStore()
				},
				createProjectsStore: () => new ProjectsStore()
			};
		}
		//#endregion
		//#region src/client/index.ts
		const name = "agent-sidecar";
		/** The slot registry is the only hard dependency; settingsScope and locale are lazy. */
		const inject = ["slots"];
		/** Entry id (list slots) and cell key (settings keyed slot) in one. */
		const ENTRY_ID = "agent-sidecar";
		/** Distinct list-seat id for the frame-wide Agent Center surface. */
		const CENTER_ENTRY_ID = "agent-sidecar-center";
		/** Host-side settings namespace (host half registers it via ctx.settings). */
		const SETTINGS_NAMESPACE = "agent-sidecar";
		/**
		* Apply-guard: whether the slot ledger already holds this plugin's entry
		* (list slots match their seat-specific `id`, the keyed settings slot by
		* namespace `key`). `entries()` answers [] for undeclared slots, and the
		* check runs inside the deferred `slots.inject` callback — i.e. at actual
		* registration time, when the ledger is authoritative.
		*/
		function hasOwnEntry(ctx, slot) {
			const entryId = slot === "shell.overlay" ? CENTER_ENTRY_ID : ENTRY_ID;
			return ctx.slots.entries(slot).some((entry) => entry.options.id === entryId || slot === "settings.plugin.item" && entry.options.key === SETTINGS_NAMESPACE);
		}
		/**
		* Lease the optional Host locale service for this injected child fiber.
		* The locale core arbitrates overlapping fibers that share one service.
		*/
		function mountHostLocale(ctx) {
			ctx.inject(["locale"], (injected) => {
				const lctx = injected;
				lctx.effect(() => attachHostLocale(lctx), "agent-sidecar: host locale bridge");
			});
		}
		const STYLE_OWNER = Symbol.for("@shendeguize/dsh-agent-sidecar/style-owner");
		const STYLE_MANIFEST = Symbol.for("@shendeguize/dsh-agent-sidecar/style-manifest");
		const STYLE_GENERATION = Symbol.for("@shendeguize/dsh-agent-sidecar/style-generation");
		/**
		* A fallback only for a sequential re-apply of this exact materialization.
		* A new intro installs a new generation object, so it can never inherit an
		* earlier materialization's cached CSS.
		*/
		let cachedStyleManifest;
		function setStyleOwner(tag, owner) {
			const sharedTag = tag;
			sharedTag[STYLE_OWNER] = owner;
		}
		function isStyleOwner(tag, owner) {
			return tag[STYLE_OWNER] === owner;
		}
		/**
		* Freeze the current materialization's CSS before another bundle can reset
		* it. A present Map is authoritative, including when it is empty.
		*/
		function snapshotStyleManifest(globals) {
			const generation = globals[STYLE_GENERATION];
			const manifest = globals[STYLE_MANIFEST];
			if (manifest instanceof Map) {
				const styles = /* @__PURE__ */ new Map();
				for (const [tagId, cssText] of manifest) if (typeof tagId === "string" && typeof cssText === "string") styles.set(tagId, cssText);
				if (typeof generation === "object" && generation !== null) cachedStyleManifest = {
					generation,
					styles
				};
				return styles;
			}
			if (typeof generation === "object" && generation !== null && cachedStyleManifest?.generation === generation) return new Map(cachedStyleManifest.styles);
			return /* @__PURE__ */ new Map();
		}
		/**
		* Keeper effect for the tsdown-injected `<style data-plugin>` tags. Ownership
		* lives on the DOM node through Symbol.for so the latest HMR fiber wins. The
		* current bundle manifest is authoritative: plugin tags for CSS modules
		* absent from it are stale and removed.
		*/
		function keepStylesAlive(documentRef = typeof document === "undefined" ? void 0 : document, globals = globalThis) {
			if (documentRef === void 0) return () => {};
			const manifest = snapshotStyleManifest(globals);
			const owner = {};
			const ownTags = `style[data-plugin=${JSON.stringify(PLUGIN_ID)}]`;
			for (const el of Array.from(documentRef.querySelectorAll(ownTags))) {
				const tag = el;
				const key = tag.dataset["pluginCss"];
				const cssText = key === void 0 ? void 0 : manifest.get(key);
				if (cssText === void 0) {
					tag.remove();
					continue;
				}
				tag.textContent = cssText;
				setStyleOwner(tag, owner);
			}
			for (const [key, cssText] of manifest) {
				const selector = `${ownTags}[data-plugin-css=${JSON.stringify(key)}]`;
				if (documentRef.querySelector(selector) === null) {
					const tag = documentRef.createElement("style");
					tag.dataset["plugin"] = PLUGIN_ID;
					tag.dataset["pluginCss"] = key;
					tag.textContent = cssText;
					setStyleOwner(tag, owner);
					documentRef.head.appendChild(tag);
				}
			}
			return () => {
				for (const el of Array.from(documentRef.querySelectorAll(ownTags))) if (isStyleOwner(el, owner)) el.remove();
			};
		}
		/**
		* Mount the board tab, footer widget, and settings card, and run the data
		* controller for as long as the plugin fiber lives.
		* @param ctx - browser plugin context handed by the client loader.
		*/
		function apply(ctx) {
			const controller = new SidecarController();
			const navigation = createCenterNavigation();
			const openAgentCenter = navigation.open;
			const sidebarEntryCopy = {
				get label() {
					return t("sidebar.centerEntryLabel");
				},
				get accessibilityLabel() {
					return t("sidebar.centerEntryAria");
				},
				subscribe: subscribeLocale
			};
			try {
				mountHostLocale(ctx);
			} catch {}
			const injectPrefs = { defaultMode: "queue" };
			const analysisPrefs = { enabled: false };
			const uiIntegration = createDefaultIntegration({
				inject: {
					actions: createInjectActions({ onDelivered: () => {
						controller.refresh();
					} }),
					getDefaultMode: () => injectPrefs.defaultMode
				},
				getAnalysisEnabled: () => analysisPrefs.enabled
			});
			ctx.effect(() => {
				try {
					controller.start();
				} catch (err) {
					console.error("agent-sidecar: data stream start failed", err);
				}
				if (typeof document === "undefined") return () => {
					controller.stop();
				};
				const onVisibility = () => {
					if (!document.hidden) controller.pollNow();
				};
				document.addEventListener("visibilitychange", onVisibility);
				return () => {
					document.removeEventListener("visibilitychange", onVisibility);
					controller.stop();
				};
			}, "agent-sidecar: client data feed");
			ctx.effect(keepStylesAlive, "agent-sidecar: injected styles");
			try {
				const SidecarCenterOverlay = createCenterOverlay(controller, uiIntegration, navigation);
				ctx.slots.inject("shell.overlay", () => acquireWithHandoff(() => hasOwnEntry(ctx, "shell.overlay") ? void 0 : ctx.slots.register({
					name: "shell.overlay",
					id: CENTER_ENTRY_ID,
					order: 30
				}, SidecarCenterOverlay), {
					isCollision: isRegistrationCollision,
					onError: (error) => {
						console.error("agent-sidecar: center overlay registration failed", error);
					},
					onTimeout: () => {
						console.error("agent-sidecar: center overlay handoff timed out");
					}
				}));
			} catch (err) {
				console.error("agent-sidecar: center overlay mount failed", err);
			}
			try {
				ctx.effect(() => mountSidebarEntry(openAgentCenter, sidebarEntryCopy), "agent-sidecar: sidebar entry");
			} catch (err) {
				console.error("agent-sidecar: sidebar entry mount failed", err);
			}
			try {
				const SidecarBoardTab = createBoardTab(controller, uiIntegration);
				ctx.slots.inject("conversation.view", () => acquireWithHandoff(() => hasOwnEntry(ctx, "conversation.view") ? void 0 : ctx.slots.register({
					name: "conversation.view",
					id: ENTRY_ID,
					order: 30,
					label: "Sidecar"
				}, SidecarBoardTab), {
					isCollision: isRegistrationCollision,
					onError: (error) => {
						console.error("agent-sidecar: board tab registration failed", error);
					},
					onTimeout: () => {
						console.error("agent-sidecar: board tab handoff timed out");
					}
				}));
			} catch (err) {
				console.error("agent-sidecar: board tab mount failed", err);
			}
			try {
				const SidecarFooterWidget = createFooterWidget(controller, openAgentCenter);
				ctx.slots.inject("sidebar.footer.action", () => acquireWithHandoff(() => hasOwnEntry(ctx, "sidebar.footer.action") ? void 0 : ctx.slots.register({
					name: "sidebar.footer.action",
					id: ENTRY_ID,
					order: 30
				}, SidecarFooterWidget), {
					isCollision: isRegistrationCollision,
					onError: (error) => {
						console.error("agent-sidecar: footer widget registration failed", error);
					},
					onTimeout: () => {
						console.error("agent-sidecar: footer widget handoff timed out");
					}
				}));
			} catch (err) {
				console.error("agent-sidecar: footer widget mount failed", err);
			}
			try {
				ctx.inject(["settingsScope"], (injected) => {
					const sctx = injected;
					try {
						const scope = sctx.settingsScope.bind({ namespace: SETTINGS_NAMESPACE });
						const adoptDefaults = () => {
							const value = scope.getSnapshot().value;
							if (value?.ui !== void 0) controller.adoptConfigDefaults(value.ui);
							if (value?.inject !== void 0) injectPrefs.defaultMode = value.inject.defaultMode;
							if (value?.analysis !== void 0) analysisPrefs.enabled = value.analysis.enabled;
						};
						sctx.effect(() => scope.subscribe(adoptDefaults), "agent-sidecar: settings→filter defaults");
						adoptDefaults();
						const SidecarSettingsCardEntry = createSettingsCardEntry(controller, scope);
						sctx.slots.inject("settings.plugin.item", () => acquireWithHandoff(() => hasOwnEntry(sctx, "settings.plugin.item") ? void 0 : sctx.slots.register({
							name: "settings.plugin.item",
							key: SETTINGS_NAMESPACE
						}, SidecarSettingsCardEntry), {
							isCollision: isRegistrationCollision,
							onError: (error) => {
								console.error("agent-sidecar: settings card registration failed", error);
							},
							onTimeout: () => {
								console.error("agent-sidecar: settings card handoff timed out");
							}
						}));
					} catch (err) {
						console.error("agent-sidecar: settings card mount failed", err);
					}
				});
			} catch (err) {
				console.error("agent-sidecar: settings scope injection failed", err);
			}
			try {
				registerSidecarCommand(ctx, { openCenter: openAgentCenter });
			} catch (err) {
				console.error("agent-sidecar: /sidecar command mount failed", err);
			}
			try {
				mountSidebarTab(ctx, controller);
			} catch (err) {
				console.error("agent-sidecar: better-sidebar tab mount failed", err);
			}
		}
		//#endregion
		exports.apply = apply;
		exports.inject = inject;
		exports.name = name;
		return module.exports;
	}
});

//# sourceMappingURL=client.js.map